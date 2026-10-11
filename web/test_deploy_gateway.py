"""Tests for the deploy gateway: auth, ref validation, guards and the happy path.

Everything runs against throwaway git repos in a temp dir — no network, no
systemd, no real checkout of this project.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from web.deploy_runner import DeployError, deploy, repo_status

_GIT_ID = ["-c", "user.email=test@example.com", "-c", "user.name=Test"]


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _make_repo(base: Path) -> Path:
    """Bare `origin.git` + a working clone with two commits on main, pushed."""
    origin = base / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    repo = base / "work"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "branch", "-M", "main")
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / "file.txt").write_text("one\n")
    _git(repo, "add", "-A")
    _git(repo, *_GIT_ID, "commit", "-q", "-m", "one")
    (repo / "file.txt").write_text("two\n")
    _git(repo, "add", "-A")
    _git(repo, *_GIT_ID, "commit", "-q", "-m", "two")
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


class DeployRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repo = _make_repo(self.base)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rejects_malicious_refs(self) -> None:
        for bad in ("../x", "main; rm -rf /", "-x", "a//b", "x..y", "main/"):
            with self.assertRaises(DeployError) as ctx:
                deploy(self.repo, bad)
            self.assertEqual(ctx.exception.status, 400, bad)

    def test_missing_ref_is_404(self) -> None:
        with self.assertRaises(DeployError) as ctx:
            deploy(self.repo, "no-such-branch")
        self.assertEqual(ctx.exception.status, 404)

    def test_dirty_tree_refused_unless_forced(self) -> None:
        (self.repo / "file.txt").write_text("dirty\n")
        with self.assertRaises(DeployError) as ctx:
            deploy(self.repo, "main")
        self.assertEqual(ctx.exception.status, 409)
        # force bypasses the guard
        result = deploy(self.repo, "main", force=True)
        self.assertTrue(result.ok)

    def test_untracked_files_do_not_block(self) -> None:
        (self.repo / "newfile.txt").write_text("new\n")  # untracked only
        self.assertTrue(deploy(self.repo, "main").ok)

    def test_happy_path_checks_out_remote_commit(self) -> None:
        # Advance origin/main, then rewind local so deploy must move HEAD forward.
        (self.repo / "file.txt").write_text("three\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, *_GIT_ID, "commit", "-q", "-m", "three")
        _git(self.repo, "push", "-q", "origin", "main")
        remote_sha = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "reset", "--hard", "-q", "HEAD~1")

        result = deploy(self.repo, "main")
        self.assertTrue(result.ok)
        self.assertEqual(result.commit, remote_sha)
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD"), remote_sha)
        self.assertEqual(_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual((self.repo / "file.txt").read_text(), "three\n")

    def test_commit_mismatch_refused(self) -> None:
        with self.assertRaises(DeployError) as ctx:
            deploy(self.repo, "main", "0" * 40)
        self.assertEqual(ctx.exception.status, 409)

    def test_allowlist_blocks_other_branches(self) -> None:
        _git(self.repo, "checkout", "-q", "-b", "feature")
        (self.repo / "file.txt").write_text("feature\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, *_GIT_ID, "commit", "-q", "-m", "feature")
        _git(self.repo, "push", "-q", "origin", "feature")
        with self.assertRaises(DeployError) as ctx:
            deploy(self.repo, "feature", allowed_refs=["main"])
        self.assertEqual(ctx.exception.status, 403)

    def test_repo_status_reports_dirty(self) -> None:
        _, _, dirty = repo_status(self.repo)
        self.assertFalse(dirty)
        (self.repo / "file.txt").write_text("x\n")
        _, _, dirty = repo_status(self.repo)
        self.assertTrue(dirty)


class DeployGatewayHttpTests(unittest.TestCase):
    TOKEN = "s3cret-deploy-token"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = _make_repo(Path(self._tmp.name))
        self.patch = patch("web.deploy_gateway.settings")
        self.s = self.patch.start()
        self.s.deploy_gateway_token = self.TOKEN
        # The gateway reads these off the real Settings object's properties;
        # on a mock, set the resolved values directly.
        self.s.deploy_repo_path = self.repo
        self.s.deploy_units = []
        self.s.deploy_ref_allowlist = []
        self.s.deploy_allow_dirty = False
        from web.deploy_gateway import create_app

        self.app = create_app()
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.patch.stop()
        self._tmp.cleanup()

    _DEFAULT = object()

    def _post(self, body: dict, token=_DEFAULT):
        tok = self.TOKEN if token is self._DEFAULT else token
        headers = {"Authorization": f"Bearer {tok}"} if tok else {}
        return self.client.post("/deploy", json=body, headers=headers)

    def test_health_open(self) -> None:
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_missing_token_401(self) -> None:
        self.assertEqual(self._post({"ref": "main"}, token=None).status_code, 401)

    def test_wrong_token_401(self) -> None:
        self.assertEqual(self._post({"ref": "main"}, token="nope").status_code, 401)

    def test_unconfigured_token_503(self) -> None:
        self.s.deploy_gateway_token = ""
        self.assertEqual(self._post({"ref": "main"}).status_code, 503)

    def test_bad_ref_400(self) -> None:
        self.assertEqual(self._post({"ref": "../etc"}).status_code, 400)

    def test_deploy_ok(self) -> None:
        r = self._post({"ref": "main"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_busy_returns_409(self) -> None:
        lock = self.app.state.deploy_lock
        lock.acquire()
        try:
            self.assertEqual(self._post({"ref": "main"}).status_code, 409)
        finally:
            lock.release()


if __name__ == "__main__":
    unittest.main()

"""Deploy runner — the fixed, non-interactive steps the gateway executes.

The deploy gateway is an HTTP endpoint that runs code, so everything here is
deliberately narrow: a fixed list of ``git``/``pip``/``systemctl`` commands,
argument lists (never a shell), and a ``ref`` that is regex-validated and then
resolved to an immutable commit SHA before use.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from utils.logger import log

# A branch/ref name: no leading dash, no ``..``/``//``/trailing slash, and a
# character set that cannot smuggle shell or option syntax.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

GIT_TIMEOUT = 180
PIP_TIMEOUT = 900
SYSTEMCTL_TIMEOUT = 60


class DeployError(Exception):
    """An expected deploy failure. ``status`` maps to the HTTP response code."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class DeployResult:
    ok: bool
    ref: str
    commit: str = ""
    steps: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "ref": self.ref,
            "commit": self.commit,
            "steps": self.steps,
            "error": self.error,
        }


def _run(cmd: list[str], *, cwd: Path, timeout: int, what: str) -> str:
    """Run one fixed command; raise DeployError on any failure."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise DeployError(f"{what}: command not found: {cmd[0]}", status=500) from exc
    except subprocess.TimeoutExpired as exc:
        raise DeployError(f"{what}: timed out after {timeout}s", status=504) from exc
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        raise DeployError(
            f"{what} failed (exit {proc.returncode}): {output or '(no output)'}",
            status=500,
        )
    return output


def repo_status(repo: Path) -> tuple[str, str, bool]:
    """Return ``(branch, sha, dirty)`` for the working tree at ``repo``.

    ``dirty`` counts tracked modifications only: untracked files survive a
    checkout, so they must not block a ref-based deploy.
    """
    branch = _run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, timeout=30, what="git branch"
    )
    sha = _run(
        ["git", "rev-parse", "HEAD"], cwd=repo, timeout=30, what="git rev-parse"
    )
    porcelain = _run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=repo,
        timeout=30,
        what="git status",
    )
    return branch, sha, bool(porcelain)


def _validate_ref(ref: str) -> str:
    ref = (ref or "").strip()
    if not ref:
        raise DeployError("missing 'ref'", status=400)
    if not _REF_RE.match(ref) or ".." in ref or "//" in ref or ref.endswith("/"):
        raise DeployError(f"invalid ref {ref!r}", status=400)
    return ref


def _resolve_commit(repo: Path, ref: str, steps: list[str]) -> str:
    """Resolve a branch name or SHA to a concrete commit that exists locally."""
    if _SHA_RE.match(ref):
        target = ref
    else:
        target = f"origin/{ref}"
    try:
        return _run(
            ["git", "rev-parse", "--verify", "--quiet", f"{target}^{{commit}}"],
            cwd=repo,
            timeout=30,
            what=f"resolve {target}",
        )
    except DeployError as exc:
        raise DeployError(
            f"ref {ref!r} not found on origin (push it first)", status=404
        ) from exc


def deploy(
    repo: Path,
    ref: str,
    commit: str | None = None,
    *,
    force: bool = False,
    allow_dirty: bool = False,
    restart_units: tuple[str, ...] | list[str] = (),
    allowed_refs: tuple[str, ...] | list[str] = (),
) -> DeployResult:
    """Fetch ``ref`` from origin, check it out, sync deps and restart units.

    Raises ``DeployError`` on any expected failure. Returns a ``DeployResult``
    whose ``steps`` is the ordered log of what ran.
    """
    repo = Path(repo)
    if not (repo / ".git").exists():
        raise DeployError(f"{repo} is not a git repository", status=500)

    ref = _validate_ref(ref)
    allowlist = [r for r in (allowed_refs or []) if r]
    force = bool(force)
    steps: list[str] = []

    # 1. Refuse to clobber uncommitted work unless explicitly allowed.
    branch, sha, dirty = repo_status(repo)
    steps.append(f"status: branch={branch} head={sha[:12]} dirty={dirty}")
    if dirty and not (force or allow_dirty):
        raise DeployError(
            "working tree is dirty; commit/stash it or pass force=true", status=409
        )

    # 2. Update remote refs, then resolve the target to an immutable SHA.
    steps.append("git fetch --prune origin")
    _run(["git", "fetch", "--prune", "origin"], cwd=repo, timeout=GIT_TIMEOUT, what="git fetch")
    target_sha = _resolve_commit(repo, ref, steps)

    if commit and not target_sha.startswith(commit[:40]):
        raise DeployError(
            f"requested commit {commit[:12]} != origin/{ref} ({target_sha[:12]})",
            status=409,
        )
    if allowlist and not _SHA_RE.match(ref) and ref not in allowlist:
        raise DeployError(f"ref {ref!r} is not in the deploy allowlist", status=403)

    # 3. Check out the target. A branch stays checked out (reset to origin);
    #    a raw SHA checks out detached.
    if _SHA_RE.match(ref):
        steps.append(f"git checkout -f {target_sha[:12]}")
        _run(["git", "checkout", "-f", target_sha], cwd=repo, timeout=GIT_TIMEOUT, what="git checkout")
    else:
        steps.append(f"git checkout -f -B {ref} origin/{ref}")
        _run(
            ["git", "checkout", "-f", "-B", ref, f"origin/{ref}"],
            cwd=repo,
            timeout=GIT_TIMEOUT,
            what="git checkout",
        )

    # 4. Sync dependencies (idempotent; skipped when there is no venv).
    venv_python = repo / ".venv" / "bin" / "python"
    if venv_python.exists():
        steps.append("pip install -r requirements.txt")
        _run(
            [str(venv_python), "-m", "pip", "install", "-q", "-r", "requirements.txt"],
            cwd=repo,
            timeout=PIP_TIMEOUT,
            what="pip install",
        )
    else:
        steps.append("pip install skipped (no .venv)")

    # 5. Restart the services that serve the new code.
    for unit in restart_units or []:
        steps.append(f"systemctl --user restart {unit}")
        _run(
            ["systemctl", "--user", "restart", unit],
            cwd=repo,
            timeout=SYSTEMCTL_TIMEOUT,
            what=f"restart {unit}",
        )

    log.info(f"Deploy | {ref} -> {target_sha[:12]} ok ({len(steps)} steps)")
    return DeployResult(ok=True, ref=ref, commit=target_sha, steps=steps)

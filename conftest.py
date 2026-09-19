"""Pytest root config — ensures the project root is importable and registers markers."""

import sys
from pathlib import Path

_ROOT = str(Path(__file__).parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "slow: end-to-end golden-number checks over the pinned fixture barcache "
        "(minutes, not seconds) — skip with `-m 'not slow'`.",
    )


# Explicit opt-in only: never derive a test DSN from application Settings/.env.
import os
import uuid

import pytest


@pytest.fixture
def editor_store_factory():
    from psycopg import sql
    from core.pattern_edit_store import EditStore
    from core.pattern_editor_db import connection, migrate

    dsn = os.environ.get('PATTERN_EDITOR_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('Set PATTERN_EDITOR_TEST_DATABASE_URL to a disposable PostgreSQL database')
    schema = 'pattern_editor_test_' + uuid.uuid4().hex
    migrate(dsn, schema)

    def factory(root=None):
        kwargs = {'dsn': dsn, 'schema': schema}
        return EditStore(**kwargs) if root is None else EditStore(root, **kwargs)

    factory.dsn = dsn
    factory.schema = schema
    try:
        yield factory
    finally:
        # Generated name only; never clean up the application's pattern_editor schema.
        with connection(dsn, schema) as con:
            con.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


@pytest.fixture
def published_pattern_catalog(editor_store_factory, monkeypatch):
    """Consumer fixture: real storage; parity is tested separately by P03's full gate."""
    from core.pattern_bootstrap import Bootstrap, freeze
    import core.pattern_bootstrap_parity as parity
    store = editor_store_factory()
    snapshot = freeze(store.root, disabled=[])
    with monkeypatch.context() as patch:
        patch.setattr(parity, 'verify_parity', lambda store, version, snapshot:
                      {'version_id': version['version_id'], 'consumer_fixture': True})
        Bootstrap(store).verify_and_publish(Bootstrap(store).stage(snapshot), snapshot)
    monkeypatch.setattr('core.pattern_loader.EditStore', lambda: store)
    monkeypatch.setattr('core.pattern_versions.EditStore', lambda: store)
    return store


class FakeEditProvider:
    """Deterministic provider double; counts calls to prove backtest retry reuse."""

    def __init__(self, *, source_suffix="\n# AI edit\n", raises=None):
        self.calls = 0
        self.raises = raises
        self.source_suffix = source_suffix
        self.model = "test-model"

    def _unavailable(self) -> bool:
        from ai.providers.deepseek import ProviderUnavailable
        return (self.raises is ProviderUnavailable
                or isinstance(self.raises, ProviderUnavailable))

    def available(self):
        return not self._unavailable()

    def balance(self):
        from ai.providers.deepseek import BalanceInfo, ProviderBalance, ProviderUnavailable
        if self._unavailable():
            raise ProviderUnavailable("DeepSeek is not configured")
        return ProviderBalance(is_available=True, infos=(
            BalanceInfo(currency="USD", total_balance="42.00",
                        granted_balance="0.00", topped_up_balance="42.00"),))

    def generate(self, request):
        from ai.providers.deepseek import EditGeneration
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return EditGeneration(
            source=request.source + self.source_suffix,
            documentation=request.documentation + "\n\nAI edit: tightened entry.\n",
            explanation="Tightened the entry rule.",
            requested_model="deepseek-flash", returned_model="deepseek-flash",
            request_id=f"req-{self.calls}")


class InProcessEditRunner:
    """Sandbox double: runs the same trusted evaluator in a plain subprocess."""

    def __init__(self, root):
        from pathlib import Path
        self.root = Path(root)

    def available(self):
        import sys
        return self.root, sys.executable

    def run(self, files, request, cancel=None):
        import json
        import subprocess
        import sys
        import tempfile
        from pathlib import Path
        from core.pattern_edit_store import EditError

        request = dict(request)
        request["test_paths"] = [
            str(self.root / p.removeprefix("/input/"))
            for p in request.get("test_paths", [])]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "input"
            root.mkdir(parents=True)
            for name, data in files.items():
                dest = root / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            driver = (
                "import contextlib, importlib.util, io, json, sys;"
                "spec=importlib.util.spec_from_file_location('ev', sys.argv[1]);"
                "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
                "buf=io.StringIO();ctx=contextlib.redirect_stdout(buf);"
                "ctx.__enter__();res=m.evaluate(json.loads(sys.argv[2]));"
                "ctx.__exit__(None,None,None);"
                "print(json.dumps(res, default=str, allow_nan=False))")
            script = root / "core/pattern_edit_evaluator.py"
            out = subprocess.run(
                [sys.executable, "-B", "-c", driver, str(script), json.dumps(request)],
                cwd=str(root), capture_output=True, text=True, timeout=120)
            if out.returncode:
                tail = (out.stderr or "").strip().splitlines()[-1:] or [""]
                raise EditError(f"candidate worker failed (exit {out.returncode}): {tail[0]}")
            return json.loads(out.stdout)


@pytest.fixture
def edit_doubles():
    """Provider and sandbox doubles for the pattern-edit frontends."""
    return FakeEditProvider, InProcessEditRunner


@pytest.fixture
def sandbox_double(monkeypatch):
    """Route candidate execution through the in-process evaluator double.

    Patching the class (not one imported name) also covers the loader, which
    imports ``Validator`` lazily when it executes a generated version inside a
    stream backtest. The real CandidateRunner is unavailable here; production
    code keeps failing closed and this only substitutes the test double.
    """
    from core.pattern_edit_validation import Validator as RealValidator

    def factory(store=None, runner=None):
        return RealValidator(store, runner=InProcessEditRunner(store.root))

    monkeypatch.setattr("core.pattern_edit_validation.Validator", factory)
    return factory

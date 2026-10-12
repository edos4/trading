"""The sandbox test double supplies fixture config without inheriting credentials."""

from pathlib import Path


def test_edit_runner_uses_isolated_fixture_environment(edit_doubles, monkeypatch):
    monkeypatch.setenv("WATCHLIST", "PARENT_WATCHLIST")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "parent-secret-sentinel")
    monkeypatch.setenv("DATABASE_URL", "parent-database-sentinel")
    root = Path(__file__).resolve().parents[1]
    _, runner_type = edit_doubles
    files = {
        "config.py": (root / "config.py").read_bytes(),
        "core/pattern_edit_evaluator.py": b"""
import os
from config import settings

def evaluate(request):
    return {
        'watchlist': settings.watchlist,
        'screener': settings.tv_screener,
        'exchange': settings.tv_exchange,
        'api_key': settings.deepseek_api_key,
        'database_url': os.environ.get('DATABASE_URL'),
        'blas_threads': os.environ.get('OPENBLAS_NUM_THREADS'),
    }
""",
    }
    result = runner_type(root).run(files, {})
    assert result == {
        "watchlist": "FIXTURE", "screener": "america", "exchange": "NASDAQ",
        "api_key": "", "database_url": None, "blas_threads": "1",
    }

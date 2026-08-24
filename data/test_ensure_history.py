"""Unit checks for stocks_history completeness on web start."""

from __future__ import annotations

from datetime import datetime, timezone

from data.ensure_history import run_ensure_complete
from data.tv_client import OHLCVCandle
from data.update import _candles_to_rows, _fetch_symbol


class _FakeConn:
    def __init__(self) -> None:
        self.upserts: list[tuple[str, list]] = []
        self.refreshed: list[str] = []

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_candles_to_rows_dedupes():
    ts = datetime(2024, 1, 2, tzinfo=timezone.utc)
    c1 = OHLCVCandle(1, 1, 1, 1, 10, ts)
    c2 = OHLCVCandle(2, 2, 2, 2, 20, ts)
    rows = _candles_to_rows([c1, c2])
    assert len(rows) == 1
    assert rows[0][4] == 2.0  # last close wins


def test_fetch_symbol_fill_all_upserts_entire_series(monkeypatch):
    conn = _FakeConn()
    ts = datetime(2024, 6, 3, tzinfo=timezone.utc)
    candles = [
        OHLCVCandle(10, 11, 9, 10.5, 100, ts),
        OHLCVCandle(10.5, 12, 10, 11, 110, datetime(2024, 6, 4, tzinfo=timezone.utc)),
    ]

    monkeypatch.setattr(
        "data.tv_client.fetch_yahoo_daily_max",
        lambda symbol: candles,
    )

    def upsert(conn_, symbol, rows):
        conn_.upserts.append((symbol, list(rows)))

    def refresh(conn_, symbol):
        conn_.refreshed.append(symbol)

    monkeypatch.setattr("data.db.upsert_bars", upsert)
    monkeypatch.setattr("data.db.refresh_symbol_meta", refresh)

    n = _fetch_symbol(conn, "AAPL", "us", fill_all=True)
    assert n == 2
    assert conn.upserts[0][0] == "AAPL"
    assert len(conn.upserts[0][1]) == 2
    assert conn.refreshed == ["AAPL"]


def test_run_ensure_complete_empty_db(monkeypatch):
    monkeypatch.setattr("data.ensure_history.db.get_conn", lambda: _FakeConn())
    monkeypatch.setattr("data.ensure_history.db.ensure_schema", lambda conn: None)
    monkeypatch.setattr("data.ensure_history.db.all_symbols", lambda conn: [])
    out = run_ensure_complete()
    assert out == {"symbols": 0, "incomplete": 0, "fetched": 0, "upserted_bars": 0}


def test_run_ensure_complete_fetches_stale(monkeypatch):
    conn = _FakeConn()
    stale = {
        "symbol": "MSFT",
        "market": "us",
        "last_bar_ts": 0,
        "row_count": 1,
        "source_path": None,
        "letter": "M",
        "file_mtime": None,
        "file_size": None,
    }
    monkeypatch.setattr("data.ensure_history.db.get_conn", lambda: conn)
    monkeypatch.setattr("data.ensure_history.db.ensure_schema", lambda c: None)
    monkeypatch.setattr("data.ensure_history.db.all_symbols", lambda c: [stale])
    monkeypatch.setattr(
        "data.ensure_history._fetch_symbol",
        lambda c, symbol, market, fill_all=False: 12 if fill_all else 0,
    )
    out = run_ensure_complete()
    assert out["incomplete"] == 1
    assert out["fetched"] == 1
    assert out["upserted_bars"] == 12


def test_run_ensure_complete_skip_fetch(monkeypatch):
    conn = _FakeConn()
    stale = {
        "symbol": "MSFT",
        "market": "us",
        "last_bar_ts": 0,
        "row_count": 1,
        "source_path": None,
        "letter": "M",
        "file_mtime": None,
        "file_size": None,
    }
    monkeypatch.setattr("data.ensure_history.db.get_conn", lambda: conn)
    monkeypatch.setattr("data.ensure_history.db.ensure_schema", lambda c: None)
    monkeypatch.setattr("data.ensure_history.db.all_symbols", lambda c: [stale])

    def boom(*_a, **_k):
        raise AssertionError("Yahoo fetch must not run when fetch=False")

    monkeypatch.setattr("data.ensure_history._fetch_symbol", boom)
    out = run_ensure_complete(fetch=False)
    assert out["incomplete"] == 1
    assert out["fetched"] == 0
    assert out["upserted_bars"] == 0


def test_start_web_history_backfill_pings_only(monkeypatch):
    import data.ensure_history as eh

    eh._started = False
    calls: list[str] = []
    monkeypatch.setattr(eh, "ping_db", lambda: calls.append("ping"))
    monkeypatch.setattr(
        eh, "run_ensure_complete", lambda **_k: calls.append("ensure"),
    )
    eh.start_web_history_backfill()
    assert calls == ["ping"]

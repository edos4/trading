"""Kronos forecast store: directional scoring and PostgreSQL persistence."""

from __future__ import annotations

from core.kronos_store import directional_hit


def _payload(days: int = 3) -> dict:
    base = [249.0, 251.0, 247.0][:days]
    fine = [247.5, 252.0, 251.0][:days]
    dates = ["2026-10-15", "2026-10-16", "2026-10-19"][:days]
    return {
        "days": days,
        "lookback": 400,
        "symbols": ["AAPL"],
        "results": [{
            "symbol": "AAPL",
            "days": days,
            "lookback": 400,
            "anchor": {"time": "2026-10-14", "value": 250.0},
            "history": [{"time": "2026-10-13", "value": 245.0}],
            "base": [{"time": d, "value": v} for d, v in zip(dates, base)],
            "finetuned": [{"time": d, "value": v} for d, v in zip(dates, fine)],
        }],
    }


def _bars(_symbol: str, _since):
    return [
        ("2026-10-15", 252.0),
        ("2026-10-16", 248.0),
        ("2026-10-19", 260.0),
    ]


def test_kronos_symbols_normalizes_and_bounds_fallback() -> None:
    from config import Settings

    explicit = Settings(
        watchlist="AAPL,MSFT", tv_screener="america", tv_exchange="NASDAQ",
        kronos_tracked_symbols="aapl, msft ,nvda",
    )
    assert explicit.kronos_symbols == ["AAPL", "MSFT", "NVDA"]

    fallback = Settings(
        watchlist=",".join(f"S{i}" for i in range(150)),
        tv_screener="america", tv_exchange="NASDAQ",
    )
    assert len(fallback.kronos_symbols) == 100


def test_directional_hit_signs() -> None:
    assert directional_hit(100.0, 101.0, 102.0) is True
    assert directional_hit(100.0, 101.0, 98.0) is False
    assert directional_hit(100.0, 99.0, 98.0) is True
    assert directional_hit(100.0, 100.0, 101.0) is False
    assert directional_hit(100.0, 101.0, 100.0) is False
    assert directional_hit(100.0, 100.0, 100.0) is True


def test_save_is_idempotent_and_reports_summary(kronos_store_factory) -> None:
    store = kronos_store_factory()
    payload = _payload()

    first = store.save_payload(payload, saved_by="admin")
    assert first["predictions"] == 6
    assert first["symbols"] == ["AAPL"]
    assert first["anchor_dates"] == ["2026-10-14"]

    second = store.save_payload(payload, saved_by="admin")
    assert second["predictions"] == 6

    stats = store.stats()
    assert stats["saved"]["predictions"] == 6
    assert stats["saved"]["symbols"] == 1
    assert stats["pending"] == 6
    assert stats["graded"] == 0


def test_recent_returns_saved_rows(kronos_store_factory) -> None:
    store = kronos_store_factory()
    store.save_payload(_payload(), saved_by="admin")

    data = store.recent(limit=3)

    assert data["total"] == 6
    assert data["limit"] == 3
    assert len(data["rows"]) == 3
    row = data["rows"][0]
    assert row["symbol"] == "AAPL"
    assert row["saved_by"] == "admin"
    assert isinstance(row["anchor_date"], str)
    assert isinstance(row["forecast_date"], str)
    assert row["actual_close"] is None
    assert row["correct"] is None


def test_grade_updates_scores_per_day(kronos_store_factory) -> None:
    store = kronos_store_factory()
    store.save_payload(_payload(), saved_by="admin")

    assert store.grade(bars_provider=_bars) == 6
    assert store.grade(bars_provider=_bars) == 0

    stats = store.stats()
    assert stats["graded"] == 6
    assert stats["pending"] == 0

    base = stats["models"]["base"]
    assert base["overall"] == {"wins": 0, "n": 3, "win_rate_pct": 0.0}
    assert [day["win_rate_pct"] for day in base["days"]] == [0.0, 0.0, 0.0]

    fine = stats["models"]["finetuned"]
    assert fine["overall"] == {"wins": 1, "n": 3, "win_rate_pct": 33.3}
    assert [day["win_rate_pct"] for day in fine["days"]] == [0.0, 0.0, 100.0]


def test_grade_leaves_insufficient_horizons_pending(kronos_store_factory) -> None:
    store = kronos_store_factory()
    store.save_payload(_payload(), saved_by="admin")

    assert store.grade(bars_provider=lambda _s, _d: [("2026-10-15", 252.0)]) == 2
    stats = store.stats()
    assert stats["graded"] == 2
    assert stats["pending"] == 4


def test_save_skips_symbols_without_anchor_or_series(kronos_store_factory) -> None:
    store = kronos_store_factory()
    payload = _payload()
    payload["results"].append({"symbol": "NVDA", "errors": {"data": "only 10 bars"}})

    saved = store.save_payload(payload, saved_by="admin")

    assert saved["symbols"] == ["AAPL"]
    assert saved["skipped"] == ["NVDA"]
    assert saved["predictions"] == 6

"""Experiment 2 store: percentage accuracy and PostgreSQL persistence."""

from __future__ import annotations

from core.kronos_experiment import KronosExperimentStore, accuracy_pct


def _payload(days: int = 5) -> dict:
    base = [390.0, 387.0, 384.0, 381.0, 400.0][:days]
    fine = [382.0, 381.0, 380.0, 379.0, 376.0][:days]
    dates = ["2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20",
             "2026-10-21"][:days]
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
        ("2026-10-15", 380.0),
        ("2026-10-16", 381.0),
        ("2026-10-19", 382.0),
        ("2026-10-20", 383.0),
        ("2026-10-21", 375.0),
    ]


def _experiment(store) -> KronosExperimentStore:
    return KronosExperimentStore(dsn=store.dsn, schema=store.schema)


def test_accuracy_matches_the_experiment_spreadsheet() -> None:
    assert round(accuracy_pct(390.0, 380.0), 2) == 97.37
    assert round(accuracy_pct(382.0, 380.0), 2) == 99.47
    assert round(accuracy_pct(400.0, 375.0), 2) == 93.33
    assert round(accuracy_pct(376.0, 375.0), 2) == 99.73
    assert accuracy_pct(380.0, 380.0) == 100.0
    assert accuracy_pct(None, 380.0) is None
    assert accuracy_pct(390.0, None) is None
    assert accuracy_pct(390.0, 0.0) is None


def test_save_keeps_only_d1_and_d5(kronos_store_factory) -> None:
    store = _experiment(kronos_store_factory())
    payload = _payload()

    saved = store.save_payload(payload, saved_by="admin")

    assert saved["predictions"] == 2
    assert saved["horizons"] == [1, 5]
    assert saved["symbols"] == ["AAPL"]
    assert saved["anchor_dates"] == ["2026-10-14"]

    data = store.recent(limit=10)
    assert data["total"] == 2
    assert [row["day_ahead"] for row in data["rows"]] == [1, 5]
    assert data["rows"][0]["base_close"] == 390.0
    assert data["rows"][0]["finetuned_close"] == 382.0
    assert data["rows"][1]["base_close"] == 400.0


def test_save_is_idempotent(kronos_store_factory) -> None:
    store = _experiment(kronos_store_factory())
    assert store.save_payload(_payload(), saved_by="admin")["predictions"] == 2
    assert store.save_payload(_payload(), saved_by="admin")["predictions"] == 2
    assert store.recent(limit=10)["total"] == 2


def test_grade_scores_both_models(kronos_store_factory) -> None:
    store = _experiment(kronos_store_factory())
    store.save_payload(_payload(), saved_by="admin")

    assert store.grade(bars_provider=_bars) == 2
    assert store.grade(bars_provider=_bars) == 0

    summary = store.summary()
    assert summary["graded"] == 2
    assert summary["pending"] == 0
    assert summary["saved"]["last_graded_anchor"] == "2026-10-14"
    assert summary["saved"]["last_graded_at"] is not None
    # Averages stay split per horizon: D1 forecasts 390/382 against a 380
    # settle, D5 forecasts 400/376 against a 375 settle. Blending them into a
    # single 95.35 / 99.60 line is exactly what hid that split.
    horizons = summary["horizons"]
    assert sorted(horizons) == ["1", "5"]
    assert horizons["1"]["base"]["graded"] == 1
    assert round(horizons["1"]["base"]["avg_accuracy_pct"], 2) == 97.37
    assert horizons["1"]["finetuned"]["graded"] == 1
    assert round(horizons["1"]["finetuned"]["avg_accuracy_pct"], 2) == 99.47
    assert horizons["5"]["base"]["graded"] == 1
    assert round(horizons["5"]["base"]["avg_accuracy_pct"], 2) == 93.33
    assert horizons["5"]["finetuned"]["graded"] == 1
    assert round(horizons["5"]["finetuned"]["avg_accuracy_pct"], 2) == 99.73

    rows = store.recent(limit=10)["rows"]
    assert rows[0]["actual_close"] == 380.0
    assert round(rows[0]["base_accuracy_pct"], 2) == 97.37
    assert round(rows[0]["finetuned_accuracy_pct"], 2) == 99.47
    assert round(rows[1]["base_accuracy_pct"], 2) == 93.33
    assert round(rows[1]["finetuned_accuracy_pct"], 2) == 99.73


def test_recent_lists_settled_rows_before_pending(kronos_store_factory) -> None:
    store = _experiment(kronos_store_factory())
    store.save_payload(_payload(), saved_by="admin")
    store.grade(bars_provider=_bars)

    newer = _payload()
    newer["results"][0]["anchor"] = {"time": "2026-10-21", "value": 344.0}
    for model in ("base", "finetuned"):
        for index, point in enumerate(newer["results"][0][model]):
            point["time"] = f"2026-10-{22 + index}"
    store.save_payload(newer, saved_by="admin")

    rows = store.recent(limit=10)["rows"]
    assert rows[0]["anchor_date"] == "2026-10-14"
    assert rows[0]["actual_close"] is not None
    assert rows[-1]["anchor_date"] == "2026-10-21"
    assert rows[-1]["actual_close"] is None


def test_grade_leaves_missing_horizons_pending(kronos_store_factory) -> None:
    store = _experiment(kronos_store_factory())
    store.save_payload(_payload(), saved_by="admin")

    assert store.grade(bars_provider=lambda _s, _d: [
        ("2026-10-15", 380.0)]) == 1
    assert store.summary()["pending"] == 1


def test_resaving_clears_grading(kronos_store_factory) -> None:
    store = _experiment(kronos_store_factory())
    store.save_payload(_payload(), saved_by="admin")
    store.grade(bars_provider=_bars)

    store.save_payload(_payload(), saved_by="admin")

    summary = store.summary()
    assert summary["graded"] == 0
    assert summary["pending"] == 2
    # Both horizons stay present so the panel never hides +1D or +5D.
    assert sorted(summary["horizons"]) == ["1", "5"]
    for entry in summary["horizons"].values():
        assert entry["base"] == {"graded": 0, "avg_accuracy_pct": None}
        assert entry["finetuned"] == {"graded": 0, "avg_accuracy_pct": None}

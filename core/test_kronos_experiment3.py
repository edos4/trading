"""Frozen experiment identity, causal inputs, bounded collection, and persistence."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest

from core import kronos_experiment3 as exp
from scripts.kronos_experiment3 import BEGIN, END, cron_block, merge_cron


def at(value: str) -> datetime:
    return datetime.fromisoformat(value)


@pytest.mark.parametrize("now,expected", [
    ("2026-11-11T16:29:59-05:00", []),
    ("2026-11-11T16:30:00-05:00", [exp.TARGETS[0]]),
    ("2026-11-12T08:00:00+08:00", [exp.TARGETS[0]]),
    ("2026-11-12T16:30:00-05:00", list(exp.TARGETS)),
    ("2026-11-13T09:29:59-05:00", list(exp.TARGETS)),
    ("2026-11-13T09:30:00-05:00", []),
    ("2027-11-11T17:00:00-05:00", []),
])
def test_only_two_closed_sessions_are_ever_due(now, expected):
    assert exp.due_targets(at(now)) == expected


def test_screener_refuses_a_later_live_universe():
    with pytest.raises(ValueError, match="window has closed"):
        exp.capture_universe(at("2026-11-11T09:30:00-05:00"))


def test_universe_is_the_frozen_fifty_nasdaq_stocks():
    snapshot = json.loads(exp.UNIVERSE_FILE.read_text())
    exp.validate_universe(snapshot)
    for mutate in (
        lambda s: s["stocks"].pop(),
        lambda s: s["stocks"][0].update(exchange="NYSE"),
        lambda s: s["stocks"][0].update(type="fund"),
        lambda s: s["stocks"][0].update(dollar_volume=float("nan")),
        lambda s: s.update(captured_at="2026-11-12T01:00:00-05:00"),
    ):
        broken = deepcopy(snapshot)
        mutate(broken)
        with pytest.raises(ValueError):
            exp.validate_universe(broken)


def result(symbol="TEST", rank=1):
    return {"symbol": symbol, "rank": rank, "days": 2,
            "anchor": {"time": "2026-11-10", "value": 100.},
            "history": [{"time": "2026-11-10", "value": 100.}],
            "base": [{"time": "2026-11-11", "value": 110.}, {"time": "2026-11-12", "value": 90.}],
            "finetuned": [{"time": "2026-11-11", "value": 95.}, {"time": "2026-11-12", "value": 100.}],
            "actuals": []}


def test_scoring_uses_only_matching_observations_and_horizons():
    row = result()
    row["actuals"] = [{"time": "2026-11-11", "value": 110.}]
    scores = exp.score([row, result("MISSING")])
    assert scores["base"][0]["count"] == 1
    assert scores["base"][0]["mape_pct"] == 0
    assert scores["base"][0]["direction_accuracy_pct"] == 100
    assert scores["base"][0]["direction_correct"] == 1
    assert scores["finetuned"][0]["direction_accuracy_pct"] == 0
    assert scores["base"][1]["count"] == 0
    assert scores["base"][1]["mape_pct"] is None


@pytest.mark.parametrize("value,anchor,expected", [
    (101., 100., "up"), (99., 100., "down"), (100., 100., "flat"),
    (129.66, 129.66000366210938, "flat"),
    (100.0049, 100., "flat"), (100.005, 100., "up"),
])
def test_direction_compares_usd_cents(value, anchor, expected):
    assert exp.direction(value, anchor) == expected


@pytest.mark.parametrize("predicted,actual,correct", [
    (101., 110., True), (99., 110., False),
    (99., 90., True), (101., 90., False),
    (100., 100., True), (101., 100., False), (100., 110., False),
])
def test_direction_outcomes_are_separate_from_price_error(predicted, actual, correct):
    row = result()
    row["base"][0]["value"] = predicted
    row["actuals"] = [{"time": "2026-11-11", "value": actual}]
    assessment = exp.direction_results(row)
    assert assessment[0]["base"]["correct"] is correct
    assert assessment[1]["actual"] is None
    assert assessment[1]["base"]["correct"] is None
    score = exp.score([row])["base"][0]
    assert score["direction_correct"] == int(correct)
    assert score["direction_accuracy_pct"] == (100 if correct else 0)


def test_second_day_direction_is_cumulative_from_anchor():
    row = result()
    row["base"][1]["value"] = 104.
    row["actuals"] = [{"time": "2026-11-11", "value": 110.},
                      {"time": "2026-11-12", "value": 105.}]
    # Actual fell on day two but is still UP from the November 10 anchor of 100.
    assessment = exp.direction_results(row)[1]
    assert assessment["actual"] == "up"
    assert assessment["base"] == {"predicted": "up", "correct": True}


def test_daily_report_counts_each_date_and_model_separately():
    rows = [result(f"S{i}", i + 1) for i in range(50)]
    for i, row in enumerate(rows):
        row["actuals"] = [{"time": "2026-11-11", "value": 110. if i < 32 else 90.},
                          {"time": "2026-11-12", "value": 90. if i < 27 else 100.}]
    experiment = {"status": "complete", "results": rows, "scores": exp.score(rows), "actual_count": 100}
    report = exp.direction_report_rows(experiment)
    assert [(row["date"], row["evaluated"], row["pending"]) for row in report] == [
        ("2026-11-11", 50, 0), ("2026-11-12", 50, 0)]
    assert report[0]["base"] == {"correct": 32, "incorrect": 18, "accuracy_pct": 64.}
    assert report[0]["finetuned"] == {"correct": 18, "incorrect": 32, "accuracy_pct": 36.}
    assert report[1]["base"] == {"correct": 27, "incorrect": 23, "accuracy_pct": 54.}
    assert report[1]["finetuned"] == {"correct": 23, "incorrect": 27, "accuracy_pct": 46.}
    markdown = exp.render_direction_report(experiment)
    assert "| 2026-11-11 | +1D | 50/50 | 0 | 32 | 18 | 64.0% | 18 | 32 | 36.0% |" in markdown
    assert "| 2026-11-12 | +2D | 50/50 | 0 | 27 | 23 | 54.0% | 23 | 27 | 46.0% |" in markdown


def test_daily_report_excludes_missing_stocks_and_leaves_unobserved_day_pending(tmp_path):
    rows = [result(f"S{i}", i + 1) for i in range(50)]
    rows[0]["actuals"] = [{"time": "2026-11-11", "value": 110.}]
    experiment = {"status": "collecting", "results": rows, "scores": exp.score(rows), "actual_count": 1}
    report = exp.direction_report_rows(experiment)
    assert report[0]["evaluated"] == 1
    assert report[0]["pending"] == 49
    assert report[0]["base"]["accuracy_pct"] == 100.
    assert report[1]["evaluated"] == 0
    assert report[1]["pending"] == 50
    assert report[1]["base"]["accuracy_pct"] is None
    path = exp.save_direction_report(experiment, tmp_path / "report.md")
    assert "| 2026-11-12 | +2D | 0/50 | 50 | Pending | Pending | Pending | Pending | Pending | Pending |" in path.read_text()
    original_mtime = path.stat().st_mtime_ns
    exp.save_direction_report(experiment, path)
    assert path.stat().st_mtime_ns == original_mtime


def test_daily_report_without_forecasts_does_not_claim_accuracy():
    experiment = {"status": "not_started", "results": []}
    assert exp.direction_report_rows(experiment) == []
    assert "No saved forecasts yet" in exp.render_direction_report(experiment)


def test_partial_wrong_date_or_nonfinite_forecasts_cannot_be_saved():
    rows = [result(f"S{i}", i + 1) for i in range(50)]
    inputs = [{"symbol": r["symbol"], "bars": [{"close": 100.}]} for r in rows]
    exp.validate_predictions({"results": rows}, inputs)
    for mutate in (
        lambda rows: rows.pop(),
        lambda rows: rows[0]["base"][0].update(time="2026-11-13"),
        lambda rows: rows[0]["base"][0].update(value=float("nan")),
        lambda rows: rows[0]["anchor"].update(time="2026-11-09"),
        lambda rows: rows[0]["history"].append({"time": "2026-11-11", "value": 100.}),
    ):
        broken = deepcopy(rows)
        mutate(broken)
        with pytest.raises(ValueError):
            exp.validate_predictions({"results": broken}, inputs)


def test_saved_forecasts_are_never_regenerated(monkeypatch):
    frozen = {"status": "collecting", "results": [result()]}
    monkeypatch.setattr(exp, "read_experiment", lambda now: frozen)
    load = Mock(side_effect=AssertionError("must not fetch new inputs"))
    monkeypatch.setattr(exp, "load_inputs", load)
    assert exp.start(at("2026-12-01T10:00:00-05:00")) == frozen
    assert not load.called


def test_late_forecast_creation_is_forbidden(monkeypatch):
    monkeypatch.setattr(exp, "read_experiment", lambda now: {"status": "not_started"})
    with pytest.raises(ValueError, match="creation closed"):
        exp.start(at("2026-11-11T09:30:00-05:00"))


def test_cron_install_remove_preserves_other_jobs_and_is_idempotent():
    other = "TZ=America/New_York\n*/15 * * * 1-5 existing-update\n20 * * * * hourly\n"
    installed = merge_cron(other, cron_block(Path("/tmp/project with spaces")))
    assert installed.startswith(other)
    assert installed.count(BEGIN) == installed.count(END) == 1
    assert merge_cron(installed, cron_block(Path("/tmp/project with spaces"))) == installed
    assert merge_cron(installed) == other
    with pytest.raises(ValueError, match="Malformed"):
        merge_cron(other + BEGIN)


@pytest.fixture
def store(monkeypatch):
    """Use the real SQL in a disposable schema; never touch the live experiment."""
    from data.db import get_conn
    try:
        connection = get_conn()
    except Exception as exc:
        pytest.skip(f"Local PostgreSQL unavailable: {type(exc).__name__}")
    schema = "kronos_experiment3_test_" + uuid4().hex

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def __enter__(self):
            self.cursor.__enter__()
            return self

        def __exit__(self, *args):
            return self.cursor.__exit__(*args)

        def execute(self, query, params=None):
            return self.cursor.execute(query.replace("kronos_experiment3", schema), params)

        def fetchone(self):
            return self.cursor.fetchone()

        def fetchall(self):
            return self.cursor.fetchall()

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            if args[0]:
                connection.rollback()
            else:
                connection.commit()

        def cursor(self):
            return Cursor(connection.cursor())

    wrapped = Connection()
    monkeypatch.setattr(exp, "get_conn", lambda: wrapped)
    try:
        yield wrapped
    finally:
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        connection.commit()
        connection.close()


def test_database_collects_once_per_date_never_rewrites_predictions(store, monkeypatch):
    from psycopg.types.json import Jsonb

    with store as conn, conn.cursor() as cur:
        cur.execute(exp.SCHEMA_SQL)
        cur.execute("INSERT INTO kronos_experiment3.run (id,metadata) VALUES (1,%s)",
                    (Jsonb({"generated_at": "2026-11-10T23:00:00+00:00"}),))
        cur.execute("INSERT INTO kronos_experiment3.forecasts VALUES ('TEST',1,%s)", (Jsonb(result()),))
    monkeypatch.setattr(exp, "provider_bars", lambda symbol: [
        {"time": "2026-11-10", "close": 999.},
        {"time": "2026-11-11", "close": 110.},
        {"time": "2026-11-12", "close": 90.},
        {"time": "2026-11-13", "close": 999.},
    ])
    first = exp.collect(at("2026-11-11T16:30:00-05:00"))
    assert first["actual_count"] == 1
    assert first["results"][0]["actuals"][0]["value"] == 110.
    monkeypatch.setattr(exp, "provider_bars", lambda symbol: [
        {"time": "2026-11-11", "close": 999.}, {"time": "2026-11-12", "close": 90.},
    ])
    second = exp.collect(at("2026-11-12T16:30:00-05:00"))
    assert second["actual_count"] == 2
    assert [p["value"] for p in second["results"][0]["actuals"]] == [110., 90.]
    assert second["results"][0]["base"] == result()["base"]
    assert exp.collect(at("2026-11-13T09:30:00-05:00"))["status"] == "expired_incomplete"


def test_database_empty_state_does_not_create_schema(store):
    assert exp.read_experiment()["status"] == "not_started"


def test_expired_or_preclose_collection_never_fetches(store, monkeypatch):
    from psycopg.types.json import Jsonb
    with store as conn, conn.cursor() as cur:
        cur.execute(exp.SCHEMA_SQL)
        cur.execute("INSERT INTO kronos_experiment3.run VALUES (1,now(),%s)", (Jsonb({}),))
    fetch = Mock(side_effect=AssertionError("must not fetch outside window"))
    monkeypatch.setattr(exp, "provider_bars", fetch)
    exp.collect(at("2026-11-11T15:00:00-05:00"))
    exp.collect(at("2026-11-13T10:00:00-05:00"))
    assert not fetch.called


def test_complete_database_stops_fetching_and_keeps_saved_actuals(store, monkeypatch):
    from psycopg.types.json import Jsonb
    with store as conn, conn.cursor() as cur:
        cur.execute(exp.SCHEMA_SQL)
        cur.execute("INSERT INTO kronos_experiment3.run VALUES (1,now(),%s)", (Jsonb({}),))
        for i in range(50):
            symbol = f"S{i}"
            cur.execute("INSERT INTO kronos_experiment3.forecasts VALUES (%s,%s,%s)",
                        (symbol, i + 1, Jsonb(result(symbol, i + 1))))
            for day in exp.TARGETS:
                cur.execute("INSERT INTO kronos_experiment3.actuals "
                            "(symbol,session_date,close,source) VALUES (%s,%s,101,'test')", (symbol, day))
    fetch = Mock(side_effect=AssertionError("complete experiment must not fetch"))
    monkeypatch.setattr(exp, "provider_bars", fetch)
    saved = exp.collect(at("2026-11-12T17:00:00-05:00"))
    assert saved["status"] == "complete"
    assert saved["actual_count"] == 100
    assert saved["scores"]["base"][1]["count"] == 50
    assert not fetch.called

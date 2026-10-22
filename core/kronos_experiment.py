"""Experiment 2: daily D1/D5 close forecasts scored by percentage accuracy.

One row per symbol x anchor date x horizon (day 1 and day 5). Both models share
the row. A row is graded once the settlement bar exists, using the same
positional (trading-bar) alignment as ``core.kronos_store``:

    accuracy_pct = 100 * (1 - abs(predicted_close - actual_close) / actual_close)

Unlike the directional win rate, this measures how close the predicted close
was to the settled close.
"""
from __future__ import annotations

from bisect import bisect_right
from contextlib import contextmanager
from datetime import date
from typing import Callable, Iterable

from core.kronos_db import KronosError, connection, verify_migrations

HORIZONS = (1, 5)
MODELS = ("base", "finetuned")

_UPSERT_SQL = """
INSERT INTO kronos_experiment2 (
    symbol, anchor_date, anchor_close, day_ahead, forecast_date,
    base_close, finetuned_close, saved_by)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (symbol, anchor_date, day_ahead) DO UPDATE SET
    anchor_close = EXCLUDED.anchor_close,
    forecast_date = EXCLUDED.forecast_date,
    base_close = EXCLUDED.base_close,
    finetuned_close = EXCLUDED.finetuned_close,
    saved_by = EXCLUDED.saved_by,
    saved_at = clock_timestamp(),
    actual_date = NULL,
    actual_close = NULL,
    base_accuracy_pct = NULL,
    finetuned_accuracy_pct = NULL,
    graded_at = NULL
"""

_GRADE_SQL = """
UPDATE kronos_experiment2
SET actual_date = %s, actual_close = %s,
    base_accuracy_pct = %s, finetuned_accuracy_pct = %s,
    graded_at = clock_timestamp()
WHERE symbol = %s AND anchor_date = %s AND day_ahead = %s
"""


def accuracy_pct(predicted_close: float | None, actual_close: float | None) -> float | None:
    """Percentage closeness: 100 means an exact close, 0 a 100% miss."""
    if predicted_close is None or actual_close is None or actual_close == 0:
        return None
    return 100.0 * (1.0 - abs(float(predicted_close) - float(actual_close))
                    / abs(float(actual_close)))


def _as_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _series(result: dict, model: str) -> list[dict]:
    rows = result.get(model)
    return rows if isinstance(rows, list) else []


def _point(rows: list[dict], day_ahead: int) -> dict | None:
    if len(rows) < day_ahead:
        return None
    point = rows[day_ahead - 1]
    if not isinstance(point, dict):
        return None
    if point.get("time") is None or point.get("value") is None:
        return None
    return point


class KronosExperimentStore:
    def __init__(self, dsn: str | None = None, schema: str = "kronos",
                 history_dsn: str | None = None):
        from config import settings
        from data.db import build_dsn

        self.dsn = dsn or settings.kronos_database_url or build_dsn()
        self.schema = schema
        self.history_dsn = history_dsn

    @contextmanager
    def connect(self):
        with connection(self.dsn, self.schema) as con:
            verify_migrations(con)
            yield con

    def save_payload(self, payload: dict, *, saved_by: str) -> dict:
        """Upsert the D1/D5 points from a ``scripts/kronos_web_forecast.py`` payload."""
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise KronosError("Forecast payload has no results")
        author = (saved_by or "").strip() or "web"
        rows: list[tuple] = []
        symbols: list[str] = []
        anchors: list[str] = []
        skipped: list[str] = []
        for result in payload["results"]:
            if not isinstance(result, dict):
                continue
            symbol = str(result.get("symbol") or "").strip().upper()
            anchor = result.get("anchor") or {}
            anchor_date = anchor.get("time")
            anchor_close = anchor.get("value")
            if not symbol or not anchor_date or anchor_close is None:
                if symbol:
                    skipped.append(symbol)
                continue
            series = {model: _series(result, model) for model in MODELS}
            added = 0
            for day_ahead in HORIZONS:
                points = {model: _point(series[model], day_ahead) for model in MODELS}
                if not any(points.values()):
                    continue
                forecast = points["base"] or points["finetuned"]
                rows.append((
                    symbol, _as_date(anchor_date), float(anchor_close), day_ahead,
                    _as_date(forecast["time"]),
                    float(points["base"]["value"]) if points["base"] else None,
                    float(points["finetuned"]["value"]) if points["finetuned"] else None,
                    author,
                ))
                added += 1
            if not added:
                skipped.append(symbol)
                continue
            if symbol not in symbols:
                symbols.append(symbol)
            if str(anchor_date) not in anchors:
                anchors.append(str(anchor_date))
        if rows:
            with self.connect() as con, con.cursor() as cur:
                cur.executemany(_UPSERT_SQL, rows)
        return {"predictions": len(rows), "symbols": symbols,
                "anchor_dates": anchors, "horizons": list(HORIZONS),
                "skipped": skipped}

    def grade(self, bars_provider: Callable[[str, date], Iterable[tuple]] | None = None) -> int:
        """Grade pending rows against settled bars; return rows updated.

        ``bars_provider(symbol, since)`` returns ``(bar_date, close)`` pairs on or
        after ``since``. Injectable so tests never need ``daily_bars``.
        """
        provider = bars_provider or self._history_bars
        with self.connect() as con:
            pending = con.execute(
                "SELECT symbol, anchor_date, day_ahead, base_close, finetuned_close "
                "FROM kronos_experiment2 WHERE actual_close IS NULL "
                "ORDER BY symbol, anchor_date, day_ahead"
            ).fetchall()
            if not pending:
                return 0
            since: dict[str, date] = {}
            for row in pending:
                symbol = row["symbol"]
                current = since.get(symbol)
                anchor = row["anchor_date"]
                since[symbol] = anchor if current is None else min(current, anchor)
            bars: dict[str, list[tuple[date, float]]] = {}
            for symbol, start in since.items():
                pairs = [(_as_date(day), float(close)) for day, close in provider(symbol, start)]
                bars[symbol] = sorted(pairs, key=lambda pair: pair[0])
            updates = []
            for row in pending:
                series = bars.get(row["symbol"], [])
                days = [pair[0] for pair in series]
                start = bisect_right(days, row["anchor_date"])
                target = start + int(row["day_ahead"]) - 1
                if target >= len(series):
                    continue
                actual_date, actual_close = series[target]
                updates.append((
                    actual_date, actual_close,
                    accuracy_pct(row["base_close"], actual_close),
                    accuracy_pct(row["finetuned_close"], actual_close),
                    row["symbol"], row["anchor_date"], row["day_ahead"],
                ))
            if updates:
                with con.cursor() as cur:
                    cur.executemany(_GRADE_SQL, updates)
            return len(updates)

    def recent(self, limit: int = 500) -> dict:
        """Recent Experiment 2 rows: settled rows first, then newest anchor."""
        limit = max(1, min(int(limit), 2000))
        with self.connect() as con:
            rows = con.execute(
                "SELECT symbol, anchor_date, anchor_close, day_ahead, forecast_date, "
                "base_close, finetuned_close, actual_date, actual_close, "
                "base_accuracy_pct, finetuned_accuracy_pct, saved_by, saved_at, graded_at "
                "FROM kronos_experiment2 "
                "ORDER BY (actual_close IS NULL), anchor_date DESC, symbol, day_ahead "
                "LIMIT %s",
                (limit,),
            ).fetchall()
            total = con.execute(
                "SELECT COUNT(*) AS n FROM kronos_experiment2").fetchone()["n"]
        out = []
        for row in rows:
            item = dict(row)
            for key in ("anchor_date", "forecast_date", "actual_date"):
                if item.get(key) is not None:
                    item[key] = item[key].isoformat()
            for key in ("saved_at", "graded_at"):
                if item.get(key) is not None:
                    item[key] = item[key].isoformat()
            out.append(item)
        return {"rows": out, "total": int(total), "limit": limit}

    def summary(self) -> dict:
        """Aggregate Experiment 2 accuracy for the section header."""
        with self.connect() as con:
            row = con.execute(
                "SELECT COUNT(*) AS total, "
                "       COUNT(*) FILTER (WHERE actual_close IS NOT NULL) AS graded, "
                "       COUNT(DISTINCT symbol) AS symbols, "
                "       MIN(anchor_date) AS first_anchor, "
                "       MAX(anchor_date) AS last_anchor, "
                "       MAX(anchor_date) FILTER (WHERE actual_close IS NOT NULL) "
                "           AS last_graded_anchor, "
                "       MAX(graded_at) AS last_graded_at, "
                "       MAX(saved_at) AS last_saved_at, "
                "       AVG(base_accuracy_pct) AS base_avg, "
                "       COUNT(base_accuracy_pct) AS base_n, "
                "       AVG(finetuned_accuracy_pct) AS fine_avg, "
                "       COUNT(finetuned_accuracy_pct) AS fine_n "
                "FROM kronos_experiment2"
            ).fetchone()
        total = int(row["total"] or 0)
        graded = int(row["graded"] or 0)

        def rate(value):
            return round(float(value), 2) if value is not None else None

        last_saved = row["last_saved_at"]
        last_graded = row["last_graded_at"]
        return {
            "saved": {
                "predictions": total,
                "symbols": int(row["symbols"] or 0),
                "first_anchor": (row["first_anchor"].isoformat()
                                 if row["first_anchor"] else None),
                "last_anchor": (row["last_anchor"].isoformat()
                                if row["last_anchor"] else None),
                "last_graded_anchor": (row["last_graded_anchor"].isoformat()
                                       if row["last_graded_anchor"] else None),
                "last_graded_at": last_graded.isoformat() if last_graded else None,
                "last_saved_at": last_saved.isoformat() if last_saved else None,
            },
            "graded": graded,
            "pending": total - graded,
            "models": {
                "base": {"graded": int(row["base_n"] or 0),
                         "avg_accuracy_pct": rate(row["base_avg"])},
                "finetuned": {"graded": int(row["fine_n"] or 0),
                              "avg_accuracy_pct": rate(row["fine_avg"])},
            },
        }

    def _history_bars(self, symbol: str, since: date) -> list[tuple[date, float]]:
        import psycopg

        from data.db import get_conn

        conn = psycopg.connect(self.history_dsn) if self.history_dsn else get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT bar_date, close FROM daily_bars "
                    "WHERE symbol = %s AND bar_date >= %s ORDER BY bar_date",
                    (symbol, since),
                )
                return [(row[0], row[1]) for row in cur.fetchall()]
        finally:
            conn.close()


_store: KronosExperimentStore | None = None


def open_experiment_store(*, dsn: str | None = None,
                          schema: str | None = None) -> KronosExperimentStore:
    global _store
    if _store is None or dsn is not None or schema is not None:
        from config import settings

        _store = KronosExperimentStore(
            dsn=dsn, schema=schema or settings.kronos_schema)
    return _store

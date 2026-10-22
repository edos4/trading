"""PostgreSQL storage and scoring for Kronos forecast history.

One row per symbol x model x forecast day. Saving the same anchor date again
upserts the prediction and clears its grading, so the settled bars can regrade
it. Win rate is directional: the sign of the predicted move from the anchor
close must match the sign of the actual move, measured at the same trading-bar
offset (not by calendar date, since forecasts come from ``pd.bdate_range`` and
can land on exchange holidays).
"""
from __future__ import annotations

from bisect import bisect_right
from contextlib import contextmanager
from datetime import date
from typing import Callable, Iterable

from core.kronos_db import KronosError, connection, verify_migrations

MODELS = ("base", "finetuned")

_UPSERT_SQL = """
INSERT INTO kronos_predictions (
    symbol, anchor_date, anchor_close, model, day_ahead, forecast_date,
    predicted_close, saved_by)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (symbol, anchor_date, model, day_ahead) DO UPDATE SET
    anchor_close = EXCLUDED.anchor_close,
    forecast_date = EXCLUDED.forecast_date,
    predicted_close = EXCLUDED.predicted_close,
    saved_by = EXCLUDED.saved_by,
    saved_at = clock_timestamp(),
    actual_date = NULL,
    actual_close = NULL,
    correct = NULL,
    graded_at = NULL
"""

_GRADE_SQL = """
UPDATE kronos_predictions
SET actual_date = %s, actual_close = %s, correct = %s, graded_at = clock_timestamp()
WHERE symbol = %s AND anchor_date = %s AND model = %s AND day_ahead = %s
"""


def _sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def directional_hit(anchor_close: float, predicted_close: float,
                    actual_close: float) -> bool:
    """True when the predicted and actual moves from the anchor agree in sign."""
    return _sign(predicted_close - anchor_close) == _sign(actual_close - anchor_close)


def _as_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _series(result: dict, model: str) -> list[dict]:
    rows = result.get(model)
    return rows if isinstance(rows, list) else []


class KronosStore:
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

    def ping(self) -> bool:
        with self.connect() as con:
            con.execute("SELECT 1")
        return True

    def save_payload(self, payload: dict, *, saved_by: str) -> dict:
        """Upsert every forecast point in a ``scripts/kronos_web_forecast.py`` payload."""
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
            if not any(series.values()):
                skipped.append(symbol)
                continue
            for model, points in series.items():
                for day_ahead, point in enumerate(points, start=1):
                    if not isinstance(point, dict):
                        continue
                    if point.get("time") is None or point.get("value") is None:
                        continue
                    rows.append((
                        symbol, _as_date(anchor_date), float(anchor_close), model,
                        day_ahead, _as_date(point["time"]), float(point["value"]), author,
                    ))
            if symbol not in symbols:
                symbols.append(symbol)
            if str(anchor_date) not in anchors:
                anchors.append(str(anchor_date))
        if rows:
            with self.connect() as con, con.cursor() as cur:
                cur.executemany(_UPSERT_SQL, rows)
        return {"predictions": len(rows), "symbols": symbols,
                "anchor_dates": anchors, "skipped": skipped}

    def grade(self, bars_provider: Callable[[str, date], Iterable[tuple]] | None = None) -> int:
        """Grade pending predictions against settled bars; return rows updated.

        ``bars_provider(symbol, since)`` returns ``(bar_date, close)`` pairs on or
        after ``since``. Injectable so tests never need ``daily_bars``.
        """
        provider = bars_provider or self._history_bars
        with self.connect() as con:
            pending = con.execute(
                "SELECT symbol, anchor_date, model, day_ahead, anchor_close, "
                "predicted_close FROM kronos_predictions WHERE correct IS NULL "
                "ORDER BY symbol, anchor_date, model, day_ahead"
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
                correct = directional_hit(float(row["anchor_close"]),
                                          float(row["predicted_close"]), actual_close)
                updates.append((actual_date, actual_close, correct, row["symbol"],
                                row["anchor_date"], row["model"], row["day_ahead"]))
            if updates:
                with con.cursor() as cur:
                    cur.executemany(_GRADE_SQL, updates)
            return len(updates)

    def recent(self, limit: int = 500) -> dict:
        """Recent saved prediction rows for inspection/auditing."""
        limit = max(1, min(int(limit), 2000))
        with self.connect() as con:
            rows = con.execute(
                "SELECT symbol, anchor_date, anchor_close, model, day_ahead, "
                "forecast_date, predicted_close, saved_by, saved_at, "
                "actual_date, actual_close, correct, graded_at "
                "FROM kronos_predictions "
                "ORDER BY anchor_date DESC, symbol, model, day_ahead "
                "LIMIT %s",
                (limit,),
            ).fetchall()
            total = con.execute(
                "SELECT COUNT(*) AS n FROM kronos_predictions").fetchone()["n"]
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

    def stats(self) -> dict:
        """Aggregate saved predictions into chart-ready per-day win rates."""
        with self.connect() as con:
            rows = con.execute(
                "SELECT model, day_ahead, "
                "       COUNT(*) AS total, "
                "       COUNT(*) FILTER (WHERE correct IS NOT NULL) AS graded, "
                "       COUNT(*) FILTER (WHERE correct) AS wins "
                "FROM kronos_predictions GROUP BY model, day_ahead "
                "ORDER BY model, day_ahead"
            ).fetchall()
            summary = con.execute(
                "SELECT COUNT(*) AS total, "
                "       COUNT(*) FILTER (WHERE correct IS NOT NULL) AS graded, "
                "       COUNT(DISTINCT symbol) AS symbols, "
                "       MIN(anchor_date) AS first_anchor, "
                "       MAX(anchor_date) AS last_anchor "
                "FROM kronos_predictions"
            ).fetchone()
        grouped: dict[str, list[dict]] = {model: [] for model in MODELS}
        for row in rows:
            grouped.setdefault(row["model"], []).append(row)
        models = {}
        for model in MODELS:
            model_rows = grouped.get(model, [])
            wins_total = sum(int(r["wins"] or 0) for r in model_rows)
            graded_total = sum(int(r["graded"] or 0) for r in model_rows)
            days = []
            for row in model_rows:
                graded = int(row["graded"] or 0)
                wins = int(row["wins"] or 0)
                days.append({
                    "day": int(row["day_ahead"]),
                    "wins": wins,
                    "n": graded,
                    "win_rate_pct": round(wins / graded * 100, 1) if graded else None,
                })
            models[model] = {
                "overall": {
                    "wins": wins_total,
                    "n": graded_total,
                    "win_rate_pct": (round(wins_total / graded_total * 100, 1)
                                     if graded_total else None),
                },
                "days": days,
            }
        total = int(summary["total"] or 0)
        graded = int(summary["graded"] or 0)
        first_anchor = summary["first_anchor"]
        last_anchor = summary["last_anchor"]
        return {
            "saved": {
                "predictions": total,
                "symbols": int(summary["symbols"] or 0),
                "first_anchor": first_anchor.isoformat() if first_anchor else None,
                "last_anchor": last_anchor.isoformat() if last_anchor else None,
            },
            "pending": total - graded,
            "graded": graded,
            "models": models,
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


_store: KronosStore | None = None


def open_kronos_store(*, dsn: str | None = None,
                      schema: str | None = None) -> KronosStore:
    global _store
    if _store is None or dsn is not None or schema is not None:
        from config import settings

        _store = KronosStore(dsn=dsn, schema=schema or settings.kronos_schema)
    return _store

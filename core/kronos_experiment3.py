"""Removable, frozen November 10 +2-session Kronos comparison (Experiment 3)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

from data.db import get_conn
from utils.logger import log

ROOT = Path(__file__).resolve().parents[1]
NY = ZoneInfo("America/New_York")
AS_OF = date(2026, 11, 10)
TARGETS = (date(2026, 11, 11), date(2026, 11, 12))
DEADLINE = datetime(2026, 11, 13, 9, 30, tzinfo=NY)
UNIVERSE_FILE = ROOT / "docs/kronos-experiment3-universe.json"
WORKER = ROOT / "scripts/kronos_experiment3_forecast.py"
SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS kronos_experiment3;
CREATE TABLE IF NOT EXISTS kronos_experiment3.run (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS kronos_experiment3.forecasts (
    symbol TEXT PRIMARY KEY,
    rank INTEGER UNIQUE NOT NULL,
    payload JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS kronos_experiment3.actuals (
    symbol TEXT NOT NULL REFERENCES kronos_experiment3.forecasts(symbol),
    session_date DATE NOT NULL CHECK (session_date IN ('2026-11-11', '2026-11-12')),
    close DOUBLE PRECISION NOT NULL CHECK (close > 0 AND close < 'Infinity'::float8),
    source TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, session_date)
);
"""


def ny_now(now: datetime | None = None) -> datetime:
    """Require an unambiguous timestamp for all market-time gates."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Experiment 3 timestamps must include a timezone")
    return now.astimezone(NY)


def due_targets(now: datetime | None = None) -> list[date]:
    """Only the two completed closes; automatic collection ends at DEADLINE."""
    local = ny_now(now)
    if local >= DEADLINE:
        return []
    return [day for day in TARGETS
            if local >= datetime.combine(day, time(16, 30), NY)]


def positive(value) -> bool:
    """Whether a saved price is a finite positive number."""
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def capture_universe(now: datetime | None = None) -> dict:
    """Capture the completed anchor session, before the next session opens.

    TradingView has no historical screener: never silently label a later
    ranking November 10. Subsequent starts reuse the checked-in snapshot.
    """
    local = ny_now(now)
    if not (datetime(2026, 11, 10, 16, 30, tzinfo=NY) <= local
            < datetime(2026, 11, 11, 9, 30, tzinfo=NY)):
        raise ValueError("The November 10 screener capture window has closed; use the frozen snapshot")
    from tradingview_screener import Query, col
    from data.tv_client import _get_scanner_data

    query = (Query().set_markets("america")
             .select("name", "description", "exchange", "type", "close", "volume", "Value.Traded")
             .where(col("exchange") == "NASDAQ", col("type").isin(["stock", "dr"]))
             .order_by("Value.Traded", ascending=False).limit(50))
    total, frame = _get_scanner_data(query)
    rows = []
    for _, row in frame.iterrows():
        value = float(row["Value.Traded"])
        if not positive(value) or not positive(float(row["close"])):
            raise ValueError("Screener returned an invalid ranking value")
        rows.append({"symbol": str(row["name"]), "rank": len(rows) + 1,
                     "name": str(row["description"]), "exchange": str(row["exchange"]),
                     "type": str(row["type"]), "close": float(row["close"]),
                     "volume": int(row["volume"]), "dollar_volume": value})
    snapshot = {"as_of": AS_OF.isoformat(), "captured_at": local.isoformat(),
                "source": "TradingView america screener, completed regular session",
                "ranking": "Value.Traded descending (session dollar volume)",
                "filter": "NASDAQ; type stock or dr (includes ADRs; excludes ETFs)",
                "eligible_count": total, "stocks": rows}
    validate_universe(snapshot)
    return snapshot


def validate_universe(snapshot: dict) -> None:
    """Reject stale, partial, duplicate, or misranked universe snapshots."""
    rows = snapshot["stocks"]
    captured = ny_now(datetime.fromisoformat(snapshot["captured_at"]))
    if (snapshot["as_of"] != AS_OF.isoformat() or len(rows) != 50
            or len({r["symbol"] for r in rows}) != 50
            or not datetime(2026, 11, 10, 16, 30, tzinfo=NY) <= captured
            < datetime(2026, 11, 11, 9, 30, tzinfo=NY)):
        raise ValueError("Expected the fixed 50-stock November 10 universe")
    for i, row in enumerate(rows):
        if (row["rank"] != i + 1 or row["exchange"] != "NASDAQ"
                or row["type"] not in ("stock", "dr") or not positive(row["dollar_volume"])
                or (i and row["dollar_volume"] > rows[i - 1]["dollar_volume"])):
            raise ValueError("Invalid NASDAQ stock ranking")


def provider_bars(symbol: str) -> list[dict]:
    """Read Yahoo daily bars without changing the shared history or disk cache."""
    from data.tv_client import TVClient
    candles = TVClient(screener="america", exchange="NASDAQ")._fetch_history_chart(
        symbol, "1d", live=True)
    return [{"time": c.timestamp.astimezone(NY).date().isoformat(),
             "open": float(c.open), "high": float(c.high), "low": float(c.low),
             "close": float(c.close), "volume": float(c.volume)}
            for c in candles if c.timestamp is not None]


def load_inputs(snapshot: dict) -> list[dict]:
    """Freeze causal OHLCV inputs, using the provider only for missing history."""
    import pandas as pd
    from core.kronos_input import prepare_context

    inputs = []
    with get_conn() as conn:
        for stock in snapshot["stocks"]:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT ON (bar_date) bar_date, open, high, low, close, volume "
                            "FROM daily_bars WHERE symbol=%s AND bar_date<=%s "
                            "ORDER BY bar_date DESC, ts DESC LIMIT 400", (stock["symbol"], AS_OF))
                rows = cur.fetchall()[::-1]
            bars = [dict(zip(("time", "open", "high", "low", "close", "volume"),
                             [r[0].isoformat(), *map(float, r[1:])])) for r in rows]
            source = "daily_bars"
            if len(bars) < 60 or bars[-1]["time"] != AS_OF.isoformat():
                bars = [r for r in provider_bars(stock["symbol"]) if r["time"] <= AS_OF.isoformat()]
                source = "Yahoo chart (missing database history)"
            frame = pd.DataFrame(bars)
            if frame.empty or bars[-1]["time"] != AS_OF.isoformat():
                raise ValueError(f"{stock['symbol']}: no November 10 anchor")
            frame.index = pd.to_datetime(frame["time"])
            context = prepare_context(frame, 400)
            if not math.isclose(float(frame.close.iloc[-1]), stock["close"], rel_tol=.0001):
                raise ValueError(f"{stock['symbol']}: historical anchor differs from screener close")
            # Save the exact values passed to both models, not just a history hash.
            bars = [{"time": t.date().isoformat(), **{k: float(row[k]) for k in
                     ("open", "high", "low", "close", "volume")}}
                    for t, row in context.frame.iterrows()]
            inputs.append({**stock, "input_source": source, "context": context.evidence(),
                           "bars": bars})
    return inputs


def validate_predictions(payload: dict, inputs: list[dict]) -> None:
    """An incomplete/wrong-date model run must never become the frozen result."""
    results = payload.get("results", [])
    if len(results) != 50 or {r["symbol"] for r in results} != {r["symbol"] for r in inputs}:
        raise ValueError("Expected both models for all 50 frozen stocks")
    anchors = {r["symbol"]: r["bars"][-1]["close"] for r in inputs}
    for row in results:
        if (row.get("errors") or row["anchor"]["time"] != AS_OF.isoformat()
                or row["days"] != 2 or row["anchor"]["value"] != anchors[row["symbol"]]
                or any(p["time"] > AS_OF.isoformat() for p in row["history"])):
            raise ValueError(f"{row['symbol']}: invalid forecast anchor or errors")
        for model in ("base", "finetuned"):
            path = row[model]
            if ([p["time"] for p in path] != [d.isoformat() for d in TARGETS]
                    or not all(positive(p["value"]) for p in path)):
                raise ValueError(f"{row['symbol']}: invalid {model} forecast")


def start(now: datetime | None = None) -> dict:
    """Generate once before target trading begins; later invocations only read."""
    from psycopg.types.json import Jsonb
    from core.kronos_process import run_worker

    existing = read_experiment(now)
    if existing["status"] != "not_started":
        return existing
    if ny_now(now) >= datetime(2026, 11, 11, 9, 30, tzinfo=NY):
        raise ValueError("Forecast creation closed before the first target session opened")
    if UNIVERSE_FILE.exists():
        snapshot = json.loads(UNIVERSE_FILE.read_text())
        validate_universe(snapshot)
    else:
        snapshot = capture_universe(now)
        UNIVERSE_FILE.write_text(json.dumps(snapshot, indent=2) + "\n")
    inputs = load_inputs(snapshot)
    with tempfile.TemporaryDirectory(prefix="kronos-experiment3-") as folder:
        input_path = Path(folder) / "inputs.json"
        input_path.write_text(json.dumps(inputs, allow_nan=False))
        payload = run_worker(WORKER, ["--input", str(input_path)], timeout_s=1800,
                             label="experiment3")
    validate_predictions(payload, inputs)
    generated = ny_now()
    if generated >= datetime(2026, 11, 11, 9, 30, tzinfo=NY):
        raise ValueError("Forecast finished too late to qualify; nothing saved")
    metadata = {"as_of": AS_OF.isoformat(), "target_dates": [d.isoformat() for d in TARGETS],
                "generated_at": generated.isoformat(), "universe": snapshot,
                "models": payload["models"], "settings": payload["settings"],
                "input_sha256": hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()}
    by_symbol = {r["symbol"]: r for r in inputs}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.execute("INSERT INTO kronos_experiment3.run (id, metadata) VALUES (1,%s) "
                        "ON CONFLICT DO NOTHING RETURNING id", (Jsonb(metadata),))
            if cur.fetchone() is not None:
                for row in payload["results"]:
                    item = by_symbol[row["symbol"]]
                    row.update(rank=item["rank"], dollar_volume=item["dollar_volume"], input=item)
                    cur.execute("INSERT INTO kronos_experiment3.forecasts (symbol,rank,payload) "
                                "VALUES (%s,%s,%s)", (row["symbol"], row["rank"], Jsonb(row)))
    return read_experiment(now)


def direction(value: float, anchor: float) -> str:
    """Classify the cumulative move at USD cent precision; unchanged is flat."""
    price = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    origin = Decimal(str(anchor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return "up" if price > origin else "down" if price < origin else "flat"


def direction_results(row: dict) -> list[dict]:
    """Compare each model's +1D/+2D move with actual, always from the anchor."""
    anchor = row["anchor"]["value"]
    actuals = {p["time"]: p["value"] for p in row["actuals"]}
    results = []
    for step, day in enumerate(TARGETS):
        observed = actuals.get(day.isoformat())
        actual = direction(observed, anchor) if observed is not None else None
        result = {"day": step + 1, "date": day.isoformat(), "actual": actual}
        for model in ("base", "finetuned"):
            predicted = direction(row[model][step]["value"], anchor)
            result[model] = {"predicted": predicted,
                             "correct": predicted == actual if actual is not None else None}
        results.append(result)
    return results


def score(results: list[dict]) -> dict:
    """Matched-close errors and direction accuracy per model and horizon."""
    summary = {}
    assessed = [(row, direction_results(row)) for row in results]
    for model in ("base", "finetuned"):
        summary[model] = []
        for step, day in enumerate(TARGETS):
            matches = []
            for row, directions in assessed:
                actual = next((p["value"] for p in row["actuals"] if p["time"] == day.isoformat()), None)
                if actual is None:
                    continue
                predicted = row[model][step]["value"]
                matches.append((abs(predicted / actual - 1) * 100,
                                directions[step][model]["correct"]))
            summary[model].append({"day": step + 1, "date": day.isoformat(), "count": len(matches),
                                   "direction_correct": sum(m[1] for m in matches),
                                   "mape_pct": sum(m[0] for m in matches) / len(matches) if matches else None,
                                   "direction_accuracy_pct": 100 * sum(m[1] for m in matches) / len(matches) if matches else None})
    return summary


def direction_report_rows(experiment: dict) -> list[dict]:
    """One row per target session with counts for both frozen models."""
    if experiment["status"] == "not_started":
        return []
    expected = len(experiment["results"])
    scores = {model: {s["date"]: s for s in experiment["scores"][model]}
              for model in ("base", "finetuned")}
    rows = []
    for step, day in enumerate(TARGETS):
        base = scores["base"][day.isoformat()]
        row = {"date": day.isoformat(), "day": step + 1, "expected": expected,
               "evaluated": base["count"], "pending": expected - base["count"]}
        for model in ("base", "finetuned"):
            summary = scores[model][day.isoformat()]
            row[model] = {"correct": summary["direction_correct"],
                          "incorrect": summary["count"] - summary["direction_correct"],
                          "accuracy_pct": summary["direction_accuracy_pct"]}
        rows.append(row)
    return rows


def render_direction_report(experiment: dict) -> str:
    """Standalone Markdown report derived entirely from saved observations."""
    lines = ["# Kronos Experiment 3 — Daily directional accuracy", "",
             "Anchor: November 10, 2026 close. Each date measures the cumulative move from this anchor.", "",
             "Up / Down / Flat use USD cent precision. Missing actual closes are Pending and excluded from accuracy.", ""]
    rows = direction_report_rows(experiment)
    if not rows:
        return "\n".join([*lines, "No saved forecasts yet.", ""])
    lines += [f"Frozen stocks: {len(experiment['results'])}. Actual closes saved: {experiment['actual_count']}/100.", "",
              "| US session | Horizon | Stocks evaluated | Stocks pending | Base correct | Base incorrect | Base accuracy | Fine-tuned correct | Fine-tuned incorrect | Fine-tuned accuracy |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        values = [row["date"], f"+{row['day']}D", f"{row['evaluated']}/{row['expected']}", str(row["pending"])]
        for model in ("base", "finetuned"):
            result = row[model]
            values += ([str(result["correct"]), str(result["incorrect"]), f"{result['accuracy_pct']:.1f}%"]
                       if row["evaluated"] else ["Pending", "Pending", "Pending"])
        lines.append("| " + " | ".join(values) + " |")
    lines += ["", "Accuracy = correct stocks / evaluated stocks × 100. A Flat prediction is correct only when actual is Flat.", "",
              "Forecasts and actual observations stay fixed in PostgreSQL; this report updates as saved actual closes arrive.", "",
              "Automatic collection is limited to November 11 and 12, ending November 13 at 09:30 ET / 22:30 PHT.", ""]
    return "\n".join(lines)


def save_direction_report(experiment: dict, path: Path | None = None) -> Path:
    """Atomically refresh the local report only when its saved results change."""
    from core.kronos_weekly import atomic_text

    path = path or ROOT / "logs/kronos_experiment3_direction_report.md"
    content = render_direction_report(experiment)
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        atomic_text(path, content)
    return path


def read_experiment(now: datetime | None = None) -> dict:
    """Read saved forecasts/actuals; a page refresh never forecasts or collects."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('kronos_experiment3.run')")
            if cur.fetchone()[0] is None:
                return {"status": "not_started", "results": []}
            cur.execute("SELECT metadata FROM kronos_experiment3.run WHERE id=1")
            saved = cur.fetchone()
            if saved is None:
                return {"status": "not_started", "results": []}
            metadata = saved[0]
            cur.execute("SELECT payload FROM kronos_experiment3.forecasts ORDER BY rank")
            results = cur.fetchall()
            cur.execute("SELECT symbol,session_date,close,source,observed_at "
                        "FROM kronos_experiment3.actuals ORDER BY symbol,session_date")
            actuals = cur.fetchall()
    by_symbol = {}
    for symbol, day, close, source, observed in actuals:
        by_symbol.setdefault(symbol, []).append({"time": day.isoformat(), "value": close,
                                                "source": source, "observed_at": observed.isoformat()})
    rows = []
    for (saved_row,) in results:
        # Full OHLCV inputs stay in PostgreSQL; the page only needs the chart.
        row = {k: v for k, v in saved_row.items() if k != "input"}
        row["actuals"] = by_symbol.get(row["symbol"], [])
        row["direction_results"] = direction_results(row)
        rows.append(row)
    count = len(actuals)
    status = "complete" if count == 100 else "expired_incomplete" if ny_now(now) >= DEADLINE else "collecting"
    experiment = {"status": status, "metadata": metadata, "results": rows,
                  "actual_count": count, "expected_actual_count": 100, "scores": score(rows)}
    experiment["direction_report"] = direction_report_rows(experiment)
    return experiment


def collect(now: datetime | None = None) -> dict:
    """Store only finalized November 11/12 closes, once each, without inference."""
    experiment = read_experiment(now)
    if experiment["status"] in ("not_started", "complete", "expired_incomplete"):
        return experiment
    due = {d.isoformat() for d in due_targets(now)}
    missing = {r["symbol"]: due - {p["time"] for p in r["actuals"]}
               for r in experiment["results"]}
    missing = {s: dates for s, dates in missing.items() if dates}
    if not missing:
        return experiment

    def fetch(symbol: str) -> tuple[str, list[dict]]:
        try:
            return symbol, provider_bars(symbol)
        except Exception as exc:
            log.warning(f"Experiment 3 | {symbol}: actual collection failed: {exc}")
            return symbol, []

    with ThreadPoolExecutor(max_workers=3) as pool:
        fetched = list(pool.map(fetch, missing))
    with get_conn() as conn:
        with conn.cursor() as cur:
            for symbol, bars in fetched:
                for bar in bars:
                    if bar["time"] in missing[symbol] and positive(bar["close"]):
                        cur.execute("INSERT INTO kronos_experiment3.actuals "
                                    "(symbol,session_date,close,source) VALUES (%s,%s,%s,%s) "
                                    "ON CONFLICT DO NOTHING", (symbol, bar["time"], bar["close"], "Yahoo chart regular-session daily close"))
    return read_experiment(now)

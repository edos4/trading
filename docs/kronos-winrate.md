# Kronos forecast history and win rate

## 1. What this adds

The **Kronos** tab (`python main.py --web`) can now persist each day's forecast
for a fixed symbol set and score those predictions once the sessions settle.

- **Save today's forecasts** forecasts the configured tracked symbols and writes
  every predicted close to PostgreSQL.
- **Refresh win rate** grades pending predictions against actual daily closes
  and graphs the directional hit rate per day-ahead horizon for both
  Kronos-base and the fine-tuned model.

The browser never chooses the saved symbol set, and saving is explicit — running
an exploratory forecast with **Submit** or **Forecast top** does not write
anything.

## 2. Configuration

| Setting | Env var | Default | Meaning |
|---|---|---|---|
| `kronos_database_url` | `KRONOS_DATABASE_URL` | empty | Dedicated PostgreSQL DSN. Empty reuses `DATABASE_URL` / `DB_*`. |
| `kronos_schema` | `KRONOS_SCHEMA` | `kronos` | Schema for forecast history. Tests use `kronos_test_<uuid>`. |
| `kronos_tracked_symbols` | `KRONOS_TRACKED_SYMBOLS` | empty | Comma-separated daily set. Empty falls back to the first 100 `WATCHLIST` names (see below). |
| `kronos_save_days` | `KRONOS_SAVE_DAYS` | `5` | Default horizon for **Save today's forecasts** (clamped 1–60). |

The tracked list is normalised (spaces removed, uppercased, de-duplicated) by
the `settings.kronos_symbols` property. It is capped at
`web/kronos.py::MAX_SYMBOLS` (100).

`WATCHLIST` in this repo is the full scanned universe (thousands of names), not
a curated daily set, so the fallback takes the first 100 entries — enough for a
daily snapshot without turning one click into hours of GPU work. An explicit
`KRONOS_TRACKED_SYMBOLS` value is never silently truncated: if it exceeds 100 the
save route returns an error naming the limit.

## 3. Schema

Migration: `migrations/kronos/0001_predictions.sql`; the shared runner owns the
schema, `schema_migrations`, and `search_path`.

```sql
CREATE TABLE kronos_predictions (
    symbol           TEXT             NOT NULL,
    anchor_date      DATE             NOT NULL,  -- last actual bar in the context
    anchor_close     DOUBLE PRECISION NOT NULL,
    model            TEXT             NOT NULL CHECK (model IN ('base', 'finetuned')),
    day_ahead        SMALLINT         NOT NULL CHECK (day_ahead BETWEEN 1 AND 60),
    forecast_date    DATE             NOT NULL,  -- intended business day
    predicted_close  DOUBLE PRECISION NOT NULL,
    saved_by         TEXT             NOT NULL,
    saved_at         TIMESTAMPTZ      NOT NULL DEFAULT clock_timestamp(),
    actual_date      DATE,                       -- settled bar actually used
    actual_close     DOUBLE PRECISION,
    correct          BOOLEAN,
    graded_at        TIMESTAMPTZ,
    PRIMARY KEY (symbol, anchor_date, model, day_ahead)
);
```

One row per symbol × model × horizon day. Re-saving the same anchor date
**upserts** the prediction and clears its grading fields, so it is regraded
against the settled bars. The `history` arrays and per-symbol errors from the
forecast payload are not stored — only forecast points awaiting/used for
scoring.

## 4. Grading rule

For a prediction at horizon `d`:

1. The **anchor** is the last actual close used as model context
   (`result.anchor`, e.g. `2026-10-14 @ 250.00`).
2. The **actual** is the `d`-th trading bar after `anchor_date` in `daily_bars`
   (positional, sorted by `bar_date`) — deliberately not the calendar date in
   the forecast row.
3. The prediction is **correct** when
   `sign(predicted_close − anchor_close) == sign(actual_close − anchor_close)`.

Why positional grading: forecast dates come from `pd.bdate_range` (Mon–Fri, no
exchange holiday calendar), so a stored forecast can point at a US market
holiday. Comparing by bar offset keeps the horizon aligned with real sessions;
`actual_date` records the bar that was actually used. If fewer than `d` bars
exist after the anchor, the row stays **pending**.

Direction only: magnitude is ignored, so a correct +0.1% call and a correct +8%
call both count as wins. Ties (`sign == 0`) only match other ties.

## 5. HTTP API

All routes require login (`require_login`).

### `POST /api/kronos/save`

Body: `{"days": 5}` (optional; defaults to `KRONOS_SAVE_DAYS`).

1. Reads `settings.kronos_symbols` and validates size/characters.
2. Runs the existing GPU forecast subprocess for that list and horizon.
3. Upserts every forecast point.

Response: the normal forecast payload plus a `saved` summary:

```json
{
  "days": 5, "lookback": 400, "symbols": ["AAPL", "MSFT"],
  "results": [ ... ],
  "saved": {
    "predictions": 20,
    "symbols": ["AAPL", "MSFT"],
    "anchor_dates": ["2026-10-16"],
    "skipped": []
  }
}
```

Errors: `400` bad days / no tracked symbols / oversized list, `409` forecast
already running, `502` forecast failure, `503` Kronos venv missing or storage
unmigrated/unreachable.

### `GET /api/kronos/predictions?limit=500`

Raw saved rows for auditing, newest anchor first (`limit` 1–2000, default 500).
Each row includes `symbol`, `anchor_date`, `anchor_close`, `model`, `day_ahead`,
`forecast_date`, `predicted_close`, `saved_by`, `saved_at`, `actual_date`,
`actual_close`, `correct`, and `graded_at`.

```json
{
  "total": 240,
  "limit": 500,
  "rows": [{
    "symbol": "BDO.PS", "anchor_date": "2026-10-16", "anchor_close": 128.5,
    "model": "base", "day_ahead": 1, "forecast_date": "2026-10-19",
    "predicted_close": 129.1, "saved_by": "admin",
    "saved_at": "2026-10-16T15:20:00", "actual_date": null,
    "actual_close": null, "correct": null, "graded_at": null
  }]
}
```

### `GET /api/kronos/winrate`

Read-only aggregate; performs no grading writes.

```json
{
  "saved": {"predictions": 120, "symbols": 12,
            "first_anchor": "2026-09-01", "last_anchor": "2026-10-16"},
  "pending": 42,
  "graded": 78,
  "models": {
    "base": {
      "overall": {"wins": 44, "n": 78, "win_rate_pct": 56.4},
      "days": [{"day": 1, "wins": 18, "n": 26, "win_rate_pct": 69.2}, ...]
    },
    "finetuned": { "overall": { ... }, "days": [ ... ] }
  }
}
```

`overall` is wins ÷ graded summed over all horizons (not an average of daily
rates). `win_rate_pct` is `null` when a day has no graded rows.

### `POST /api/kronos/winrate/refresh`

Grades every pending row whose bars are available, then returns the same shape
with `graded_now` added.

## 6. UI

Inside `/kronos`, below the per-symbol charts:

- **Save today's forecasts** — server-side tracked list, horizon from the
  adjacent input; renders the resulting charts and reloads the stats.
- **Refresh win rate** — grades and redraws.
- **Summary line** — saved rows, symbols, graded/pending counts, and each
  model's overall win rate.
- **Win-rate chart** — base (blue dashed) and fine-tuned (red) lines, x-axis
  `D1..Dn` (day ahead), y-axis fixed to 0–100 with a dashed 50% reference.
- **Saved prediction rows** — an expandable table of the last 500 rows with the
  saved timestamp, symbol, anchor date/close, model, day ahead, forecast
  date/close, actual date/close, and `win` / `loss` / `pending`. This is the
  audit view for verifying that the stored data matches the charts.

If the schema is missing or the database is down, the panel shows the error and
`python scripts/setup_kronos_db.py`; the forecast forms keep working.

## 7. Setup and deploy

```bash
.venv/bin/python scripts/setup_kronos_db.py
```

The script is idempotent, creates a dedicated database when
`KRONOS_DATABASE_URL` names one, and applies pending migrations atomically under
an advisory lock. It is wired into `install.sh` and `deploy_to_contabo.sh` as a
non-fatal step next to the pattern-editor setup.

## 8. Tests

```bash
# No database required
.venv/bin/python -m pytest web/test_kronos_api.py web/test_kronos_history_api.py -q

# Store + grading against a disposable PostgreSQL database
KRONOS_TEST_DATABASE_URL=postgresql://... \
  .venv/bin/python -m pytest core/test_kronos_store.py -q
```

DB tests use a generated `kronos_test_<uuid>` schema and drop it afterwards.
`grade()` accepts an injected `bars_provider`, so scoring math is tested
without `daily_bars`.

## 9. Limitations / next steps

- Grading is directional only. Magnitude metrics (MAE, signed return) can reuse
  `core/kronos_eval.py` and be added to `KronosStore.stats()`.
- Predictions saved after the anchor close stay pending until the market prints
  enough sessions; the UI reports the pending count rather than hiding it.
- The forecast subprocess runs one symbol at a time (existing lock); saving a
  large tracked list takes proportionally longer (~3 s per symbol observed for
  a +5d run).
- The win-rate snapshot has no scheduler yet. Saving is a manual one-click
  action by design; a cron/systemd timer could call `POST /api/kronos/save` with
  the same login credentials if unattended daily capture is wanted. (Experiment
  2 below does have a scheduler.)

## 10. Experiment 2 — 5-day close accuracy

Experiment 2 is a separate panel below the win-rate section. It mirrors the
experiment spreadsheet: one row per symbol × anchor date × horizon, with the
base and fine-tuned predicted closes side by side and a percentage accuracy for
each once the actual close settles.

- Horizons are fixed to **D1 and D5** even though the underlying forecast runs
  five trading days.
- Accuracy is `100 × (1 − |predicted − actual| / actual)`. It is stored per
  model at grading time. The panel shows the averages **split by horizon**
  (a `+1D` row and a `+5D` row, each with the base and fine-tuned mean and its
  graded count) because a five-day miss and a one-day miss are not comparable;
  the raw table below shows every value used.
- Symbols are the same tracked list used by **Save today's forecasts**
  (`KRONOS_TRACKED_SYMBOLS`, falling back to the first 100 `WATCHLIST` names).

### Schema

Migration: `migrations/kronos/0002_experiment2.sql`.

```sql
CREATE TABLE kronos_experiment2 (
    symbol                 TEXT             NOT NULL,
    anchor_date            DATE             NOT NULL,
    anchor_close           DOUBLE PRECISION NOT NULL,
    day_ahead              SMALLINT         NOT NULL CHECK (day_ahead BETWEEN 1 AND 60),
    forecast_date          DATE             NOT NULL,
    base_close             DOUBLE PRECISION,
    finetuned_close        DOUBLE PRECISION,
    actual_date            DATE,
    actual_close           DOUBLE PRECISION,
    base_accuracy_pct      DOUBLE PRECISION,
    finetuned_accuracy_pct DOUBLE PRECISION,
    saved_by               TEXT             NOT NULL,
    saved_at               TIMESTAMPTZ      NOT NULL DEFAULT clock_timestamp(),
    graded_at              TIMESTAMPTZ,
    PRIMARY KEY (symbol, anchor_date, day_ahead)
);
```

Re-running the same anchor date upserts the row and clears its grading, exactly
like `kronos_predictions`. Grading uses the `day_ahead`-th trading bar after the
anchor (positional, not the calendar forecast date).

### HTTP API

- `POST /api/kronos/experiment2/run` — forecast the tracked symbols for five
  days, upsert the D1/D5 rows, grade whatever settled, and return
  `{days, symbols, saved, graded_now, summary}`.
- `POST /api/kronos/experiment2/refresh` — grade pending rows only and return
  `{graded_now, summary}`.
- `GET /api/kronos/experiment2?limit=2000` — raw rows (settled rows first, then
  newest anchor) plus the `summary` object used by the section header.

### Cron

`scripts/kronos_experiment2_daily.sh` POSTs the endpoints with the repo's
`WEB_UI_USERNAME` / `WEB_UI_PASSWORD`. Debian cron evaluates schedules in host
time, so the installed block invokes the script hourly and the script proceeds
only during the 17:00 `America/New_York` hour on weekdays (no host-timezone or
DST assumption). It needs no manual button press:

1. **Grade first** — `POST /api/kronos/experiment2/refresh`, so settled rows are
   scored even if the forecast step later fails.
2. **Capture** — `POST /api/kronos/experiment2/run` for the one-week save window.
3. **Grade tail** — after the save window, keep calling the refresh endpoint
   daily for ten more days so the final D5 anchors settle.
4. Then the script removes its own `# kronos-experiment2 BEGIN/END` block.

Logs go to `logs/kronos_experiment2.log` (`--check` only verifies
authentication).

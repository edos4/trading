# Pattern Editor screens (web)

The Patterns screens are thin renderers over one shared, UI-independent facade,
`core/pattern_editor_api.py` (`PatternEditor`). The web frontend submits durable
requests and reads the same PostgreSQL job/version/run records; it does not
resolve patterns, validate presets, or decide eligibility itself.

## Shared facade

- Reads: `catalog`, `versions_for`, `version_detail` (version + lifecycle +
  reports + backtests), `source`, `diff`, `presets`, `run_payload`.
- Writes: `submit_edit`, `cancel_job`, `retry_backtest`, `set_default`,
  `archive`.
- `edit_request_from_values` mirrors `request_from_values`: an explicit saved
  `preset_id` or settings that validate and are persisted; contradictory values
  raise. An automatic edit requires a historical-stream preset.
- Execution runs on a background thread owned by the facade. The durable job row
  is the authority, so a page reload reconnects by id; the
  thread is only a fast path.
- `run_payload` is now the single implementation used by both `/api/backtest`
  and the Patterns tab.

## Web tab

- `web/templates/patterns.html` + `web/static/patterns.js`; a **Patterns** link
- Authenticated endpoints in `web/app.py`:
  - `GET /patterns`
  - `GET /api/patterns`, `GET /api/patterns/{pattern_id}/versions`
  - `GET /api/patterns/versions/{version_id}`, `.../source`, `.../diff`
  - `GET /api/patterns/provider` (AI provider config + account balance)
  - `POST /api/patterns/edits`, `GET /api/patterns/edits/{job_id}`,
    `POST /api/patterns/edits/{job_id}/cancel`, `.../retry`
  - `GET /api/patterns/runs/{run_id}`
  - `POST /api/patterns/versions/{version_id}/default`, `.../archive`
  - presets reuse the authenticated `/api/backtest/presets` endpoints.
- Errors are mapped once (`web/patterns.py::patterns_error`): unknown job 404,
  conflict 409, database unavailable/migration required 503, domain/value error
  400. No endpoint falls back to files or in-memory state.
- The page shows version history with default/availability/validation/archived
  badges, source and documentation, a diff against the parent, prior reports and
  runs, an instruction box with a visible saved-preset selector and the replay
  settings, live progress, the explanation, edit diff, validation diagnostics and
  a candidate-vs-base metrics/trades comparison. Submit disables itself as a fast
  path only; the API is idempotent and enforces every invariant.
- The AI-edit panel shows the DeepSeek account balance (`Provider: <model> ·
  Balance: <CUR> <total>`), refreshed on load and after a terminal job, with a red
  `INSUFFICIENT — top up` when the provider reports no funds.

## Lifecycle actions

- **Set default** sends the catalog generation; a stale generation is a 409 and
  the page reloads. Only validated versions with a successful backtest are
  eligible (server-side check).
- **Backtest again** retries the recorded edit job's backtests; it never calls the
  provider and never creates a version.
- **Edit from this version** focuses the instruction box for the selected base.
- **Delete version** archives; deleting the current default requires a
  replacement chosen in the same transaction. Archived versions cannot be
  selected for new runs or reused as defaults.

## Evidence

- Web tests: `core/test_patterns_web.py` (auth, navigation, catalog/versions/
  source/diff, submit→generate→validate→backtest, duplicate Submit,
  reload reconnect, failed validation, block+retry without a model call,
  backtest-only retry, cancellation, expired lease, default/archive with
  replacement and conflict, malformed requests, database unavailable).
- Smokes: [P09 smoke](verification/pattern-editor-p09-smoke.txt) (real
  `main.py --web`). See the tracker for the recorded gate commands.

## Not performed / gates

- Live DeepSeek calls and a real candidate sandbox remain unconfigured here.
- Full integration/release rehearsal (backup/restore, restart, concurrency,
  cutover) is P11.

## Editing from a trade chart

Double-click an open or closed trade, then right-click its chart and choose
**Edit pattern…**. The editor attaches the chart's symbol, actual OHLCV history,
pattern annotations, and version identity. Enter the change and choose the
backtest start/end dates. Leave Symbols blank to scan the universe plus the
chart symbol (PH uses symbols from PH history); enter symbols to limit the scan.

After generation, inspect the new version and its detections. Select a detection
to open that version's chart. **Backtest again** uses
the current dates and symbols without another AI call. **Save / use version**
makes it the default; **Reject / archive** keeps it out of future selection.
Switching versions restores the latest completed run and clears unrelated charts.
Installations with `PATTERN_API_URL` use the same remote editor endpoints
as the web page.

# Pattern Editor screens (P09 web, P10 desktop)

The Patterns screens are thin renderers over one shared, UI-independent facade,
`core/pattern_editor_api.py` (`PatternEditor`). Web and desktop submit the same
durable requests and read the same PostgreSQL job/version/run records; neither
frontend resolves patterns, validates presets, or decides eligibility itself.

## Shared facade

- Reads: `catalog`, `versions_for`, `version_detail` (version + lifecycle +
  reports + backtests), `source`, `diff`, `presets`, `run_payload`.
- Writes: `submit_edit`, `cancel_job`, `retry_backtest`, `set_default`,
  `archive`.
- `edit_request_from_values` mirrors `request_from_values`: an explicit saved
  `preset_id` or settings that validate and are persisted; contradictory values
  raise. An automatic edit requires a historical-stream preset.
- Execution runs on a background thread owned by the facade. The durable job row
  is the authority, so a page reload or a reopened dialog reconnects by id; the
  thread is only a fast path.
- `run_payload` is now the single implementation used by both `/api/backtest`
  and the Patterns tab.

## Web tab (P09)

- `web/templates/patterns.html` + `web/static/patterns.js`; a **Patterns** link
  sits immediately beside **Kronos** in `web/templates/base.html`.
- Authenticated endpoints in `web/app.py`:
  - `GET /patterns`
  - `GET /api/patterns`, `GET /api/patterns/{pattern_id}/versions`
  - `GET /api/patterns/versions/{version_id}`, `.../source`, `.../diff`
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

## Desktop dialog (P10)

- `ui/patterns_dialog.py` is opened by the **Patterns** toolbar button beside
  **Kronos** in `ui/app.py`. It uses the same `PatternEditor` facade and the same
  `edit_request_from_values` builder, so both frontends enforce identical rules.
- It shows the same history/badges, source/documentation/diff, saved-preset
  selector with the replay settings, live progress, diagnostics and a
  candidate-vs-base comparison, plus the same four lifecycle actions.
- Every service call runs on a background thread; results are delivered through a
  `queue.Queue` drained on the Tk thread. Worker threads never touch Tk directly.
- Closing the window only stops that window's polling — it never marks a durable
  job cancelled or completed. The last job id is remembered by the app, so
  reopening the dialog reconnects to the same durable job.

## Evidence

- Web tests: `core/test_patterns_web.py` (auth, navigation, catalog/versions/
  source/diff, submit→generate→validate→backtest, duplicate Submit,
  reload reconnect, failed validation, block+retry without a model call,
  backtest-only retry, cancellation, expired lease, default/archive with
  replacement and conflict, malformed requests, database unavailable).
- Desktop tests: `core/test_patterns_desktop.py` (one request builder for both
  frontends, offscreen submit→results, closing during a job keeps the durable
  state, reopen reconnects, PostgreSQL error surfacing, stale-generation conflict
  when another frontend changes the default).
- Smokes: [P09 smoke](verification/pattern-editor-p09-smoke.txt) (real
  `main.py --web`) and [P10 smoke](verification/pattern-editor-p10-smoke.txt)
  (real `main.py --ui` toolbar → dialog → submit → blocked → reopen reconnect).
  See the tracker for the recorded gate commands.

## Not performed / gates

- Live DeepSeek calls and a real candidate sandbox remain unconfigured here.
- Full integration/release rehearsal (backup/restore, restart, concurrency,
  cutover) is P11.

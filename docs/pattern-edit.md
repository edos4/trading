# AI-assisted pattern editing

Status: agreed feature requirements and proposed implementation design. This document does not implement the feature.

## Purpose and agreed behavior

Edit a pattern's general detection logic directly from a paper-trading chart using DeepSeek-V4.1-Flash. A candle selection, dragged point, or chat instruction becomes an example of the desired rule change across symbols. Every proposal must include an executable preview, plain-language explanation, code diff, and validation results before the user clicks **Apply**.

Support charts opened from both open and closed paper trades. Provide a chat panel beside the chart, candle selection, draggable pattern points, and visible instructions. AI may change detection, drawing, entry, exit, stop-loss, and associated target rules. Maintain independent versions for each pattern, with descriptions and rollback. New versions govern future signals; existing positions retain their original version and exit rules.

All of these capabilities are required in both `python main.py --ui` (the native Tkinter desktop application) and `python main.py --web` (the browser dashboard). Desktop editing must work directly inside its trade chart window without starting the web server or opening a browser.

## Existing code and integration points

| Area | Current implementation | Required extension |
| --- | --- | --- |
| Desktop entry point and paper desk | `main.py` → `ui/app.py` → `ui/paper_dashboard.py`; open/closed rows already bind `<Double-1>` | Preserve this route and expose the complete editor from desktop trade charts |
| Desktop interactive chart | `ui/tv_chart.py`; `open_trade_viewer` creates a Tk Toplevel containing a native Canvas-based `TradingViewChart` | Native context menu, point dragging, chat panel, preview, explanation/diff, validation, Apply, and history |
| Paper trade chart | `web/templates/paper.html`, `web/static/app.js`; double-click calls `/api/paper/chart` | Add Edit Pattern interaction, editor panel, preview, version history |
| Interactive chart | `web/static/tv_chart.js`; Lightweight Charts wrapper draws candles, markers, levels, and segments | Candle context menu, point identities, dragging, image capture, synchronized before/after views |
| HTTP application | `web/app.py`; FastAPI and existing authentication | Authenticated edit-session, validation, apply, and rollback endpoints |
| Trade chart data | `core/paper_books.py`; uses stored trade annotations | Include stable trade identity, pattern identity/version, data cutoff, and editable anchor metadata |
| Pattern source | `patterns/*.py` and corresponding `.md` rule descriptions | Version code and documentation together |
| Pattern interface | `patterns/base_pattern.py`; `BasePattern`, `TradeSignal`, annotation builders | Carry version identity and stable semantic anchor identifiers |
| Discovery and execution | `ui/app.py`, `web/services.py`, `core/scanner.py`, `core/pattern_jobs.py`, `core/backtester.py` | Resolve immutable pattern versions consistently and refresh cached instances safely |
| Static chart rendering | `analysis/chart_renderer.py` | Reuse where appropriate for reproducible chart images |

For example, `008_head_and_shoulders.py` calculates left shoulder, head, right shoulder, neckline, and trade rules. It emits LS, HEAD, RS, and other annotations. A right-shoulder correction must change the relevant selection logic and regenerate these annotations by running the candidate detector.

Some patterns use shared helpers such as `_rules.py` and `_dedup.py`. A version must record its dependency hashes and preserve the dependencies needed to reproduce its behavior. Ordinary edits should stay within the selected detector and its Markdown file. If a shared helper needs modification, show the expanded scope and validate affected patterns; never silently alter every pattern through a helper edit.

## User instructions and interaction

Show these instructions in the editor, with a persistent **How to edit** control:

1. Double-click an open or closed paper trade to open its chart.
2. Right-click the candle you want to reference and choose **Edit Pattern**.
3. Use the chat panel to describe the change, for example: “This should be the point where the right shoulder is.” The selected candle's date and OHLC values appear beside the message box.
4. Select additional candles or drag a labeled pattern point onto another candle. Assign its role, such as Right Shoulder, when needed. Dragging adds a proposed correction to the conversation; it does not save a version.
5. Click **Generate Preview**. Review the updated chart, explanation, code diff, and test results.
6. Continue chatting or moving points to refine the proposal. Every revision receives a fresh preview and validation.
7. Optionally run **Broader Comparison** against other historical examples.
8. Click **Apply** to activate the reviewed version, or **Discard** to leave the active version unchanged.
9. Open **Version History** to compare versions and preview a rollback.

For desktop users, start `python main.py --ui`, click **Paper Trading**, and follow these steps in the native chart window. For web users, open the paper dashboard and follow the same steps. Both interfaces must display these instructions and provide the same review and approval controls.

The panel must explain that corrections change general pattern behavior across symbols. Include examples for both geometry (“Prefer this later peak as the right shoulder”) and trade rules (“Place the stop above the right shoulder high”).

Identify candles by symbol, market, timeframe, timestamp, and dataset index, not screen coordinates alone. Distinguish candle high/low/open/close from an arbitrary selected price. A drag should expose its snap target and resulting value. Temporarily suspend chart panning while dragging. Offer point selection and candle selection as an alternative to dragging.

Show the selected pattern, the historical trade version, and the current active version. When a trade predates the active version, default edits to the current active code and explicitly show that the original chart came from an older version. Preserve the original annotations for comparison. If several patterns are present, require selection of the intended pattern.

If a requested anchor conflicts with other rules, AI should explain the conflict and propose the rule changes required. It must not merely move the displayed marker or hardcode a symbol, date, or candle index into the detector.

## AI context and model integration

Use DeepSeek directly for both image understanding and code editing. DeepSeek's September 10, 2026 release identifies V4.1-Flash as natively multimodal and specifies the API model name `deepseek-flash`. Use that configurable API identifier while displaying **DeepSeek-V4.1-Flash** in the UI. Record the requested model ID and provider-reported model information in each revision. See the [official release](https://www.deepseek.com/en/news/deepseek-v4-1-flash/) and [vision API guide](https://api-docs.deepseek.com/guides/vision/).

Keep API credentials server-side. Proposed configuration: `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, and `PATTERN_EDIT_MODEL`. Validate image requests against the provider's current API contract during implementation; do not silently fall back to text-only analysis or another provider.

For each generation request, assemble:

- User instructions, conversation history, selected candles, and dragged anchors.
- The actual visible chart image including pattern overlays and selection highlights, plus a wider pattern-context image when the viewport omits relevant points.
- Structured OHLCV data, indicator values, timeframe, timezone, data hashes, and exact selection coordinates in market data.
- Selected pattern source, Markdown rules, relevant shared helpers, base interfaces, and relevant tests.
- Base version, original trade annotations and trade rules, current candidate changes, and previous validation feedback.

Use images for visual interpretation and structured values for exact prices and timestamps. Distinguish post-entry chart context from data available when the signal was generated. Do not allow future candles to become inputs to historical detection tests.

Require a structured response containing proposed file edits, a concise version description, a rule-by-rule explanation, intended anchor changes, and any unresolved questions. Parse and validate the response before evaluating code. The AI may update rules previously labeled LOCKED when requested changes require it; explicitly identify those changes in the explanation and update the paired Markdown documentation. Apply is the approval step for the complete proposal.

## Drafts, preview, and validation

Keep candidate code in an isolated draft workspace. Generating or revising a draft must not modify active pattern files or influence paper scans. Execute generated Python in a restricted worker with no provider credentials, broker access, or network access, with time and resource limits. A subprocess by itself is not adequate isolation.

Preview must run the proposed detector and render its actual output. Show original, current baseline, and proposed results with clear labels; a synchronized before/after toggle or side-by-side view is acceptable. Display changed anchors and entry, stop, target, and exit behavior. If the candidate no longer detects a pattern, show that outcome honestly and explain the failed conditions when available.

Required validation before Apply:

- Syntax/import and `BasePattern`/`TradeSignal` compatibility checks.
- Deterministic replay on the selected chart data, using appropriate historical cutoffs and the normal scan path.
- Relevant existing pattern, overlay, and paper-trading tests, plus a regression example for the requested correction where practical.
- Checks that code, rule documentation, annotations, and signal fields agree.
- Checks against future-data leakage and hardcoded example-specific detection.

Use the selected candle as the expected anchor when the user's instruction explicitly establishes that expectation. Model-written assertions alone are not evidence of correctness. Show failures, unavailable data, and unverified expectations; block Apply for required checks that failed or could not run.

**Broader Comparison** is optional and user-triggered. Let the user choose symbols, date range, and timeframes, then compare the base and candidate on identical historical data. Report added/removed signals, anchor changes, entry/stop/target changes, trade count, and available backtest outcome metrics. Keep this separate from mandatory selected-chart validation and label when it has not run.

Any candidate edit invalidates its previous approval preview and validation. Apply must reference the exact candidate hash and successful validation report shown to the user.

## Version storage and activation

Use immutable per-pattern snapshots and a durable registry outside the dynamically discovered `patterns/` package. A proposed layout is `pattern_versions/<pattern_id>/<version_id>/` for source, rule documentation, dependency manifest, and metadata, with a transactional registry holding the active version and edit sessions. Keep chart datasets and images as referenced, hashed artifacts. Initialize a baseline snapshot before the first edit.

Each applied version records:

- Pattern ID, monotonically increasing version number, immutable version ID, parent version, timestamps, and actor.
- Editable plain-language description and AI-generated explanation.
- Original user instructions and references to conversation, selected candles, and chart artifacts.
- Complete source/document snapshots, dependency hashes, and code diff.
- Provider/model information, candidate hash, validation report, and optional broader comparison results.
- Activation state and, for rollback, the version being restored.

Example description: “Select a later confirmed right shoulder after the right neckline trough; set the initial stop above its high.”

Apply checks that the active version still matches the draft's base version. If another edit or external file change occurred, require a refreshed candidate and preview instead of overwriting it. Serialize activation per pattern and make retries idempotent.

Stage the complete version before atomically changing its active registry reference. Resolve executable code through that reference. Keep `patterns/<file>.py` and its `.md` as the active source mirror, with journaled recovery so interrupted updates cannot leave mixed versions. Detect manual source edits through hashes and require reconciliation before further activation.

Existing discovery uses Python imports and cached pattern instances. Editing a file alone will not reliably update running consumers. Introduce version-aware loading for scanner processes, pattern workers, backtests, the desktop explorer, and the web explorer. Switch at a defined scan boundary; each in-progress job retains the version it started with. Show activation as pending until the relevant paper workers acknowledge it. Clear or namespace detector deduplication state by version.

Persist `pattern_version_id` and the complete resolved exit-rule snapshot on signals, open positions, closed trades, exports, and signal logs. Existing positions continue with their entry-time rules after Apply or rollback. If an exit rule requires executable code, retain and resolve that code by the position's version. Do not rewrite historical fills or outcomes. Mark older trades without provable version information as legacy/unknown rather than claiming they used the new baseline.

## Version history and rollback

Version History shows version number, date, description, active status, validation summary, and links to the explanation and diff. Allow comparison of any two versions.

Selecting **Rollback** previews restoration of the chosen version, its documentation, and required dependencies. Run compatibility checks and selected-chart validation before confirmation. Confirmation creates a new version recording the restoration and reason; it never deletes intervening history. Activate through the same mechanism as Apply. Existing positions remain pinned to their original rules.

## Proposed backend workflow

Add a UI-independent pattern-edit service, a DeepSeek adapter, a version registry/loader, and an isolated validation worker. Place shared orchestration under `core/` or another framework-independent package. The desktop calls this service directly; FastAPI routes adapt HTTP requests to the same service. Core editing operations must not depend on a running FastAPI application, HTTP session, or `WEB_UI_PASSWORD` for desktop use. Reuse existing web authentication for HTTP access and existing background-job conventions where compatible.

Both interfaces use the same session format, validation rules, active-version registry, and activation locks. When they use the same workspace, applied versions and saved drafts are visible to both. Synchronization must work across processes, not just through an in-process lock.

| Operation | Proposed route |
| --- | --- |
| Create session from trade/chart | `POST /api/pattern-edits` |
| Read conversation, draft, and job status | `GET /api/pattern-edits/{id}` |
| Submit instruction and anchor changes | `POST /api/pattern-edits/{id}/messages` |
| Generate revised candidate and preview | `POST /api/pattern-edits/{id}/preview` |
| Run broader historical comparison | `POST /api/pattern-edits/{id}/compare` |
| Apply exact validated candidate | `POST /api/pattern-edits/{id}/apply` |
| Discard draft | `DELETE /api/pattern-edits/{id}` |
| Read version history | `GET /api/patterns/{pattern_id}/versions` |
| Create rollback draft for preview/Apply | `POST /api/patterns/{pattern_id}/rollback` |

Generation and comparison return job IDs and expose progress, cancellation, and actionable failures. Persist sessions so closing the chart or restarting the web process does not lose drafts. Authorize every mutation, validate source paths server-side, and keep active-code writing exclusive to the activation service. API failures and invalid model responses leave the active version intact and permit retry.

## Native desktop implementation requirements

Extend `ui/tv_chart.py` with editing callbacks and add a native editor panel, for example in `ui/pattern_edit_panel.py`. Have `ui/paper_dashboard.py` pass trade identity and pattern/version context alongside the existing `paper_books.chart(...)` payload. Keep non-editor uses of `TradingViewChart`, such as the Kronos dialog, working through optional editing hooks.

Bind a platform-appropriate right-click context menu to real candles in the price plot. The existing `_index_at` returns a viewport-relative index; combine it with `_start` to resolve the actual candle after zooming or panning, and check the pointer's vertical plot bounds. Exclude predicted Kronos candles from editable historical anchors. Preserve pan/zoom and crosshair behavior outside point-drag mode.

Provide chat input, selected-candle details, draggable anchors, visible instructions, preview controls, a scrollable explanation and code diff, test results, broader comparison, Apply/Discard, and version history in Tk widgets. The user must be able to finish the entire workflow inside the desktop application.

Capture the actual desktop chart and selection highlights as an image for DeepSeek. Implement chart-only capture or a deterministic image export matching the Canvas viewport, overlays, and selection state; verify that image against the visible chart. Do not assume the browser's image-capture implementation works for Tk Canvas. Use the same structured market-data context as the web editor.

Run network requests, generation, comparisons, and validation outside Tk's main thread. Deliver progress and results through a queue polled with `after`; only the main thread may update widgets. Preserve responsive chart navigation, prevent duplicate Apply, and handle cancellation or window closure without callbacks touching destroyed widgets. Retain saved drafts across desktop restarts and reload activation status when a chart reopens.

## Acceptance criteria

1. Open and closed paper-trade rows support double-click → candle right-click → Edit Pattern.
2. The editor provides instructions, multi-message chat, candle selection, and draggable labeled points.
3. AI receives chart images, exact candle data, and relevant pattern code, then proposes a general rule change.
4. Preview shows the detector's actual updated output, explanation, complete code/document diff, and validation results before Apply.
5. Detection and entry/exit/stop changes are supported; broader historical comparison is available.
6. Drafting has no effect on active scans. Apply activates exactly the validated candidate and exposes worker activation status.
7. Version descriptions, immutable snapshots, comparisons, and rollback survive restart.
8. New signals use the activated version; open positions and historical trades preserve their original rules and provenance.
9. Tests cover candle mapping after zoom/pan, drag behavior, candidate failures, stale drafts, activation recovery, rollback, and version-pinned exits. Use a mocked provider for automated tests and a separate configured-provider smoke test for image input.
10. Run an end-to-end desktop check from `python main.py --ui` → Paper Trading for both open and closed rows, covering chat, candle selection, point dragging, image context, preview, diff, validation, Apply, broader comparison, history, and rollback with no web server running.
11. Verify desktop responsiveness during slow generation, cancellation/window-close handling, and saved draft recovery. Test native controls under a display-enabled environment, using a virtual display where needed; web browser tests alone do not establish desktop support.
12. Verify that a version applied in either interface is observed by the other when sharing a workspace, and that concurrent edits receive the same stale-base protection.

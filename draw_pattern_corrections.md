# Draw pattern corrections

Status: implementation plan only.

## Goal

After double-clicking a trade or signal row from `python main.py --ui`, `python main.py --web`, or web Replay, draw the intended pattern on the opened chart and submit those drawings with a correction instruction to the existing pattern editor.

Interpretation: a correction supplies visual evidence for a proposed detector change. Drawing alone does not modify detector code, historical trade records, or the active default pattern version.

## Existing implementation

| Surface | Entry point | Shared chart |
| --- | --- | --- |
| Desktop paper dashboard: open positions, closed trades, signal log | `ui/paper_dashboard.py::_open_chart` | `ui/tv_chart.py::open_trade_viewer` / `TradingViewChart` |
| Web paper: open positions, closed trades, signal log | `web/static/app.js::openTradeChart`, `/api/paper/chart` | `web/static/tv_chart.js::mount` |
| Web Replay: open and closed trades | `web/static/app.js::openReplayTradeChart`, `/api/replay/chart` | Same web chart |
| Pattern editor detection results | Desktop and web Patterns screens | Existing shared viewers; preserve compatibility |

Both viewers already support Trend, VLine, Undo, and Clear, including price/RSI endpoints. Web shapes live inside `ChartDraw.attach`; desktop shapes live in `_drawings`. These are temporary drawings, not correction inputs.

Both viewers also have a right-click **Edit pattern…** action. Desktop passes `_payload` to `PatternsDialog`; web writes the original chart payload into `sessionStorage` and opens `/patterns?chart=...`. Neither handoff includes current manual drawings. `ChartEditContext` currently has no manual-correction field, so adding client data alone will not deliver it to the provider.

The existing edit service already serializes validated chart context into `trade-chart.json`, stores the edit request, generates an immutable candidate version, validates it, and backtests it against its base. Reuse that route.

Scope is the interactive row-opened viewers above. The symbol explorer's static image chart is a separate surface; replacing it is not required for this work. Replay refers to the existing web Replay page; desktop paper sessions driven by replay data use the same desktop viewer.

## User workflow

1. Double-click a row and inspect the existing detected pattern.
2. Choose **Draw pattern** to click consecutive turning points. Finish with Enter or a Finish button; Escape cancels the unfinished shape. Keep Trend and VLine for rails, necklines, and timing marks.
3. Select a drawn shape to adjust its points, delete it, or give it an optional short label such as “left shoulder”, “neckline”, or “correct breakout”. Undo and Clear apply to manual drawings only.
4. Click a visible **Correct pattern…** button. Keep the right-click action as an equivalent shortcut. Show a preview and drawing count alongside the existing instruction and base-version controls.
5. Enter a correction instruction, for example: “Use the drawn peaks and neckline; the detected right shoulder is too early.” Review the selected base version and backtest settings, then submit through the existing editor.
6. Review the resulting candidate and comparison results. Promotion remains the existing explicit default-version action.

Original detector overlays and user corrections must have distinguishable styles and labels. Pan/zoom works normally outside drawing mode. Keep corrections attached to bars while resizing, zooming, and scrolling.

## Implementation steps

### 1. Add a shared correction contract

Extend `core/pattern_editor_contracts.py::ChartEditContext` with an optional, default-empty `manual_corrections` collection. Use a small discriminated shape schema:

- Trend: two endpoints, each containing actual bar time, finite value, and pane (`price` or `rsi`). Preserve existing cross-pane lines.
- Pattern: an ordered polyline of two or more price-pane endpoints; this is the multi-point pattern drawing tool.
- VLine: one actual bar time, spanning panes.
- Each shape: a local identifier and optional bounded label.

Use timestamps matching chart candle times, never pixels or desktop bar indices. Proposed limits: 100 shapes per request, 100 vertices per pattern, and 200 characters per label. Validate shape types, point counts, panes, finite values, and membership in the actual candle series. Require increasing bar times for pattern vertices. Forecast-only points cannot be submitted as historical correction evidence; show a specific validation message rather than silently dropping them.

Keep manual corrections separate from detector `segments`, `markers`, `levels`, and replay `chart_annotations`. Old chart contexts with no corrections must still validate. Carry the existing symbol, market, timeframe, pattern, and pattern-version provenance through unchanged.

### 2. Extend the existing drawing tools

In `web/static/chart_draw.js`, expose snapshot/load methods and a change callback in addition to `destroy()`. Return independent copies so a submitted snapshot cannot change when the user draws again. Extend the existing primitive renderer and undo history for polylines, selection, point dragging, labels, and deletion; do not add a drawing library.

In `ui/tv_chart.py`, add the same interactions to the existing canvas. Convert its internal bar indices to actual candle times when exporting and resolve times on import. Reset drawing state when switching to a different chart payload.

In `web/static/tv_chart.js`, retain the drawing handle for the lifetime of the mounted chart. Use actual candle times for drawing anchors rather than deriving the time list from RSI data, which can omit initial bars. Detach primitives/listeners and cancel unfinished gestures on unmount or pointer cancellation. Scope keyboard shortcuts to the active viewer; Escape should cancel drawing before a modal consumes it.

Use ordinary Tk controls and HTML buttons for discoverable, keyboard-accessible actions. No preset library for every pattern type is needed: a polyline plus labeled trendlines covers corrections to peaks, troughs, shoulders, channels, and necklines.

### 3. Connect charts to the correction editor

Build a fresh chart-context snapshot when the correction action is clicked, including current manual corrections. Both the visible button and context menu must use this same path.

- Desktop: pass the snapshot into `ui/patterns_dialog.py::PatternsDialog`.
- Web and Replay: use the existing session-storage handoff from `web/static/tv_chart.js` into `web/static/patterns.js`.
- In both editors: show correction count, labels, and a preview; preserve the snapshot on validation or submission failure. Keep drawing data when editing instruction text or backtest settings.
- If the chart lacks a pattern identity, retain drawing functionality but explain why submission needs a target pattern. If version provenance is missing or unavailable, require explicit base-version selection; never silently redirect a correction to today's default.
- If the user switches the target pattern, require an explicit context reset so drawings from one pattern cannot accidentally correct another.

Preserve drafts across chart close/reopen within the current application session, keyed by stable chart/trade identity and version. Use existing IDs where available, not table row numbers; include a replay dataset/session discriminator to prevent collisions after a new upload. Where a stable identity is unavailable, warn before discarding a dirty draft. Submitted corrections are durable through the existing stored edit request; cross-device draft synchronization is outside this first implementation.

### 4. Verify the existing backend handoff

Trace `web/app.py::PatternEditRequest` and `/api/patterns/edits`, `core/pattern_editor_api.py::edit_request_from_values`, and the desktop submission path to ensure the extended context survives validation and request persistence.

Reuse `core/pattern_edit_service.py::_generate` and its `trade-chart.json` attachment. Explain in the provider context that manual corrections describe the user's intended geometry and labels, while detector overlays describe the original output. A drawing is evidence for the user's instruction, not an executable command or an automatic training update.

No new correction endpoint, database table, image-upload service, or provider is planned. Verify that the stored request JSON round-trips the added field before concluding that no migration is required. Preserve authentication, bounded inputs, immutable version pinning, candidate validation, and the existing isolated execution boundary.

For replay, corrections refer to the chart's actual returned candle snapshot. Preserve original detection annotations/version and available replay cutoff provenance; do not substitute a fresh scan or treat later visible bars as information available at the historical signal time.

### 5. Validate the complete workflow

Extend the relevant existing tests rather than creating a second test infrastructure:

- `core/test_pattern_editor_contracts.py`: old payload compatibility, valid geometry, malformed/oversized shapes, non-finite coordinates, and unknown/forecast-only timestamps.
- `core/test_pattern_edit_pipeline.py`: persisted request and provider `trade-chart.json` contain exactly the submitted corrections; submitting does not change the default version.
- `core/test_patterns_desktop.py` and `core/test_patterns_web.py`: current drawing snapshot reaches the editor and submission; unavailable version requires explicit selection.
- `web/test_replay_client.cjs` and `web/test_replay_flow.py`: replay row double-click preserves corrections and provenance through the shared chart/editor route.
- Extend the drawing checks in `analysis/test_pattern_overlays.py`; add a small Node check beside existing client checks if needed for snapshot isolation, early candle anchors, and drawing lifecycle cleanup.

Manual acceptance matrix:

| Launch/surface | Rows to exercise | Required result |
| --- | --- | --- |
| `main.py --ui` paper dashboard | Open, closed, signal log | Draw, adjust, undo, preview, and submit the selected row's correction |
| `main.py --web` paper dashboard | Open, closed, signal log | Same workflow through the web editor |
| Web Replay | Open and closed rows; reload and new upload | Same workflow without leaking drawings between datasets |

For each surface, verify pan/zoom/resize anchoring, price and RSI lines, multi-point pattern drawing, visible correction action, correct version selection, and retained drafts after a failed submission. Test two rows of the same symbol to catch identity collisions. Check that imported detections and forecasts remain distinct from corrections.

## Completion criteria

- Every supported row-opened chart exposes pattern drawing and a visible correction action.
- Drawings arrive as validated structured geometry in the saved edit request and provider context.
- Users can inspect the submitted geometry and instructions before generation.
- Existing unannotated edits still work, and detector overlays/trade history remain intact.
- Candidate generation, validation, comparison, and explicit promotion use the existing workflow.
- Relevant automated checks and the desktop/web/replay manual matrix pass.

## Delivery order

Implement contract and serialization first, then complete the shared web viewer and replay handoff, then desktop parity, then the end-to-end checks. This document authorizes no implementation itself; it records the requested plan.

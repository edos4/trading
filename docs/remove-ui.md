# Remove the desktop UI (`main.py --ui`), keep the web UI (`main.py --web`)

Status: implemented
Branch: `remove-ui`
Decisions taken: **thorough** collateral cleanup, **hard remove** the `--ui` flag.

---

## Goal

Delete the tkinter desktop application (`ui/`, ~4,024 lines in 6 modules) and every
reference to it, leaving `python main.py --web` (FastAPI + uvicorn, `web/`) fully
functional. `--web` must be unaffected at import time, at runtime, and in tests.

Target end state:

```bash
python main.py --ui    # → argparse: unrecognized arguments: --ui  (exit 2)
python main.py --web   # → unchanged: uvicorn on WEB_UI_HOST:WEB_UI_PORT, login required
```

---

## Why this is safe (exploration findings)

- **Nothing outside `ui/` imports `ui/` in production.** The only production import site is
  `main.py:865`, inside `if _args.ui:` under `if __name__ == "__main__":`. The import is lazy,
  so `import main` never loads tkinter.
- **`web/` has zero imports from `ui/`.** `web/services.py` is a *re-implementation* of the
  explorer flows (its docstring at `web/services.py:1` says "mirrors tk ui/app.py flows"), not a
  dependency. `web/app.py` imports only fastapi/starlette/pydantic + `core`/`data`/`analysis`.
- **All shared code was already extracted out of `ui/`.** `core/backtest_params.py`,
  `core/backtest_service.py`, `core/pattern_editor_api.py`, `core/paper_books.py` are the shared
  layer; `ui/backtest_dialog.py:23` even carries `# noqa: F401 - shared with headless web`,
  importing *from* core. `core/backtest_params.py:1-7` documents that headless web startup never
  imports Tkinter. **No file needs to be moved — only deleted.**
- **No deploy surface.** `deploy/` has only `trading-web.service`, `trading-deploy.service`,
  `hermes-dashboard.service` — all web. No nginx/docker/supervisor/CI/Makefile entries mention
  the desktop app. The tkinter app binds **no port**.
- **The only real couplings are:** the `--ui` flag + dispatch in `main.py`, `run_ui.sh`,
  `requirements.txt` (`tkcalendar`), `install.sh` (`python3-tk` + smoke test), 7 test modules, and
  docs/comments.
- **Trap to avoid:** `WEB_UI_PASSWORD`, `WEB_UI_HOST`, `WEB_UI_PORT`, `WEB_UI_USERS`,
  `WEB_UI_SECRET_KEY`, `WEB_UI_SESSION_HOURS` all say "UI" but belong to `web/`. **Do not rename
  them.** Likewise `data/history.py:44 enable_ui_web_history()` is still called by
  `web/app.py:1486` — **do not rename or delete that function.**

---

## Step 1 — Delete the desktop application

```bash
rm -rf ui/
```

This removes `ui/__init__.py`, `app.py` (594 L), `backtest_dialog.py` (744 L),
`paper_dashboard.py` (805 L), `patterns_dialog.py` (860 L), `tv_chart.py` (1020 L), and
`ui/__pycache__/` — which holds orphan `.pyc` files whose sources no longer exist
(`kronos_dialog`, `test_pattern_edit_panel`).

```bash
rm run_ui.sh
```

`run_ui.sh` is 2 lines (`source .venv/bin/activate` / `python main.py --ui`) and exists only for
the desktop app.

---

## Step 2 — Unwire `main.py`

### 2a. Remove the flag — `main.py:606-610`

Delete the whole `parser.add_argument("--ui", ...)` block.

### 2b. Remove the dispatch branch — `main.py:860-877`

Becomes (note the branch order collapses cleanly; `--web` keeps precedence over `--mcp`):

```python
if __name__ == "__main__":
    # --web / --mcp own their own event loop (uvicorn / stdio). Running them
    # inside asyncio.run(main()) nests asyncio.run and crashes uvicorn.
    _args = _parse_args()
    if _args.web:
        from web.app import run as run_web

        run_web()
    elif _args.mcp:
        from mcp_server.server import run as run_mcp

        run_mcp()
    else:
        raise SystemExit(_run_async_entry(_args))
```

### 2c. Docstring / help touch-ups in the same file

| Line | Change |
|---|---|
| `main.py:19` | delete `python main.py --ui  # Launch the symbol explorer GUI` |
| `main.py:39` | `(paper: 7497, live: 7496) - not required for --ui` → `- not required for --web/--backtest` |
| `main.py:644` | `so paper trading (--paper / --ui) can run` → `so paper trading (--paper) can run` |
| `main.py:861-862` | the comment is rewritten by 2b |

Leave `async def main()` untouched — it never inspects `args.ui` (verified), so nothing else in
`main.py` depends on the flag.

---

## Step 3 — Launcher, installer, requirements

### `requirements.txt:20`
Delete `tkcalendar>=1.6.1` — its only consumer is `ui/paper_dashboard.py:19`.
**Keep** `matplotlib` (line 15) and `Pillow` (line 16): both are used by `analysis/chart_renderer.py`,
which `web/services.py:9` calls for PNG charts.

### `install.sh`
- Line ~59: delete the comment `# python3-tk: required for \`python main.py --ui\` (tkinter + tkcalendar)`.
- Line ~64: remove `python3-tk` from the apt package list.
- Line 106: remove `run_ui.sh` from the `chmod +x` list.
- Lines 170-174: delete the tkinter verification block
  (`try: import tkinter ... except ... "install python3-tk for --ui"`). Keep the rest of the
  verify script intact.
- Line ~184: delete `echo "     Desktop explorer (needs X):     ./run_ui.sh    # needs python3-tk"`.

### No changes needed
`run_web.sh`, `run_mcp.sh`, `paper_stream.sh`, `run_deploy.sh`, `deploy_to_contabo.sh`,
`deploy_to_engine.sh`, `deploy/*.service`, `.gitignore`. (Only `install.sh` enumerates `run_ui.sh`.)

---

## Step 4 — Tests

### 4a. Delete outright (pure desktop suites)

| File | Note |
|---|---|
| `core/test_patterns_desktop.py` | 415 L — **carve out one test first, see 4b** |
| `analysis/test_desktop_trendlines.py` | 266 L — **carve out three tests first, see 4b** |

### 4b. Carve out the non-UI tests before deleting the files

These two files mix shared-payload coverage (which `--web` relies on) with Tk-widget coverage.
Preserve the good half.

`core/test_patterns_desktop.py:65-92` → `test_desktop_and_web_build_the_same_edit_request` is
**display-free** and asserts `edit_request_from_values()` produces the same `settings` as the web
path. Move it into a new `core/test_pattern_edit_requests.py` together with the `desktop_editor`
fixture (`core/test_patterns_desktop.py:32-44`, which depends on root-level fixtures
`published_pattern_catalog`, `edit_doubles`, `sandbox_double` in `conftest.py`) and the `rows()` /
`base_version()` helpers. Then delete `core/test_patterns_desktop.py`.

`analysis/test_desktop_trendlines.py` — three tests use no Tk widgets; move them into
`analysis/test_chart_viewer_payload.py`:

- `test_detection_chart_enables_rails_using_only_the_detection_tape` (L92-120) — `PatternEditor` +
  `build_trade_viewer_payload`, real frozen barcache tape.
- `test_weekly_anchor_mapping_preserves_labels_and_rejects_ambiguous_or_missing_week` (L227-239) —
  `analysis.chart_renderer._weekly_chart_time`.
- `test_legacy_weekly_rounding_marker_is_aligned_before_geometry_recovery` (L242-266) —
  `build_trade_viewer_payload`.

Then delete `analysis/test_desktop_trendlines.py`.

### 4c. Surgical edits in mixed files

| File | Change |
|---|---|
| `analysis/test_chart_viewer_payload.py:333-346` | delete `test_desktop_pattern_segment_interpolates_when_panned` (only Tk test in the file; the other 14 tests are pure payload/renderer/indicator coverage — keep them) |
| `analysis/test_pattern_overlays.py:102-121` | `test_curve_draws_multiple_legs_when_panned_on_desktop_and_png` — drop the `TradingViewChart` half, keep the `ChartRenderer()._draw_annotations(axis, df, [ann])` assertions; rename to `test_curve_draws_multiple_legs_in_png` |
| `analysis/test_pattern_overlays.py:237-357` | delete the six `test_desktop_*` tests (`label_prints_the_reason_under_it`, `markers_hand_their_reason_to_the_label`, `trendline_clips_to_the_visible_window`, `trendline_can_span_price_and_rsi_panes`, `pattern_refuses_forecast_bars`, `drawings_use_the_pane_under_the_cursor`). Keep L1-236 and L358-end (`test_generated_version_connects_saved_pivots_without_executing_detector` is pure) |
| `data/test_pattern_client.py:155-195` | delete `test_remote_desktop_editor_uses_existing_web_endpoints` — it exists only to exercise `RemotePatternEditor` via `PatternsDialog`, both of which go away (see Step 5) |
| `patterns_v2/test_patternsv2.py:272-284` | `test_checkboxes_exist_on_web_and_desktop` → `test_checkboxes_exist_on_web`: delete the two `read_text()` lines for `ui/app.py` / `ui/paper_dashboard.py` and the two `text="Patternsv2"` assertions; keep the web/template/JS assertions |
| `indicators/test_indicators.py:352-360` and `:363-371` | in the two checkbox tests, delete `Path("ui/paper_dashboard.py").read_text()` / `Path("ui/app.py").read_text()` and the `text="Indicators only"` assertions; keep the `web/templates/*.html` + `web/static/app.js` assertions |

Without these edits the `read_text()` tests fail with `FileNotFoundError` (not `ImportError`),
so a plain `pytest` run surfaces them as confusing failures rather than obvious missing-module
errors. Do them in the same commit.

### 4d. Keep as-is

- `core/test_backtest_frontends.py:265 test_headless_web_imports_do_not_require_tkinter` — still
  meaningful as a regression guard that `web.app`/`web.jobs`/`web.runs` never pull Tkinter.
  (Its sibling test around `:240-262` uses local variables named `desktop` while testing
  `BacktestRequest` parity — harmless stale wording, optional rename.)
- `data/test_history.py:419,422` — assertions about Yahoo not being used; only their message
  text says `--ui/--web`. Update the wording, keep the assertions.
- Everything under `tests/` — verified zero references to `ui`, `tkinter`, `--ui`, `run_ui`.

---

## Step 5 — Dead production code (thorough cleanup)

| Target | Why it is dead | Action |
|---|---|---|
| `data/pattern_client.py:227-289` `class RemotePatternEditor` | Used **only** by `ui/patterns_dialog.py:138-140`. `web/patterns.py:21` builds a local `PatternEditor()`; the VPS read path uses `core.remote_pattern_store.RemotePatternStore`, which calls the module-level `fetch_pinned` / `fetch_version_bundle` / `upload_run` / `fetch_run` — **keep all of those**. | Delete the class (it is the file tail). Drop the now-unused `from urllib.parse import quote` (line 21) — `quote` appears only inside that class. |
| `utils/trade_display.py` (158 L) + `utils/test_trade_display.py` (66 L) | Only consumer is `ui/paper_dashboard.py:24`. `core.market.format_money` is the shared formatter used by web and stays. | Delete both files. |
| `requirements.txt:20` `tkcalendar` | `ui/paper_dashboard.py:19` only. | Delete (also in Step 3). |

Verify after: `grep -rn "RemotePatternEditor\|trade_display\|tkcalendar" --include=*.py --include=*.txt . | grep -v .venv`
should return nothing.

---

## Step 6 — Comment / docstring sweep (comment-only, no behaviour change)

Comments that say `--ui / --web` must become `--web` only:

- `config.py:184-185`, `config.py:204`
- `data/history.py:45, 73, 76, 81, 321`
- `data/tv_client.py:787`
- `data/stream_server.py:690`
- `data/test_history.py:419, 422` (message strings)
- `scripts/build_barcache.py:42`
- `core/paper_books.py:3` (`--ui and --web both drive this manager` → `--web drives this manager`)
- `core/engine_defaults.py:51` (`The Explorer UI (ui/app.py, web/services.py)` → `web/services.py`)
- `core/paper_trader.py:336` (`(see ui/paper_dashboard.py)` → drop the parenthetical)
- `web/services.py:1` (`mirrors tk ui/app.py flows` → `Explorer data/pattern/chart helpers`)
- `web/app.py:1432` (`mirrors the tkinter UI datepicker` → `mirrors the web paper-tab datepicker`)
- `.env.example:253-254`

**Do not touch:** `data/history.py:44 enable_ui_web_history()` (function name, still used by
`web/app.py:1486`), any `WEB_UI_*` setting, or `mcp_server/*` descriptions of the web UI.

---

## Step 7 — User-facing docs

### `README.md`
- `:52-54` — "Both run in CLI ... and in the **`--ui` explorer, Backtest dialog, and Paper Trading
  dashboard**" → reword to the web UI only.
- `:246-248` — drop the three `python main.py --ui → symbol explorer` rows from the volume-gate table.
- `:275-283` — delete `## Symbol Explorer UI` → `### Desktop (tkinter)` block (`python main.py --ui`);
  keep `### Web (VPS)` at `:285`, promote it to the section body.
- `:641` — "Local `--ui` / `--web` **always** read ..." → `--web`.
- `:651` — "What both UIs support:" → "What the web UI supports:".
- `:662-669` — remove the toolbar-only rows (`**Volume gate** checkbox (toolbar)`), keep the web
  controls; `dual is --ui / --web only` → `dual is --web only`.
- `:1369-1371` — delete the `ui/` subtree from the repo-tree listing.

### `docs/`
- `docs/pattern-editor-ui.md` — the whole P10 desktop half (title at `:1`, `:63`, `:88`) describes
  `ui/patterns_dialog.py`. Delete the P10 sections and retitle to web-only.
- `docs/pattern-editor-operations.md:21, 197` — remove the "Desktop dialog" row / desktop wording.
- `docs/pattern-editor-cutover.md:58-59, 84`, `docs/pattern-editor-acceptance.md:15, 20`,
  `docs/pattern-editor.md:14, 108, 136, 160`, `docs/pattern-editor-lifecycle.md:26, 38`,
  `docs/pattern-editor-phase-1-audit.md:24-25`, `docs/pattern-editor-implementation-plan.md`
  (P10 sections), `docs/pattern-editor-ai-edits.md:4, 103`, `docs/collect_first.md:54`,
  `docs/visual-indicator-editor.md:537`, `docs/mcp-plan.md:23, 25, 123, 284`,
  `docs/fix-backtest.md:19, 119`, `docs/indicators-on-indicators.md:5, 48, 54`
  — drop desktop references; where a doc is explicitly a *plan* for P10, mark it superseded.
- `docs/laya.md:127` — already says the Tkinter app is not the target; update to past tense.

### Root-level markdown
`draw_pattern_corrections.md:7, 15, 58, 68, 100` and `patterns-only-sept4.md:259, 286, 378`
reference `ui/` files. Either retarget to `web/` equivalents or mark the referencing paragraph
historical.

### Leave alone
`docs/verification/*.txt` and `docs/codebase-audit-2026-10-12.md` are dated records of past runs —
they describe what happened, not what to do. Do not rewrite history.

---

## Explicit non-goals

- `frontend/` — a gitignored, source-less React/Vite leftover (`.gitignore:29-31`). Nothing builds
  or serves it (`web/app.py:246` mounts only `web/static`). Out of scope for this change; flag it
  separately.
- `web/api/` — stale `__pycache__` only, no `.py` sources; unrelated.
- Renaming `--web`'s `WEB_UI_*` env vars, `enable_ui_web_history()`, or `web/` module names.
- Refactoring `web/services.py`.

---

## Verification

Run in this order.

```bash
# 1. Nothing references the desktop app any more
grep -rn "ui\.\|from ui\|--ui\|run_ui\|tkinter\|tkcalendar" \
  --include=*.py --include=*.sh --include=*.txt --include=*.md . | grep -v "\.venv"
#   expected: only docs/history notes you deliberately kept + WEB_UI_* names

# 2. It is really gone
python -c "import ui"                 # → ModuleNotFoundError: No module named 'ui'
python main.py --ui                   # → argparse error "unrecognized arguments: --ui", exit 2

# 3. Entry point still imports and the web path still works
python -c "import main, web.app; print('ok')"
WEB_UI_PASSWORD=test python -c "import web.app; web.app.create_app(); print('app ok')"
./run_web.sh                          # → login page on http://localhost:8080, /health returns {"ok": true}
# log in, then exercise the Explorer tab, a chart render (PNG path uses
# analysis/chart_renderer.py), and the Patterns tab.

# 4. Tests
python -m pytest analysis/ core/ data/ indicators/ patterns_v2/ -q
python -m pytest -q                   # full suite; web/test_* still pass

# 5. Nothing still needs an X display
grep -rn "DISPLAY\|Xvfb" --include=*.py . | grep -v "\.venv"
#   expected: no hits

# 6. Packaging sanity
python -m pip install -r requirements.txt --dry-run   # tkcalendar no longer requested
```

The critical assertion for this change is #3: **`python main.py --web` serves the dashboard and
the Patterns/Explorer/Paper tabs work** — that is the whole reason the desktop app can go.

---

## Risk notes

- `main.py` dispatch is an `if/elif` chain. After removing the `--ui` arm, confirm `--web --mcp`
  still resolves to `--web` (unchanged precedence) and that `--web --papertrade-stream` behaves
  exactly as it does today (it already wins in `__main__`, so the stream flag is ignored — this is
  pre-existing behaviour, do not "fix" it in this commit).
- `data/pattern_client.py` is imported by `core/remote_pattern_store.py` for the VPS read path.
  Deleting `RemotePatternEditor` must not disturb `fetch_pinned` / `fetch_version_bundle` /
  `upload_run` / `fetch_run` / `_call_json`.
- The `read_text()`-based tests in `patterns_v2/` and `indicators/` fail with `FileNotFoundError`
  rather than `ImportError` — easy to miss if only `-x` is used. Listed explicitly in Step 4c.

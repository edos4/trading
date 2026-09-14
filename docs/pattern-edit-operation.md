# AI pattern editor — operation and setup

Companion to [requirements](pattern-edit.md), [contracts](pattern-edit-contracts.md) and the
[implementation tracker](pattern-edit-implementation-plan.md). This document is the operator-facing
guide for running the editor from either interface and recovering from failures.

The editor is shared by both launch modes: `python main.py --ui` (native Tk) and
`python main.py --web` (browser). Desktop editing never starts the web server or needs a browser.

## Configuration

Set these in `.env` (see `.env.example`):

| Setting | Purpose |
| --- | --- |
| `DEEPSEEK_API_KEY` | Server-side credential for DeepSeek generation. Never sent to the browser. |
| `DEEPSEEK_BASE_URL` | Provider base URL; must use HTTPS. Default `https://api.deepseek.com`. |
| `PATTERN_EDIT_MODEL` | Configurable API model ID (default `deepseek-flash`; UI shows **DeepSeek-V4.1-Flash**). |
| `PATTERN_EDIT_TIMEOUT` | Generation request timeout in seconds. |
| `PATTERN_EDIT_CGROUP_ROOT` | Writable delegated cgroup v2 subtree used to execute candidates. Empty disables preview/Apply. |
| `PATTERN_EDIT_WORKER_PYTHON` | Isolated interpreter used for candidate import/execution. Empty disables preview/Apply. |

`WEB_UI_PASSWORD` and the other web settings are unchanged; the editor reuses the existing web
authenticated session for HTTP access.

## Candidate-execution prerequisites (Linux sandbox)

Drafting, chat and provider generation work without the sandbox. **Generate Preview / Apply are
unavailable until candidate execution is isolated** (fails closed; it never falls back to an
ordinary subprocess). Required on the host:

- `bubblewrap`, `util-linux` (`prlimit`), `libseccomp`, and a pinned worker Python environment.
- A dedicated unprivileged worker service with a **delegated cgroup v2 subtree**
  (`Delegate=yes` in its systemd unit) exposing the `pids`, `memory` and `cpu` controllers.
- Point `PATTERN_EDIT_CGROUP_ROOT` at that subtree and `PATTERN_EDIT_WORKER_PYTHON` at the pinned
  interpreter.

Verify the host with:

```bash
.venv/bin/python scripts/probe_pattern_edit_host.py
```

The probe and the `CandidateRunner.available()` checks confirm bwrap/prlimit/seccomp, cgroup
writability and the enabled controllers. If any requirement is missing, `preview` and `apply`
return an actionable "sandbox unavailable" result instead of running unisolated code.

## Launching

- Desktop: `python main.py --ui` → **Paper Trading** → double-click an open or closed row.
- Web: `python main.py --web` → open the paper dashboard → double-click an open or closed row.

Charts opened from log rows (signals without a stored trade) are view-only: they have no stable
trade identity, so the editor is not attached.

## Editing workflow (matches the in-app **How to edit** text)

1. Double-click an open or closed paper trade to open its chart.
2. Right-click a real candle and choose **Edit Pattern**, or drag a labeled pattern point onto
   another candle. Right-click / drag add a proposed correction; they do not save a version.
3. Describe the change in the chat panel, e.g. "Prefer this later peak as the right shoulder" or
   "Place the stop above the right shoulder high". The selected candle's date and OHLC appear
   beside the message box. Corrections change general behavior across symbols.
4. Select a semantic role (LS/LN/HEAD/RN/RS/entry/stop/target) and an OHLC or price snap.
   Selected candles are identified by symbol/market/timeframe/timestamp/dataset index.
5. **Generate Preview**. Review the executed baseline/candidate chart, explanation, code/document
   diff and validation checks. Continue chatting or dragging to refine; every revision gets a
   fresh preview and validation.
6. **Broader Comparison** (optional) runs the baseline and candidate over chosen local symbols on
   identical data and reports added/removed signals and trade changes. It stays separate from the
   mandatory selected-chart validation and is labelled **Not run** until requested.
7. **Apply** activates exactly the displayed, validated revision. **Discard** leaves the active
   version unchanged.

## Versions, activation and pending state

- Applied versions are immutable snapshots under `pattern_versions/<pattern_id>/<version_id>/`,
  with content-addressed artifacts below `pattern_versions/artifacts/`.
- The transactional registry is `data/pattern_edit/registry.sqlite3` (WAL, foreign keys,
  `BEGIN IMMEDIATE`, compare-and-swap base checks). Both interfaces and both processes share it.
- Apply stages and fsyncs the complete version, journals the source-mirror write, then atomically
  switches the active registry reference. A crash at any boundary is reconciled by
  `PatternVersions.recover()`; a partially written version can never become executable.
- Scanner/worker activation is acknowledged at a scan boundary. The UI distinguishes
  **registered-active** from **worker-pending**; jobs that started under the old version keep it.
- New signals carry `pattern_version_id` and the resolved rule snapshot. Open positions and closed
  trades keep their entry-time rules across Apply, rollback, restart and future exit checks.

### Pending activation / stuck workers

If activation shows `worker-pending`, a required worker has not acknowledged the new version. The
activation status excludes stopped workers (dead PIDs) automatically; restart the app so workers
re-register at the next scan boundary. The registry's `activations` rows carry the required and
acknowledged worker IDs.

## Session recovery

- Sessions are durable. Closing the chart or restarting either process does not lose a draft.
- Desktop: **Reopen saved draft**. Web: **Reopen saved draft**.
- New instructions create a new immutable revision and invalidate the previous preview/validation
  eligibility; a late provider/job response cannot overwrite newer work (generation tokens).

## Manual source reconciliation

`patterns/<file>.py` and its `.md` stay the active source mirror. On each baseline/edit the mirror
hash is compared with the active version. If a `.py`/`.md` file was edited by hand:

- the change is **never overwritten silently**;
- `PatternVersions.baseline` raises a conflict and recovery refuses to proceed;
- reconcile by restoring the mirrored bytes or by starting a fresh baseline that captures the
  manual change as a new reviewed revision.

## Rollback

**Rollback** selects an earlier version and creates a *new* reviewable draft that restores its
source, documentation and dependencies. It runs the same compatibility and selected-chart
validation and requires an explicit **Apply**; history is never deleted and existing positions
keep their original rules. The new version records the restoration provenance and reason.

## Backup

Back up both the registry and the artifacts together:

- `data/pattern_edit/registry.sqlite3` (plus `-wal`/`-shm` when running);
- the `pattern_versions/` tree.

Both are excluded from pattern discovery and source control (`.gitignore`). Artifacts referenced by
versions, trades or active drafts are never garbage-collected.

## Failure handling summary

| Situation | Behaviour |
| --- | --- |
| Missing/invalid chart image | Generate Preview is refused; draft preserved. |
| Provider error / bad JSON / rejected image | Actionable error, draft preserved, no provider/model fallback. |
| Sandbox or cgroup unavailable | Preview/Apply blocked; drafts still save. |
| Required validation failed/unavailable | **Apply** disabled; blocker shown in the checks list. |
| Stale draft / base changed / manual edit | Conflict; refresh the draft and preview before Apply. |
| Retry after lost Apply response | Same idempotency key returns the original activation. |
| Hook/runtime missing for an old position | Position management pauses with an error; current code is never substituted. |

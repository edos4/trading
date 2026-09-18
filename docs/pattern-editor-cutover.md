# Pattern Editor cutover checklist (P11-06)

This is the concrete procedure to move a target environment to the
PostgreSQL-authoritative pattern editor. **Rollout status: not performed.** A
rehearsal on an isolated PostgreSQL cluster is not a deployment. Apply only
within separately established implementation/deployment authorization, and
record the actual rollout status truthfully.

## 0. Preconditions (verified on the release candidate)

- [ ] Required suites green (see [P11 test output](verification/pattern-editor-p11-tests.txt)).
- [ ] Migration/bootstrap rehearsal passed on an isolated cluster
      (P11-02 evidence).
- [ ] Backup/restore and restart rehearsals passed (P11-03 evidence).
- [ ] Concurrency rehearsal passed (P11-04 evidence).
- [ ] Exact release source snapshot recorded (Git commit, dirty state, hashes).
- [ ] No unresolved blocker in the tracker's work log.

## 1. Freeze the source snapshot

- [ ] Confirm the release commit and that the working tree matches the snapshot
      you intend to import (the bootstrap freezes working-tree bytes including
      uncommitted changes).
- [ ] Preserve the original `patterns/` files unchanged; they are import inputs
      only.

## 2. Back up the target

- [ ] Stop editor writes/jobs for a consistent boundary.
- [ ] `pg_dump` the `pattern_editor` schema to a new protected path and verify
      with `pg_restore --list`.
- [ ] Record the backup path and checksum (not credentials).

## 3. Migrate and import

- [ ] Point `PATTERN_EDITOR_DATABASE_URL` at the target editor database.
- [ ] Run `scripts/migrate_pattern_editor.py`; expect migration `1` applied (or
      "already up to date").
- [ ] Run `scripts/import_pattern_baselines.py`; expect `status: "passed"` and
      record `batch_id`, `report_id`, `snapshot_sha256`.
- [ ] Continue only if the inventory accounts for all ten detectors, six enabled
      and four skipped, with stored verification reports and no unexplained
      difference.

## 4. Worker boundary

- [ ] Stop old workers/schedulers that resolve file-backed patterns.
- [ ] Start new workers from the same release so every consumer resolves the
      published PostgreSQL catalog and passes explicit version IDs to spawned
      workers.
- [ ] Confirm no process still enumerates `patterns/*.py` for discovery and that
      no SQLite/file store is created or opened.

## 5. Health checks (both launch modes)

- [ ] `main.py --web`: `/health` 200; unauthenticated `/patterns` and
      `/api/patterns` return 401; login works; `/patterns` renders with the nav
      beside Kronos; catalog lists ten patterns with the imported defaults.
- [ ] `main.py --ui`: the Patterns toolbar opens the dialog; catalog/versions
      load; the desktop is responsive during a run.
- [ ] Start, inspect, cancel and revisit a historical stream backtest from each
      mode with a pinned version; results persist across a restart.

## 6. Post-cutover sanity

- [ ] One edit submission yields exactly one immutable version and automatic
      candidate/base backtests; the default is unchanged.
- [ ] A deliberately failing candidate leaves the default unchanged and its
      version/diagnostics visible.
- [ ] `Backtest again` recovers a transient failure with no provider call.
- [ ] A default changed in one frontend is observed (and conflict-handled) in
      the other.

## 7. Rollback conditions and steps

Roll back if any of the following hold:

- Migration or import fails or reports an unexplained difference.
- A required consumer still needs a file-backed catalog.
- Blob hash / default / report verification fails on the target.
- Both launch modes cannot run or cancel a pinned stream backtest.

Rollback:

- [ ] Stop the new workers and web/desktop processes.
- [ ] Restore the verified PostgreSQL backup (or redeploy the prior
      application) — this is the only rollback path.
- [ ] Do **not** downgrade migrations or fall back to SQLite/files at runtime.
- [ ] Record what was rolled back, why, and the restored backup identifier.

## 8. Known gates carried into rollout

- Live DeepSeek call and a real candidate sandbox are required for AI edits to
  complete end-to-end; without them edits report `blocked` (see the P09/P10
  smokes). Configure `DEEPSEEK_API_KEY` and the `PATTERN_EDIT_*` sandbox before
  declaring the feature fully live.
- Detector causal-window limitation for strict stream replay is recorded in the
  stream handoff; it is not a cutover blocker.

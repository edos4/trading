# AI pattern edits (P08)

P08 adds the direct DeepSeek adapter and the durable edit coordinator. It does not
add user-facing screens; the web Patterns tab (P09) and desktop dialog (P10) consume
this service. Generated code still executes only through the existing isolation
boundary.

## Components

- `ai/providers/deepseek.py` — direct, server-side-only DeepSeek client for the
  verified API model id `deepseek-flash`. It sends one system/user message pair
  (system instruction plus the selected immutable source, paired documentation,
  interface contract and instruction) and requires a JSON object with exactly
  `source`, `documentation`, and `explanation`. It bounds the HTTP timeout,
  retries (transient transport/429/5xx only), response size, and concurrent
  calls (an in-process semaphore sized by `DEEPSEEK_MAX_CONCURRENCY`, held across
  retries). It records the requested and returned model and the provider request
  id. The API key never leaves the process; a missing key makes the provider
  report unavailable rather than substitute another model.
- `core/pattern_edit_service.py` — the coordinator. Submission pins the immutable
  base version, the frozen dataset and the saved preset (via an idempotency key)
  *before* any provider call. Execution records `queued → generating →
  validating → backtesting → completed` with explicit `failed`, `cancelled` and
  `blocked` outcomes. The generated code is persisted as exactly one immutable
  version (parented to the selected base) before validation, and failed versions
  and diagnostics are preserved. The automatic candidate and base backtests run
  on identical frozen inputs and reuse a stored result only when the version,
  dataset, engine/runtime and every effective setting match.
- `core/test_pattern_edit_pipeline.py` — the provider is exercised over
  `httpx.MockTransport` (no network); the pipeline runs against PostgreSQL with a
  deterministic provider double and a sandbox double that executes the same
  trusted evaluator in a subprocess.

## Configuration (server-side only)

```
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_TIMEOUT_SECONDS=120
DEEPSEEK_MAX_RETRIES=2
DEEPSEEK_MAX_CONCURRENCY=2
```

The API model id `deepseek-flash` (DeepSeek-V4.1-Flash) was verified on
2026-09-18 against the official release documentation referenced in
`docs/pattern-editor.md`. A different model must be configured explicitly; the
adapter never silently substitutes one because the returned model is recorded.

## Failure and recovery semantics

- Provider unavailable (no credentials/endpoint) → the job is `blocked`, not
  failed, and no version is created.
- Malformed/oversized provider response → the job `failed` and retains the
  request/error but creates no runnable version.
- Sandbox unavailable → the job is `blocked`; there is no unrestricted fallback.
- Validation failure → the version and its report are preserved; the job
  `failed`; the default is unchanged.
- Data/backtest failure after a valid version exists → the job `failed`; the
  version survives and `retry_backtest` re-runs only the backtests with no
  further provider call and no new version.
- Cancellation at any stage → the job is `cancelled`; a version created before
  the cancellation stays preserved. A leaked lease is marked `interrupted`
  rather than re-run.
- A default change while an edit is pinned never redirects the edit or its runs,
  and no generated version is auto-promoted to default.

## Verification (P08)

- Command and result: [P08 test output](verification/pattern-editor-p08-tests.txt)
  — `124 passed, 0 failed, 0 skipped` across the P08 pipeline/provider suite plus
  the P05/P06/P07 and editor/store/version/loader/validation/lifecycle/jobs/
  bootstrap/web suites, on a disposable PostgreSQL 16.15 database.
- Covered: structured parse + model provenance; bounded retries, size, timeout;
  concurrent-call bounding; duplicate Submit idempotency; base pinning across a
  default change; missing data before generation; malformed response with no
  version; unavailable provider; validation failure preserving the version;
  zero-trade candidate treated as success; unavailable sandbox blocking; backtest
  failure with backtest-only retry; cancellation; identical-input result reuse.
- Two defects were fixed during this phase: `retry_backtest` rejected a
  `completed` edit job (so "Backtest again" could never run), and it did not fall
  back to the version id recorded in the job payload for a job that failed after
  version creation.

## Not performed / remaining gates

- **Live provider smoke:** `DEEPSEEK_API_KEY` is not configured in this
  environment, so no real call was made. Mocked/provider-double verification is
  not a live-provider result.
- **Real candidate sandbox:** unavailable here; the pipeline tests use a sandbox
  double and the blocked path is asserted separately. Production
  `PATTERN_EDIT_*` sandbox prerequisites are unchanged from P02/P04.
- User-facing editing UI is P09 (web) and P10 (desktop); integration/release
  rehearsal is P11.

Resume at **P09-01** in [the implementation tracker](pattern-editor-implementation-plan.md).

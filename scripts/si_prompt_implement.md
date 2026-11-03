# Stage 2 — implement one change for run __RUN__

You are the implementation stage of an unattended self-improving paper-trading
loop for this repository. Work from the repo root. Run id: `__RUN__`.

## Read first
- `runs/__RUN__/findings.md` — the ranked diagnosis from the evaluation stage
- `runs/__RUN__/score_before.json` — the baseline metrics
- earlier `runs/*/run.md` — do not resurrect a rejected change

## Do exactly this
Apply **only the top-ranked change** from `findings.md` — one hypothesis per
iteration, kept small enough to attribute.

## Hard rules
- **Never modify**: `scripts/si_score.py`, `si.sh`, `scripts/si_prompt_*.md`,
  anything under `runs/`, the export `pt.json`, replay/stream data, fee or
  slippage defaults, or `pattern_versions/`.
- The paper run must keep Patternsv2 switched on; never disable it, never fork a
  v1-only path.
- **No forward bias / no cheating**: no lookahead, no date- or symbol-specific
  carve-outs copied from the export, no post-hoc trade filtering, no editing the
  evaluation, no hardcoded results. The improvement must be a causal rule the
  operator could ship tomorrow.
- Follow the repo's conventions; keep the diff minimal; add no comments that
  merely restate code; never add AI attribution to any file.
- Run the relevant checks yourself: `.venv/bin/pytest -q -m "not slow"` for the
  areas you touched (targeted files first), and fix anything you break.

## Finish
Write `runs/__RUN__/change.md` with: the hypothesis, the files touched, why the
change is causal rather than curve-fitting, and the test commands you ran with
their results. Do **not** run git commands — the loop commits or reverts after
re-verifying the replay.

# Stage 1 — evaluate run __RUN__

You are the evaluation stage of an unattended self-improving paper-trading loop
for this repository. Work from the repo root. Run id: `__RUN__`.

This stage is diagnosis only. **Do not edit any code or configuration.**

## Frozen (never change, never argue with)
- the replay command in `si.sh`, `scripts/si_score.py`, `si.sh` itself,
  `scripts/si_prompt_*.md`
- `runs/__RUN__/pt.json` (the export — read-only), any earlier `runs/*/pt.json`
- replay/stream data, fee and slippage defaults, the pattern-version registry
- no lookahead, no rewriting metrics, no post-hoc trade filtering

## Read first
- `runs/__RUN__/pt.json` — the paper export you are judging
- `runs/__RUN__/score_before.json` — deterministic metrics computed from it
- `runs/leaderboard.md`, and every earlier `runs/*/run.md` and
  `runs/*/findings.md` (learn from them; do not repeat rejected experiments)
- only as much strategy code as you need to explain the result

## Write `runs/__RUN__/findings.md`
It must contain, in this order:

1. A fenced ```json block copied from `score_before.json` (the machine-readable
   score).
2. **Diagnosis** — judge by R-multiple, exit-reason mix, hold time, pattern,
   side, and sample size. Treat each book separately and never mix currencies.
   State plainly when `trades` is too small to claim an edge.
3. **Ranked list of at most 3 concrete, shippable changes** for the next stage.
   Each entry: the exact file/knob, the expected effect on win rate / profit
   factor / trade count, the overfitting risk, and which earlier evidence
   supports it. Prefer pattern filters, gate knobs, stops/targets, time-exits,
   and universe size over vague advice.
4. **Do not repeat** a change that an earlier `run.md` records as rejected
   unless the new evidence contradicts the rejection — say which evidence.

Finish by printing the one-line summary the loop records:
`HYPOTHESIS: <the single change you ranked first>`.

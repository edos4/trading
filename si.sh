#!/usr/bin/env bash
# Self-improving paper-trading loop.
#
#   ./si.sh
#
# One iteration:
#   1. replay the fixed US paper stream from a fresh ledger (--patterns-v2)
#   2. score the export deterministically (scripts/si_score.py)
#   3. `commandcode -p --yolo` diagnoses it      -> runs/NN/findings.md
#   4. `commandcode -p --yolo` applies one change -> runs/NN/change.md
#   5. replay + rescore; commit only if it beats the best accepted run,
#      otherwise revert the agent's edits
#
# Runs unattended: bounded retries/timeouts, a lock, per-iteration logs, and no
# human gate. There is no iteration cap: run numbers keep counting up (runs/NN)
# and a supervisor relaunches the loop after any unexpected exit.
# Tunables (env):
#   SI_MAX_STALE          consecutive non-improvements to stop (default 2, 0 = never)
#   SI_MIN_CLOSED         min closed trades for a valid run    (default 4)
#   SI_STREAM_START       replay window start, YYYY-MM-DD      (default 6 months back)
#   SI_REPLAY_TIMEOUT     seconds per replay                   (default 14400)
#   SI_AGENT_TIMEOUT      seconds per commandcode call         (default 3600)
#   SI_AGENT_MODEL        optional --model for commandcode
#   SI_CMD_BIN            commandcode binary                   (default commandcode)
#   SI_RELAUNCH_BACKOFF   seconds to wait before a relaunch    (default 60)
#   SI_REPIN              set 1 after deliberately editing a pinned file
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv/bin/python"
RUNS="$ROOT/runs"
LOGS="$ROOT/logs/si"
STATE_DIR="$RUNS/.state"
MANIFEST="$RUNS/.si_manifest"
LOCK="$RUNS/.si.lock"
LEADERBOARD="$RUNS/leaderboard.md"
LEDGER="$ROOT/data/cache/paper_account.json"
SIGNALS="$ROOT/logs/paper_signals_us.jsonl"

MAX_STALE="${SI_MAX_STALE:-2}"
MIN_CLOSED="${SI_MIN_CLOSED:-4}"
STREAM_START="${SI_STREAM_START:-$(date -d '6 months ago' +%F)}"
RELAUNCH_BACKOFF="${SI_RELAUNCH_BACKOFF:-60}"
REPLAY_TIMEOUT="${SI_REPLAY_TIMEOUT:-14400}"
AGENT_TIMEOUT="${SI_AGENT_TIMEOUT:-3600}"
CMD_BIN="${SI_CMD_BIN:-commandcode}"
PINNED=(scripts/si_score.py si.sh scripts/si_prompt_evaluate.md \
        scripts/si_prompt_implement.md)

mkdir -p "$RUNS" "$LOGS" "$STATE_DIR"

ITER_LOG="$LOGS/startup.log"
log() { printf '%s %s\n' "$(date -Is)" "$*" | tee -a "$ITER_LOG" >&2; }
die() { log "FATAL: $*"; exit 1; }

# ── lock (flock releases automatically if this process dies) ────────────────
command -v flock >/dev/null || die "flock is required"
exec 9>"$LOCK"
flock -n 9 || die "another si.sh holds $LOCK"
printf '%s %s\n' "$$" "$(date +%s)" >>"$LOCK"

# ── pinned-file guard: the improver may not rewrite the judge ───────────────
pin_guard() {
  local f expected actual
  if [[ "${SI_REPIN:-0}" == "1" || ! -s "$MANIFEST" ]]; then
    : >"$MANIFEST"
    for f in "${PINNED[@]}"; do
      printf '%s  %s\n' "$(sha256sum "$f" | cut -d' ' -f1)" "$f" >>"$MANIFEST"
    done
    log "pinned ${#PINNED[@]} files in $MANIFEST"
    return 0
  fi
  while read -r expected f; do
    actual="$(sha256sum "$f" 2>/dev/null | cut -d' ' -f1)"
    [[ "$actual" == "$expected" ]] \
      || die "pinned file changed: $f (re-pin deliberately with SI_REPIN=1)"
  done <"$MANIFEST"
}

pins_changed() { # 0 = an agent rewrote a pinned file
  [[ -s "$MANIFEST" ]] || return 1
  local expected f actual
  while read -r expected f; do
    actual="$(sha256sum "$f" 2>/dev/null | cut -d' ' -f1)"
    if [[ "$actual" != "$expected" ]]; then
      log "pinned file changed by the agent: $f"
      return 0
    fi
  done <"$MANIFEST"
  return 1
}

# ── operator desk state: the loop replays from a fresh ledger each time, so
#    back the real one up and put it back when the iteration ends ───────────
backup_state() {
  rm -f "$STATE_DIR"/*.present
  if [[ -f "$LEDGER" ]]; then
    cp -a "$LEDGER" "$STATE_DIR/paper_account.json"; : >"$STATE_DIR/ledger.present"
  fi
  if [[ -f "$SIGNALS" ]]; then
    cp -a "$SIGNALS" "$STATE_DIR/paper_signals_us.jsonl"; : >"$STATE_DIR/signals.present"
  fi
}
restore_state() {
  if [[ -f "$STATE_DIR/ledger.present" ]]; then
    cp -a "$STATE_DIR/paper_account.json" "$LEDGER"
  else
    rm -f "$LEDGER"
  fi
  if [[ -f "$STATE_DIR/signals.present" ]]; then
    cp -a "$STATE_DIR/paper_signals_us.jsonl" "$SIGNALS"
  else
    rm -f "$SIGNALS"
  fi
}

# The --stream replay leaves its replay server up on exit. If we started it,
# take it down with us (an operator's pre-existing server is left alone).
STREAM_PORT="$("$VENV" -c 'from config import settings; print(settings.papertrade_stream_port)' 2>/dev/null || echo 8765)"
port_open() { timeout 1 bash -c "</dev/tcp/127.0.0.1/${STREAM_PORT}" 2>/dev/null; }
PORT_WAS_OPEN=0
port_open && PORT_WAS_OPEN=1
cleanup_stream() {
  if (( PORT_WAS_OPEN == 0 )) && port_open; then
    fuser -k "${STREAM_PORT}/tcp" >/dev/null 2>&1 || true
    log "stopped the paper stream server we started (port ${STREAM_PORT})"
  fi
}
# Stop path: Ctrl-C reaches the whole process group, but a TERM sent to this PID
# alone (systemd, a plain `kill`) has to take the loop and its stage children
# down too — `timeout` does not forward signals, so walk the tree ourselves.
kill_tree() { # $1 = pid
  local pid="$1" kid
  for kid in $(pgrep -P "$pid" 2>/dev/null); do kill_tree "$kid"; done
  kill -TERM "$pid" 2>/dev/null || true
}
CHILD_PID=""
STOP_REQUESTED=0
on_stop() {
  STOP_REQUESTED=1
  log "stop requested — terminating the current stage"
  [[ -n "$CHILD_PID" ]] && kill_tree "$CHILD_PID"
}
trap on_stop INT TERM
trap 'restore_state; cleanup_stream' EXIT

# ── helpers ────────────────────────────────────────────────────────────────
next_nn() {
  local max=0 d n
  for d in "$RUNS"/[0-9]*; do
    [[ -d "$d" && ${d##*/} =~ ^[0-9]+$ ]] || continue
    n=$((10#${d##*/}))
    (( n > max )) && max=$n
  done
  printf '%02d' $((max + 1))
}
score_field() {
  "$VENV" -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2]))' "$1" "$2"
}
untracked_now() { git ls-files --others --exclude-standard | sort; }

ensure_git_identity() {
  [[ -n "$(git config user.email || true)" ]] && return 0
  local name email
  name="$(git log -1 --format='%an' 2>/dev/null || true)"
  email="$(git log -1 --format='%ae' 2>/dev/null || true)"
  [[ -n "$email" ]] || die "git identity is unset and there is no prior commit to copy"
  git config user.name "$name"
  git config user.email "$email"
  log "git identity was unset — set repo-local $name <$email>"
}

replay() { # $1 = export path; returns non-zero when no usable export appears
  local out="$1" attempt
  for attempt in 1 2 3; do
    log "replay attempt $attempt -> $out"
    VOLUME_GATE_ENABLED=false timeout -k 30 "${REPLAY_TIMEOUT}s" \
      "$VENV" main.py --paper --patterns-v2 --paper-reset \
        --market us --symbols=2000 --stream="$STREAM_START" \
        --kronos-gate-mode off --export-trades-log="$out" >>"$ITER_LOG" 2>&1
    if [[ -s "$out" ]]; then
      return 0
    fi
    log "replay attempt $attempt produced no export"
    rm -f "$out"
  done
  return 1
}

score() { # $1 = export, $2 = out json
  pin_guard
  "$VENV" "$ROOT/scripts/si_score.py" score \
    --export "$1" --out "$2" --min-closed "$MIN_CLOSED" >>"$ITER_LOG" 2>&1
}

run_agent() { # $1 prompt template, $2 run id, $3 label
  local status
  log "agent stage: $3"
  sed "s/__RUN__/$2/g" "$1" \
    | timeout -k 30 "${AGENT_TIMEOUT}s" "$CMD_BIN" -p --yolo --skip-onboarding \
        ${SI_AGENT_MODEL:+--model "$SI_AGENT_MODEL"} >>"$ITER_LOG" 2>&1
  status=$?
  log "agent stage $3 exited $status"
  return "$status"
}

write_run_md() { # $1 dir, $2 status, $3 before json, $4 after json, $5 sha, $6 note
  local dir="$1" status="$2" before="${3:-}" after="${4:-}" sha="${5:-}" note="$6"
  local -a lines=("# Run $(basename "$dir")" "" "- status: **$status**"
                  "- started: $STARTED" "- replay: fixed US stream from $STREAM_START"
                  "- finished: $(date -Is)")
  if [[ -n "$before" && -f "$before" ]]; then
    lines+=("- baseline: $(score_field "$before" win_rate_pct)% win, $(score_field "$before" trades) trades, PF $(score_field "$before" profit_factor), expR $(score_field "$before" expectancy_r), window $(score_field "$before" sim_window)")
  fi
  if [[ -n "$after" && -f "$after" ]]; then
    lines+=("- after: $(score_field "$after" win_rate_pct)% win, $(score_field "$after" trades) trades, PF $(score_field "$after" profit_factor), expR $(score_field "$after" expectancy_r)")
  fi
  [[ -n "$sha" ]] && lines+=("- commit: $sha")
  [[ -n "${CHANGE_STAT:-}" ]] && lines+=("- change: $CHANGE_STAT")
  [[ -n "$note" ]] && lines+=("- note: $note")
  printf '%s\n' "${lines[@]}" >"$dir/run.md"
}

finish_iteration() { # $1 status, $2 note, $3 before, $4 after, $5 sha
  local status="$1" note="$2" before="${3:-}" after="${4:-}" sha="${5:-}"
  write_run_md "$RUN_DIR" "$status" "$before" "$after" "$sha" "$note"
  "$VENV" "$ROOT/scripts/si_score.py" leaderboard --runs-dir "$RUNS" \
    --label "$NN" --status "$status" ${after:+--score "$after"} \
    --sha "$sha" --note "$note" >>"$ITER_LOG" 2>&1
  log "iteration $NN finished: $status ($note)"
}

# ── preconditions ──────────────────────────────────────────────────────────
[[ -x "$VENV" ]] || die "missing $VENV (create the venv first)"
command -v timeout >/dev/null || die "timeout is required"
command -v "$CMD_BIN" >/dev/null || die "commandcode binary '$CMD_BIN' not found"
command -v git >/dev/null || die "git is required"
ensure_git_identity
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  die "tracked working tree is dirty — commit or stash first (revert needs a clean baseline)"
fi
pin_guard
backup_state

FIRST_SHA="$(git rev-parse HEAD)"
log "si.sh starting: max_stale=$MAX_STALE min_closed=$MIN_CLOSED stream=$STREAM_START head=${FIRST_SHA:0:10}"

# ── the loop ───────────────────────────────────────────────────────────────
# One iteration per run dir. No iteration cap: NN keeps counting up. Stops on
# the no-progress brake (SI_MAX_STALE consecutive non-improvements, 0 = never)
# or on SIGINT/SIGTERM; anything else is a crash the supervisor retries.
iteration_loop() {
  local NN RUN_DIR STARTED CHANGE_STAT
  local STALE=0
  while :; do
  NN="$(next_nn)"
  RUN_DIR="$RUNS/$NN"
  STARTED="$(date -Is)"
  CHANGE_STAT=""
  mkdir -p "$RUN_DIR"
  ITER_LOG="$LOGS/$NN.log"
  : >"$ITER_LOG"
  log "=== iteration $NN (stale=$STALE/$MAX_STALE, stream from $STREAM_START) ==="
  restore_state
  pin_guard

  # 1. replay baseline
  if ! replay "$RUN_DIR/pt.json"; then
    finish_iteration "FAILED" "replay produced no export after 3 attempts"
    STALE=$((STALE + 1))
  elif ! "$VENV" "$ROOT/scripts/si_score.py" assert-patternsv2 \
        --export "$RUN_DIR/pt.json" >>"$ITER_LOG" 2>&1; then
    finish_iteration "FAILED" "Patternsv2 was OFF in the export"
    STALE=$((STALE + 1))
  else
    score "$RUN_DIR/pt.json" "$RUN_DIR/score_before.json"
    if ! "$VENV" "$ROOT/scripts/si_score.py" valid \
          --score "$RUN_DIR/score_before.json" >>"$ITER_LOG" 2>&1; then
      finish_iteration "INVALID" "baseline sample too small to judge" \
        "$RUN_DIR/score_before.json" "" ""
      STALE=$((STALE + 1))
    else
      # 2. evaluate
      if ! run_agent "$ROOT/scripts/si_prompt_evaluate.md" "$NN" "evaluate" \
           || [[ ! -s "$RUN_DIR/findings.md" ]]; then
        finish_iteration "FAILED" "evaluation stage produced no findings.md" \
          "$RUN_DIR/score_before.json" "" ""
        STALE=$((STALE + 1))
      else
        # 3. implement (one hypothesis)
        PRE_SHA="$(git rev-parse HEAD)"
        UNTRACKED_BEFORE="$(untracked_now)"
        run_agent "$ROOT/scripts/si_prompt_implement.md" "$NN" "implement" || true
        NEW_UNTRACKED="$(comm -13 <(printf '%s\n' "$UNTRACKED_BEFORE") <(untracked_now) | sed '/^$/d')"
        CHANGE_STAT="$(git diff --stat -- . | tr '\n' ';' | sed 's/;*$//')"
        if [[ -n "$NEW_UNTRACKED" ]]; then
          CHANGE_STAT="${CHANGE_STAT:+$CHANGE_STAT }new: $(tr '\n' ' ' <<<"$NEW_UNTRACKED")"
        fi
        if pins_changed; then
          log "reverting: the implementation stage touched a pinned file"
          git reset --hard "$PRE_SHA" >/dev/null
          while IFS= read -r f; do
            [[ -n "$f" ]] && rm -f -- "$f"
          done <<<"$NEW_UNTRACKED"
          finish_iteration "REJECTED" "agent modified a pinned file — reverted" \
            "$RUN_DIR/score_before.json" "" ""
          STALE=$((STALE + 1))
        elif [[ -z "$(git status --porcelain --untracked-files=no)" && -z "$NEW_UNTRACKED" ]]; then
          finish_iteration "REJECTED" "agent made no code/config change" \
            "$RUN_DIR/score_before.json" "" ""
          STALE=$((STALE + 1))
        elif ! replay "$RUN_DIR/pt_after.json"; then
          log "post-change replay failed — reverting"
          git reset --hard "$PRE_SHA" >/dev/null
          while IFS= read -r f; do
            [[ -n "$f" ]] && rm -f -- "$f"
          done <<<"$NEW_UNTRACKED"
          finish_iteration "REJECTED" "post-change replay produced no export" \
            "$RUN_DIR/score_before.json" "" ""
          STALE=$((STALE + 1))
        else
          score "$RUN_DIR/pt_after.json" "$RUN_DIR/score_after.json"
          REF="$RUN_DIR/score_before.json"
          BEST="$("$VENV" "$ROOT/scripts/si_score.py" best --runs-dir "$RUNS" 2>/dev/null)"
          [[ -n "$BEST" ]] && REF="$BEST"
          if "$VENV" "$ROOT/scripts/si_score.py" compare \
               --new "$RUN_DIR/score_after.json" --reference "$REF" >>"$ITER_LOG" 2>&1; then
            git add -u
            while IFS= read -r f; do
              [[ -n "$f" ]] && git add -- "$f"
            done <<<"$NEW_UNTRACKED"
            WR_B="$(score_field "$RUN_DIR/score_before.json" win_rate_pct)"
            WR_A="$(score_field "$RUN_DIR/score_after.json" win_rate_pct)"
            if git commit -q -F - <<EOF
feat(strategy): run $NN paper win rate $WR_B% -> $WR_A%

Self-improving loop iteration $NN.
Hypothesis and evidence: runs/$NN/findings.md, runs/$NN/change.md.
Scored from the fixed US Patternsv2 stream replay (runs/$NN/score_after.json).
EOF
            then
              SHA="$(git rev-parse HEAD)"
              mkdir -p "$RUNS/accepted"
              cp "$RUN_DIR/score_after.json" "$RUNS/accepted/$NN.json"
              finish_iteration "COMMITTED" "beat reference $REF" \
                "$RUN_DIR/score_before.json" "$RUN_DIR/score_after.json" "$SHA"
              STALE=0
            else
              log "git commit failed — reverting the change"
              git reset --hard "$PRE_SHA" >/dev/null
              while IFS= read -r f; do
                [[ -n "$f" ]] && rm -f -- "$f"
              done <<<"$NEW_UNTRACKED"
              finish_iteration "REJECTED" "git commit failed — reverted" \
                "$RUN_DIR/score_before.json" "$RUN_DIR/score_after.json" ""
              STALE=$((STALE + 1))
            fi
          else
            git reset --hard "$PRE_SHA" >/dev/null
            while IFS= read -r f; do
              [[ -n "$f" ]] && rm -f -- "$f"
            done <<<"$NEW_UNTRACKED"
            finish_iteration "REJECTED" "no verified improvement — reverted" \
              "$RUN_DIR/score_before.json" "$RUN_DIR/score_after.json" ""
            STALE=$((STALE + 1))
          fi
        fi
      fi
    fi
  fi

  restore_state
  (( STOP_REQUESTED )) && return 3
  if (( MAX_STALE > 0 && STALE >= MAX_STALE )); then
    log "no-progress brake: $STALE consecutive non-improving iterations"
    return 0
  fi
  done
}

# ── supervisor ─────────────────────────────────────────────────────────────
# The loop runs in a subshell so a fatal stage cannot take the supervisor down.
# A clean return means the no-progress brake fired; anything else is a crash and
# is retried after SI_RELAUNCH_BACKOFF. Three failures inside two minutes abort,
# so a broken environment cannot spin. The lock, state backup and pinned-file
# manifest live in this parent, so they survive every relaunch.
CRASHES=0
while :; do
  if (( STOP_REQUESTED )); then
    log "supervisor: stopped by request — not relaunching"
    break
  fi
  LOOP_STARTED="$(date +%s)"
  ( iteration_loop ) &
  CHILD_PID=$!
  wait "$CHILD_PID"
  RC=$?
  if (( STOP_REQUESTED )) && kill -0 "$CHILD_PID" 2>/dev/null; then
    kill_tree "$CHILD_PID"
    wait "$CHILD_PID" 2>/dev/null
    RC=143
  fi
  CHILD_PID=""
  restore_state
  (( STOP_REQUESTED )) && { log "supervisor: stopped by request — not relaunching"; break; }
  if (( RC == 0 )); then
    log "supervisor: no-progress brake reached — not relaunching"
    break
  fi
  ELAPSED=$(( $(date +%s) - LOOP_STARTED ))
  if (( ELAPSED < 120 )); then
    CRASHES=$((CRASHES + 1))
  else
    CRASHES=0
  fi
  log "supervisor: loop exited $RC after ${ELAPSED}s (fast failures $CRASHES/3)"
  if (( CRASHES >= 3 )); then
    log "supervisor: giving up after 3 fast failures"
    break
  fi
  log "supervisor: relaunching in ${RELAUNCH_BACKOFF}s"
  sleep "$RELAUNCH_BACKOFF"
done

log "si.sh done: best accepted: $("$VENV" "$ROOT/scripts/si_score.py" best --runs-dir "$RUNS" 2>/dev/null || echo none)"
log "leaderboard: $LEADERBOARD"

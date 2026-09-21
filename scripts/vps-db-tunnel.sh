#!/usr/bin/env bash
# SSH tunnel to the Contabo `stocks_history` PostgreSQL that owns the pattern
# editor. That server (docker `trading-postgres`, PostgreSQL 16.14) listens on
# 127.0.0.1:5437 only, so the laptop reaches the same database through a
# forwarded port. The local port mirrors the remote one, so the same
# PATTERN_EDITOR_DATABASE_URL form works on both sides.
#
#   scripts/vps-db-tunnel.sh start    # open the tunnel (idempotent)
#   scripts/vps-db-tunnel.sh stop
#   scripts/vps-db-tunnel.sh status
set -euo pipefail

VPS_HOST=${VPS_HOST:-contabo-edos}
VPS_KEY=${VPS_KEY:-$HOME/.ssh/contabo-edos}
REMOTE_HOST=${REMOTE_HOST:-127.0.0.1}
REMOTE_PORT=${REMOTE_PORT:-5437}
LOCAL_PORT=${LOCAL_PORT:-5437}
PGBIN=${PGBIN:-/usr/lib/postgresql/18/bin}
PIDFILE=${PIDFILE:-${XDG_RUNTIME_DIR:-/tmp}/trading-vps-db-tunnel.pid}

SSH_OPTS=(
  -i "$VPS_KEY"
  -o IdentitiesOnly=yes
  -o BatchMode=yes
  -o PasswordAuthentication=no
  -o PreferredAuthentications=publickey
  -o StrictHostKeyChecking=accept-new
  -o ConnectTimeout=15
  -o ServerAliveInterval=15
  -o ServerAliveCountMax=3
  -o ExitOnForwardFailure=yes
)

up() {
  [[ -f "$PIDFILE" ]] || return 1
  local p
  p=$(<"$PIDFILE")
  [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null
}

ready() {
  if [[ -x "$PGBIN/pg_isready" ]]; then
    "$PGBIN/pg_isready" -h 127.0.0.1 -p "$LOCAL_PORT" -q
  else
    (exec 3<>/dev/tcp/127.0.0.1/"$LOCAL_PORT") 2>/dev/null
  fi
}

case "${1:-status}" in
  start)
    if up; then
      echo "tunnel already up: 127.0.0.1:$LOCAL_PORT -> $VPS_HOST:$REMOTE_PORT (pid $(<"$PIDFILE"))"
      exit 0
    fi
    [[ -f "$VPS_KEY" ]] || { echo "SSH key not found: $VPS_KEY" >&2; exit 1; }
    nohup ssh "${SSH_OPTS[@]}" -N \
      -L "127.0.0.1:${LOCAL_PORT}:${REMOTE_HOST}:${REMOTE_PORT}" "$VPS_HOST" \
      >/dev/null 2>&1 &
    echo $! > "$PIDFILE"
    for _ in $(seq 1 40); do
      if ready; then
        echo "tunnel up: 127.0.0.1:$LOCAL_PORT -> $VPS_HOST:$REMOTE_PORT (pid $(<"$PIDFILE"))"
        exit 0
      fi
      if ! up; then
        rm -f "$PIDFILE"
        echo "tunnel exited during startup — check: ssh $VPS_HOST" >&2
        exit 1
      fi
      sleep 0.5
    done
    kill "$(<"$PIDFILE")" 2>/dev/null || true
    rm -f "$PIDFILE"
    echo "tunnel did not become ready on 127.0.0.1:$LOCAL_PORT" >&2
    exit 1
    ;;
  stop)
    if up; then
      kill "$(<"$PIDFILE")" 2>/dev/null || true
      rm -f "$PIDFILE"
      echo "tunnel stopped"
    else
      rm -f "$PIDFILE"
      echo "tunnel not running"
    fi
    ;;
  status)
    if up; then
      echo "tunnel up (pid $(<"$PIDFILE")): 127.0.0.1:$LOCAL_PORT -> $VPS_HOST:$REMOTE_HOST:$REMOTE_PORT"
    else
      echo "tunnel down"
      exit 1
    fi
    ;;
  *)
    echo "usage: $0 {start|stop|status}" >&2
    exit 2
    ;;
esac

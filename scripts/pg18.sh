#!/usr/bin/env bash
# Dedicated user-owned PostgreSQL 18 instance for the pattern editor.
#
# The editor needs a live PostgreSQL; this instance is separate from the shared
# system clusters (16/17) so it can be started/stopped without root. It listens
# on port 5433 and a private Unix socket, and owns the `stocks_history` database
# that PATTERN_EDITOR_DATABASE_URL points at.
#
#   scripts/pg18.sh init     # first-time: create the cluster + database
#   scripts/pg18.sh start    # start (idempotent)
#   scripts/pg18.sh stop
#   scripts/pg18.sh status
#   scripts/pg18.sh psql     # interactive shell
set -euo pipefail

PGBIN=${PGBIN:-/usr/lib/postgresql/18/bin}
PGROOT=${PGROOT:-$HOME/.local/share/trading-pg18}
PORT=${PGPORT:-5433}

case "${1:-status}" in
  init)
    mkdir -p "$PGROOT/socket"
    chmod 700 "$PGROOT/socket"
    "$PGBIN/initdb" -D "$PGROOT/data" --auth=trust --username=r00t -E UTF8 --no-locale
    cat >> "$PGROOT/data/postgresql.conf" <<EOF

# trading_bot editor database
port = $PORT
listen_addresses = '127.0.0.1'
unix_socket_directories = '$PGROOT/socket'
EOF
    "$PGBIN/pg_ctl" -D "$PGROOT/data" -l "$PGROOT/server.log" -w start
    "$PGBIN/createdb" -h "$PGROOT/socket" -p "$PORT" -U r00t stocks_history
    ;;
  start)
    "$PGBIN/pg_ctl" -D "$PGROOT/data" -l "$PGROOT/server.log" -w start
    ;;
  stop)
    "$PGBIN/pg_ctl" -D "$PGROOT/data" -m fast -w stop
    ;;
  restart)
    "$PGBIN/pg_ctl" -D "$PGROOT/data" -l "$PGROOT/server.log" -m fast -w restart
    ;;
  status)
    "$PGBIN/pg_ctl" -D "$PGROOT/data" status
    ;;
  psql)
    "$PGBIN/psql" -h "$PGROOT/socket" -p "$PORT" -U r00t -d stocks_history
    ;;
  *)
    echo "usage: $0 {init|start|stop|restart|status|psql}" >&2
    exit 2
    ;;
esac

#!/usr/bin/env bash
# install.sh — one-command setup for Lubuntu (Ubuntu-based, lightweight).
#
# Usage:
#   ./install.sh                      # full setup: system pkgs + venv + deps + .env + postgres
#   ./install.sh --with-playwright    # also: playwright chromium + OS deps (for web replay tests)
#   ./install.sh --no-system          # skip apt (no sudo / already installed)
#   ./install.sh --no-db              # skip postgres create/start
#
# Idempotent: safe to re-run. Heavy/optional bits (IB TWS)
# are NOT installed by default — see notes at the end.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

WITH_PLAYWRIGHT=0
DO_SYSTEM=1
DO_DB=1
for arg in "$@"; do
  case "$arg" in
    --with-playwright) WITH_PLAYWRIGHT=1 ;;
    --no-system) DO_SYSTEM=0 ;;
    --no-db) DO_DB=0 ;;
    -h|--help)
      sed -n '2,11p' "$0"
      exit 0
      ;;
    *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }

# ── 1. OS check (informational only) ──────────────────────────────────────
if command -v lsb_release >/dev/null 2>&1; then
  say "OS: $(lsb_release -ds 2>/dev/null || echo unknown)"
else
  warn "lsb_release not found — assuming Debian/Ubuntu-compatible."
fi

# ── 2. System packages (apt) ──────────────────────────────────────────────
if [[ "$DO_SYSTEM" -eq 1 ]]; then
  if ! command -v apt-get >/dev/null 2>&1; then
    warn "apt-get not found — skipping system packages. Install manually: python3-venv python3-tk postgresql bubblewrap"
  elif [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    SUDO=""
  elif command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    SUDO=""
    warn "sudo not found and not root — skipping apt. Re-run with sudo or --no-system."
  fi

  if command -v apt-get >/dev/null 2>&1 && { [[ "${EUID:-$(id -u)}" -eq 0 ]] || command -v sudo >/dev/null 2>&1; }; then
    say "Installing system packages (apt)"
    $SUDO apt-get update -y
    # python3-tk: required for `python main.py --ui` (tkinter + tkcalendar)
    # postgresql: local stocks_history DB for --check-db/--update-db + pattern editor
    # bubblewrap+prlimit(util-linux): pattern-version sandbox (optional at runtime)
    # libfreetype/png: matplotlib/mplfinance chart rendering
    $SUDO apt-get install -y \
      python3 python3-venv python3-pip python3-dev python3-tk \
      build-essential git curl rsync pkg-config \
      libpq-dev postgresql postgresql-contrib \
      libfreetype6 libpng16-16 \
      bubblewrap
  fi
else
  say "Skipping system packages (--no-system)"
fi

# ── 3. Python venv + deps ─────────────────────────────────────────────────
say "Creating venv (.venv)"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
else
  echo "    .venv already exists — reusing"
fi

say "Installing Python deps (requirements.txt)"
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

if [[ "$WITH_PLAYWRIGHT" -eq 1 ]]; then
  say "Installing Playwright chromium"
  # install-deps needs sudo/apt; install browsers into the venv
  .venv/bin/python -m playwright install --with-deps chromium || \
    .venv/bin/python -m playwright install chromium
else
  echo "    (skip Playwright browsers — re-run with --with-playwright if needed)"
fi

# ── 4. .env + runtime dirs ────────────────────────────────────────────────
if [[ ! -f .env ]]; then
  say "Creating .env from .env.example (EDIT IT next)"
  cp .env.example .env
else
  echo "    .env already exists — leaving it alone"
fi

say "Creating runtime dirs"
mkdir -p logs charts data/barcache trade_histories pattern_versions

chmod +x run_ui.sh run_web.sh run_deploy.sh paper_stream.sh deploy_to_contabo.sh deploy_to_engine.sh 2>/dev/null || true

# ── 5. PostgreSQL (local stocks_history) ──────────────────────────────────
if [[ "$DO_DB" -eq 1 ]]; then
  if ! command -v psql >/dev/null 2>&1; then
    warn "psql not found — skipping DB setup (install postgresql or re-run without --no-db)."
  else
    say "Ensuring PostgreSQL is running"
    if command -v systemctl >/dev/null 2>&1; then
      sudo systemctl enable --now postgresql 2>/dev/null || \
        sudo service postgresql start 2>/dev/null || \
        warn "could not start postgresql via systemctl/service — start it manually."
    else
      sudo service postgresql start 2>/dev/null || warn "could not start postgresql."
    fi

    # Default .env uses peer auth over the local socket:
    #   DATABASE_URL=postgresql://<os-user>@/stocks_history?host=/var/run/postgresql
    # So the role must match the current OS user and the DB must exist.
    DB_USER="${USER:-$(id -un)}"
    say "Ensuring role + database (user=${DB_USER}, db=stocks_history)"
    sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" 2>/dev/null | grep -q 1 || \
      sudo -u postgres createuser -s "${DB_USER}" 2>/dev/null || \
      warn "could not create role ${DB_USER} (may already exist)."
    sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='stocks_history'" 2>/dev/null | grep -q 1 || \
      sudo -u postgres createdb -O "${DB_USER}" stocks_history 2>/dev/null || \
      warn "could not create database stocks_history (may already exist)."

    say "Migrating pattern-editor tables (non-fatal if DB unreachable)"
    if ! .venv/bin/python scripts/setup_editor_db.py; then
      warn "setup_editor_db.py did not complete — run it later after fixing DATABASE_URL."
    fi

    say "Migrating visual-indicator rules tables (non-fatal if DB unreachable)"
    if ! .venv/bin/python scripts/setup_rules_db.py; then
      warn "setup_rules_db.py did not complete — run it later after fixing DATABASE_URL."
    fi
  fi
else
  say "Skipping PostgreSQL setup (--no-db)"
fi

# ── 6. Smoke test ─────────────────────────────────────────────────────────
say "Smoke test"
.venv/bin/python - <<'EOF'
import sys
ok = True
for mod, hint in [
    ("config", "config.py"),
    ("fastapi", "pip install -r requirements.txt"),
    ("pandas", "pip install -r requirements.txt"),
    ("mplfinance", "pip install -r requirements.txt"),
]:
    try:
        __import__(mod)
        print(f"    OK   {mod}")
    except Exception as exc:
        ok = False
        print(f"    FAIL {mod}: {exc} ({hint})")
try:
    import tkinter
    print(f"    OK   tkinter {tkinter.TkVersion} (--ui will work)")
except Exception as exc:
    print(f"    NOTE tkinter unavailable ({exc}) — --web/--paper/--backtest still work; install python3-tk for --ui")
sys.exit(0)
EOF

echo
echo "Done. Next steps:"
echo "  1. Edit .env (at minimum: WATCHLIST, WEB_UI_PASSWORD for --web)"
echo "  2. Headless (fastest on Lubuntu):  .venv/bin/python main.py --paper --symbols=50"
echo "     Backtest offline:               .venv/bin/python main.py --backtest 50"
echo "     Web dashboard:                  ./run_web.sh   # or: .venv/bin/python main.py --web"
echo "     Desktop explorer (needs X):     ./run_ui.sh    # needs python3-tk"
echo
echo "Not installed by design (heavy/optional):"
echo "  - IB TWS/Gateway: not required (execution is disabled in core/scanner.py). For live,"
echo "    prefer IB Gateway over TWS on Lubuntu (much lighter), Java 8+."
echo "  - Playwright browsers: re-run as ./install.sh --with-playwright if you need them."

#!/usr/bin/env bash
# Prepare the delegated cgroup v2 subtree the candidate sandbox needs.
#
# Generated pattern code only runs inside the sandbox (core/pattern_edit_worker.py).
# It needs a writable cgroup subtree with the pids/memory/cpu controllers, plus
# the worker Python. This script creates the subtree inside the user session and
# prints the two values to put in .env. Re-run it after a reboot (the cgroup
# directory is not persistent).
#
#   scripts/sandbox.sh
set -euo pipefail

U=$(id -u)
PARENT="/sys/fs/cgroup/user.slice/user-${U}.slice/user@${U}.service"
ROOT="${PATTERN_EDIT_CGROUP_ROOT:-$PARENT/pattern-edit}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -d "$PARENT" ]]; then
  echo "No user session cgroup at $PARENT — run inside a systemd user session." >&2
  exit 1
fi
command -v bwrap >/dev/null || { echo "Missing bwrap (Bubblewrap)." >&2; exit 1; }
command -v prlimit >/dev/null || { echo "Missing prlimit (util-linux)." >&2; exit 1; }

mkdir -p "$ROOT"
# The parent must hand the controllers down before the child can pass them on.
echo "+pids +memory +cpu" > "$(dirname "$ROOT")/cgroup.subtree_control"
echo "+pids +memory +cpu" > "$ROOT/cgroup.subtree_control"

extras=()
for tool in bwrap prlimit; do
  command -v "$tool" >/dev/null || extras+=("$tool")
done

echo "PATTERN_EDIT_CGROUP_ROOT=$ROOT"
echo "PATTERN_EDIT_WORKER_PYTHON=$REPO_DIR/.venv/bin/python"
echo "# controllers: $(cat "$ROOT/cgroup.subtree_control")"
if (( ${#extras[@]} )); then
  echo "# WARNING: missing sandbox tools: ${extras[*]}" >&2
fi

#!/bin/zsh

set -euo pipefail

source "$(cd "$(dirname "$0")" && pwd)/common_env.sh"

require_command go
require_command python3

acquire_bridge_lock
trap 'release_bridge_lock' EXIT INT TERM

if bridge_healthcheck; then
  echo "WhatsApp bridge is already healthy on http://127.0.0.1:${HTTP_PORT}"
  exit 0
fi

if ! bridge_has_linked_session; then
  clear_bridge_pid
  echo "No WhatsApp session is linked yet. Run 'pnpm setup' first." >&2
  exit 1
fi

if bridge_pid_is_alive; then
  kill "$(read_bridge_pid)" 2>/dev/null || true
  sleep 1
fi

listener_pid="$(bridge_listener_pid || true)"
if [[ -n "$listener_pid" ]]; then
  kill "$listener_pid" 2>/dev/null || true
  sleep 1
fi

clear_bridge_pid

bridge_pid="$(
  python3 - <<'PY'
import os
import subprocess

with open(os.environ["BRIDGE_LOG_FILE"], "ab", buffering=0) as log_file:
    process = subprocess.Popen(
        ["go", "run", "main.go"],
        cwd=os.environ["BRIDGE_DIR"],
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    print(process.pid)
PY
)"

echo "$bridge_pid" >"$BRIDGE_PID_FILE"

for _ in {1..60}; do
  if bridge_healthcheck; then
    echo "WhatsApp bridge started on http://127.0.0.1:${HTTP_PORT}"
    exit 0
  fi
  sleep 1
done

if bridge_pid_is_alive; then
  kill "$(read_bridge_pid)" 2>/dev/null || true
fi
clear_bridge_pid

echo "Failed to start WhatsApp bridge. See $BRIDGE_LOG_FILE" >&2
exit 1

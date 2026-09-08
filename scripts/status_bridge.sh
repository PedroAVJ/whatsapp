#!/bin/zsh

set -euo pipefail

source "$(cd "$(dirname "$0")" && pwd)/common_env.sh"

pid="$(read_bridge_pid || true)"
pid_alive=0
if bridge_pid_is_alive; then
  pid_alive=1
fi

healthy=0
if bridge_healthcheck; then
  healthy=1
fi

listener_pid="$(bridge_listener_pid || true)"
log_tail="$(tail -n 20 "$BRIDGE_LOG_FILE" 2>/dev/null || true)"

export STATUS_PID="${pid:-}"
export STATUS_PID_ALIVE="$pid_alive"
export STATUS_HEALTHY="$healthy"
export STATUS_LISTENER_PID="${listener_pid:-}"
export STATUS_LOG_TAIL="$log_tail"

python3 - <<'PY'
import json
import os

def split_log(value: str):
    return [line for line in value.splitlines() if line]

payload = {
    "healthy": os.environ.get("STATUS_HEALTHY") == "1",
    "pid": os.environ.get("STATUS_PID") or None,
    "pid_alive": os.environ.get("STATUS_PID_ALIVE") == "1",
    "listener_pid": os.environ.get("STATUS_LISTENER_PID") or None,
    "http_port": os.environ.get("HTTP_PORT"),
    "state_root": os.environ.get("STATE_ROOT"),
    "store_dir": os.environ.get("UPSTREAM_STORE_DIR"),
    "log_file": os.environ.get("BRIDGE_LOG_FILE"),
    "qr_text_file": os.environ.get("QR_TEXT_PATH"),
    "qr_png_file": os.environ.get("QR_PNG_PATH"),
    "lock_dir": os.environ.get("BRIDGE_LOCK_DIR"),
    "log_tail": split_log(os.environ.get("STATUS_LOG_TAIL", "")),
}
print(json.dumps(payload, indent=2))
PY

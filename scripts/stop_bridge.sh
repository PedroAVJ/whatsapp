#!/bin/zsh

set -euo pipefail

source "$(cd "$(dirname "$0")" && pwd)/common_env.sh"

acquire_bridge_lock
trap 'release_bridge_lock' EXIT INT TERM

stopped=0
stale_mcp_stopped=0

if bridge_pid_is_alive; then
  kill "$(read_bridge_pid)" 2>/dev/null || true
  stopped=1
fi

listener_pid="$(bridge_listener_pid || true)"
if [[ -n "$listener_pid" ]]; then
  kill "$listener_pid" 2>/dev/null || true
  stopped=1
fi

for _ in {1..10}; do
  if ! bridge_healthcheck; then
    break
  fi
  sleep 1
done

if pgrep -f "$MCP_SERVER_DIR/.venv/bin/python3 main.py" >/dev/null 2>&1; then
  pkill -f "$MCP_SERVER_DIR/.venv/bin/python3 main.py" || true
  stale_mcp_stopped=1
fi

clear_bridge_pid

if (( stopped == 1 || stale_mcp_stopped == 1 )); then
  echo "Stopped WhatsApp bridge"
  if (( stale_mcp_stopped == 1 )); then
    echo "Stopped stale WhatsApp MCP server processes"
  fi
else
  echo "WhatsApp bridge is not running"
fi

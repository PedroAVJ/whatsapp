#!/bin/zsh

set -euo pipefail

source "$(cd "$(dirname "$0")" && pwd)/common_env.sh"

require_command uv
require_command python3

if [[ "${WHATSAPP_ALLOW_NATIVE_MCP:-0}" != "1" ]]; then
  cat >&2 <<EOF
Native WhatsApp MCP is disabled by default during the direct-code workaround.

Use the direct CLI wrapper instead:
  cd $PLUGIN_ROOT && pnpm cli -- --json chats list --limit 15 --no-last-message

If you explicitly need the legacy MCP server for manual recovery only:
  cd $PLUGIN_ROOT && WHATSAPP_ALLOW_NATIVE_MCP=1 pnpm mcp
EOF
  exit 1
fi

if ! bridge_healthcheck; then
  cat >&2 <<EOF
WhatsApp bridge is not running.
Start it first with:
  cd $PLUGIN_ROOT && pnpm start

Helpful follow-ups:
  pnpm status
  pnpm stop
  pnpm reset-sync
  pnpm setup
EOF
  exit 1
fi

cd "$MCP_SERVER_DIR"
exec uv run main.py

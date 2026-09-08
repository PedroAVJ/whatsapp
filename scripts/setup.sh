#!/bin/zsh

set -euo pipefail

source "$(cd "$(dirname "$0")" && pwd)/common_env.sh"

require_command go

if bridge_healthcheck || [[ -n "$(bridge_listener_pid || true)" ]]; then
  echo "Stop the durable WhatsApp bridge first with 'pnpm stop' before running setup." >&2
  exit 1
fi

if [[ "${WHATSAPP_USE_PHONE_PAIRING:-}" == "1" ]]; then
  resolve_pair_phone
  if [[ -z "${WHATSAPP_MCP_PAIR_PHONE:-}" ]]; then
    echo "Phone-number pairing was requested, but no pairing phone could be resolved. Refusing to fall back to QR pairing." >&2
    exit 1
  fi
fi

export WHATSAPP_MCP_EXIT_AFTER_AUTH=1
export WHATSAPP_MCP_REQUEST_FULL_HISTORY="${WHATSAPP_MCP_REQUEST_FULL_HISTORY:-1}"
export WHATSAPP_MCP_HISTORY_SYNC_TIMEOUT_SECS="${WHATSAPP_MCP_HISTORY_SYNC_TIMEOUT_SECS:-600}"
export WHATSAPP_MCP_HISTORY_SYNC_GRACE_SECS="${WHATSAPP_MCP_HISTORY_SYNC_GRACE_SECS:-15}"

if [[ -n "${WHATSAPP_MCP_PAIR_PHONE:-}" ]]; then
  echo "Phone-number pairing fallback enabled. In WhatsApp, use Linked devices -> Link with phone number instead, then enter the code shown below."
else
  echo "QR pairing enabled. In WhatsApp, use Linked devices -> Link a device, then scan the QR code shown below."
fi
echo "After pairing, setup will remain connected while WhatsApp sends the maximum linked-device history it permits."

cd "$BRIDGE_DIR"
exec go run main.go

#!/bin/zsh

set -euo pipefail

source "$(cd "$(dirname "$0")" && pwd)/common_env.sh"

# Phone pairing must be fully resolvable before linked-device state is removed.
# Preserve the number in the private state handoff file so setup can read it
# after the old whatsmeow database has been deleted.
if [[ "${WHATSAPP_USE_PHONE_PAIRING:-}" == "1" ]]; then
  resolve_pair_phone
  if [[ -z "${WHATSAPP_MCP_PAIR_PHONE:-}" ]]; then
    echo "Phone-number pairing was requested, but no pairing phone could be resolved. Existing WhatsApp sync state was not changed." >&2
    exit 1
  fi
  print -r -- "$WHATSAPP_MCP_PAIR_PHONE" > "$PAIR_PHONE_FILE"
  chmod 0600 "$PAIR_PHONE_FILE"
fi

"$PLUGIN_ROOT/scripts/stop_bridge.sh" >/dev/null || true

rm -rf "$UPSTREAM_STORE_DIR"
rm -f "$BRIDGE_LOG_FILE" "$QR_TEXT_PATH" "$QR_PNG_PATH"
find "$STATE_ROOT" -maxdepth 1 -type f -name 'upstream-qr*' -delete 2>/dev/null || true
clear_bridge_pid
release_bridge_lock

ensure_state_layout

echo "Reset WhatsApp bridge sync state under $STATE_ROOT"

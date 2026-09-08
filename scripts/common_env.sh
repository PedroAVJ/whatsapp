#!/bin/zsh

set -euo pipefail

# WhatsApp state includes the linked-device session, message databases,
# transcripts, drafts, QR material, and logs. Keep every file created by this
# shell and by the bridge private from the outset.
umask 077

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE_ROOT="${WHATSAPP_PLUGIN_STATE_ROOT:-$HOME/.local/share/codex-whatsapp}"
if [[ "$STATE_ROOT" != /* ]]; then
  echo "WHATSAPP_PLUGIN_STATE_ROOT must be an absolute, dedicated directory." >&2
  return 1 2>/dev/null || exit 1
fi
STATE_ROOT="${STATE_ROOT:A}"
USER_HOME_ROOT="${HOME:A}"
case "$STATE_ROOT" in
  / | /Applications | /Library | /System | /Users | /Volumes | /opt | /private | /private/tmp | /private/var | /tmp | /usr | /var | "$USER_HOME_ROOT")
    echo "Refusing an unsafe WhatsApp state root; choose a dedicated directory." >&2
    return 1 2>/dev/null || exit 1
    ;;
esac
if [[ "$STATE_ROOT" == "$USER_HOME_ROOT"/* ]]; then
  state_home_relative="${STATE_ROOT#"$USER_HOME_ROOT"/}"
  if [[ "$state_home_relative" != */* ]]; then
    echo "Refusing an overly broad WhatsApp state root under the home directory." >&2
    return 1 2>/dev/null || exit 1
  fi
fi
LOG_DIR="$STATE_ROOT/logs"
UPSTREAM_ROOT="$PLUGIN_ROOT/vendor/lharries-whatsapp-mcp"
BRIDGE_DIR="$UPSTREAM_ROOT/whatsapp-bridge"
MCP_SERVER_DIR="$UPSTREAM_ROOT/whatsapp-mcp-server"
UPSTREAM_STORE_DIR="$STATE_ROOT/upstream-store"
BRIDGE_PID_FILE="$STATE_ROOT/upstream-bridge.pid"
BRIDGE_LOG_FILE="$LOG_DIR/upstream-bridge.log"
BRIDGE_LOCK_DIR="$STATE_ROOT/upstream-bridge.lock"
HTTP_PORT="${WHATSAPP_MCP_HTTP_PORT:-18080}"
LOCK_TIMEOUT_SEC="${WHATSAPP_MCP_LOCK_TIMEOUT_SEC:-30}"
QR_TEXT_PATH="$STATE_ROOT/upstream-qr.txt"
QR_PNG_PATH="$STATE_ROOT/upstream-qr.png"
PAIR_PHONE_FILE="$STATE_ROOT/pair-phone.txt"
PAIR_PHONE_SOURCE_FILE="${WHATSAPP_MCP_PAIR_PHONE_SOURCE_FILE:-}"

export PLUGIN_ROOT
export STATE_ROOT
export LOG_DIR
export UPSTREAM_ROOT
export BRIDGE_DIR
export MCP_SERVER_DIR
export UPSTREAM_STORE_DIR
export BRIDGE_PID_FILE
export BRIDGE_LOG_FILE
export BRIDGE_LOCK_DIR
export HTTP_PORT
export LOCK_TIMEOUT_SEC
export QR_TEXT_PATH
export QR_PNG_PATH
export PAIR_PHONE_FILE
export PAIR_PHONE_SOURCE_FILE

export WHATSAPP_MCP_STORE_DIR="$UPSTREAM_STORE_DIR"
export WHATSAPP_MCP_MESSAGES_DB_PATH="$UPSTREAM_STORE_DIR/messages.db"
export WHATSAPP_MCP_API_BASE_URL="http://127.0.0.1:${HTTP_PORT}/api"
export WHATSAPP_MCP_HTTP_PORT="$HTTP_PORT"
export WHATSAPP_MCP_QR_TEXT_PATH="$QR_TEXT_PATH"
export WHATSAPP_MCP_QR_PNG_PATH="$QR_PNG_PATH"

# Transcribe voice notes on the bridge event itself, then reconcile the durable
# message store whenever the bridge starts or reconnects. Prefer the stable CLI
# front door: a long-lived bridge must not retain a versioned plugin-cache path
# that disappears during an upgrade. Source checkouts still work before the
# stable front door has been installed.
resolve_whatsapp_cli_front_door() {
  if [[ -n "${WHATSAPP_CLI_FRONT_DOOR:-}" ]]; then
    print -r -- "$WHATSAPP_CLI_FRONT_DOOR"
    return 0
  fi

  local installed_cli
  installed_cli="$(command -v whatsapp 2>/dev/null || true)"
  if [[ -n "$installed_cli" ]]; then
    print -r -- "$installed_cli"
    return 0
  fi

  print -r -- "$PLUGIN_ROOT/bin/whatsapp"
}

whatsapp_cli_front_door="$(resolve_whatsapp_cli_front_door)"
export WHATSAPP_MEDIA_ARRIVAL_HOOK="${WHATSAPP_MEDIA_ARRIVAL_HOOK:-$whatsapp_cli_front_door}"
export WHATSAPP_MEDIA_ARRIVAL_HOOK_TYPES="${WHATSAPP_MEDIA_ARRIVAL_HOOK_TYPES:-audio}"
export WHATSAPP_MEDIA_RECONCILE_HOOK="${WHATSAPP_MEDIA_RECONCILE_HOOK:-$whatsapp_cli_front_door}"
export WHATSAPP_MEDIA_RECONCILE_LIMIT="${WHATSAPP_MEDIA_RECONCILE_LIMIT:-50}"
export WHATSAPP_MEDIA_RECONCILE_LOOKBACK_DAYS="${WHATSAPP_MEDIA_RECONCILE_LOOKBACK_DAYS:-14}"
export WHATSAPP_MEDIA_RECONCILE_TIMEOUT_MINUTES="${WHATSAPP_MEDIA_RECONCILE_TIMEOUT_MINUTES:-240}"
unset whatsapp_cli_front_door

resolve_pair_phone() {
  local raw_phone="${WHATSAPP_MCP_PAIR_PHONE:-}"
  if [[ -n "$raw_phone" ]]; then
    :
  elif [[ -f "$PAIR_PHONE_FILE" ]]; then
    raw_phone="$(head -n 1 "$PAIR_PHONE_FILE" 2>/dev/null || true)"
  elif [[ -n "$PAIR_PHONE_SOURCE_FILE" && -f "$PAIR_PHONE_SOURCE_FILE" ]]; then
    raw_phone="$(
      python3 - "$PAIR_PHONE_SOURCE_FILE" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
match = re.search(r"(\+?[\d][\d\s().-]{8,}\d)", text)
if match:
    print(re.sub(r"\D", "", match.group(1)))
PY
    )"
  elif [[ -f "$UPSTREAM_STORE_DIR/whatsapp.db" ]]; then
    raw_phone="$(
      python3 - "$UPSTREAM_STORE_DIR/whatsapp.db" <<'PY'
import sqlite3
import sys

database_path = sys.argv[1]
connection = None
try:
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    row = connection.execute(
        "SELECT jid FROM whatsmeow_device WHERE jid IS NOT NULL LIMIT 1"
    ).fetchone()
except sqlite3.Error:
    row = None
finally:
    if connection is not None:
        connection.close()

if row:
    user = str(row[0]).split("@", 1)[0].split(":", 1)[0]
    if user.isdigit() and len(user) >= 10:
        print(user)
PY
    )"
  fi

  raw_phone="${raw_phone//[^0-9]/}"
  # WhatsApp historically encoded Mexican mobile accounts as +521 followed by
  # the ten national digits. Phone-number pairing now accepts the current
  # Mexican E.164 form (+52 plus those same ten digits), so normalize only that
  # exact legacy shape and leave every other country/number untouched.
  if [[ ${#raw_phone} -eq 13 && "$raw_phone" == 521* ]]; then
    raw_phone="52${raw_phone#521}"
  fi
  if [[ -n "$raw_phone" ]]; then
    export WHATSAPP_MCP_PAIR_PHONE="$raw_phone"
  fi
}

ensure_state_layout() {
  mkdir -p \
    "$STATE_ROOT" \
    "$LOG_DIR" \
    "$UPSTREAM_STORE_DIR" \
    "$UPSTREAM_STORE_DIR/transcripts" \
    "$UPSTREAM_STORE_DIR/transcript-locks"

  # Upgrade existing installations as well as protecting new state. Do not
  # follow symlinks or print paths: state filenames can themselves be private.
  find -P "$STATE_ROOT" -type d -exec chmod 0700 {} +
  find -P "$STATE_ROOT" -type f -exec chmod 0600 {} +
}

ensure_state_layout

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

bridge_healthcheck() {
  python3 - "$HTTP_PORT" <<'PY'
import json
import sys
import urllib.request

port = int(sys.argv[1])
url = f"http://127.0.0.1:{port}/api/health"
try:
    with urllib.request.urlopen(url, timeout=1) as response:
        data = json.load(response)
        sys.exit(0 if data.get("ok") else 1)
except Exception:
    sys.exit(1)
PY
}

bridge_pid_is_alive() {
  if [[ ! -f "$BRIDGE_PID_FILE" ]]; then
    return 1
  fi

  local pid
  pid="$(cat "$BRIDGE_PID_FILE" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

read_bridge_pid() {
  if [[ -f "$BRIDGE_PID_FILE" ]]; then
    cat "$BRIDGE_PID_FILE" 2>/dev/null || true
  fi
}

clear_bridge_pid() {
  rm -f "$BRIDGE_PID_FILE"
}

bridge_listener_pid() {
  if ! command -v lsof >/dev/null 2>&1; then
    return 1
  fi

  lsof -nP -iTCP:"$HTTP_PORT" -sTCP:LISTEN -t 2>/dev/null | head -n 1
}

acquire_bridge_lock() {
  local waited=0
  while ! mkdir "$BRIDGE_LOCK_DIR" 2>/dev/null; do
    if (( waited >= LOCK_TIMEOUT_SEC )); then
      echo "Timed out waiting for WhatsApp bridge lock: $BRIDGE_LOCK_DIR" >&2
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
}

release_bridge_lock() {
  rmdir "$BRIDGE_LOCK_DIR" 2>/dev/null || true
}

bridge_has_linked_session() {
  python3 - "$UPSTREAM_STORE_DIR/whatsapp.db" <<'PY'
import sqlite3
import sys
from pathlib import Path

db_path = Path(sys.argv[1])
if not db_path.exists():
    raise SystemExit(1)

try:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='whatsmeow_device'")
    if cur.fetchone()[0] == 0:
        raise SystemExit(1)
    cur.execute("SELECT count(*) FROM whatsmeow_device")
    linked = cur.fetchone()[0] > 0
    raise SystemExit(0 if linked else 1)
except sqlite3.Error:
    raise SystemExit(1)
finally:
    try:
        conn.close()
    except Exception:
        pass
PY
}

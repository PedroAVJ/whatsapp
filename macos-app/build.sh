#!/bin/zsh
# Build "WhatsApp Bridge.app": the bridge binary plus the launch agent that runs
# it, registered by the app through SMAppService. Installs atomically into
# APP_DIR (default ~/Applications) and restarts an already-registered agent so
# an update takes effect. Registering is the app's job: open it once, or run
#   "<APP_DIR>/WhatsApp Bridge.app/Contents/MacOS/WhatsApp Bridge" register
#
# Environment:
#   BUNDLE_ID                  app bundle id and agent label (default com.pedro.whatsapp-bridge)
#   APP_DIR                    install directory (default ~/Applications)
#   WHATSAPP_PLUGIN_STATE_ROOT state root (default ~/.local/share/codex-whatsapp)
#   WHATSAPP_MCP_HTTP_PORT     bridge port (default 18080)
#   WHATSAPP_CLI_FRONT_DOOR    stable whatsapp CLI for media hooks (default ~/.local/bin/whatsapp)
#   CODESIGN_IDENTITY          signing identity (default: the first Apple Development
#                              identity in the keychain, else ad-hoc "-")

set -euo pipefail
umask 022

APP_SOURCE="${0:A:h}"
REPO_ROOT="${APP_SOURCE:h}"
BRIDGE_SOURCE="$REPO_ROOT/vendor/lharries-whatsapp-mcp/whatsapp-bridge"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

BUNDLE_ID="${BUNDLE_ID:-com.pedro.whatsapp-bridge}"
APP_DIR="${APP_DIR:-$HOME/Applications}"
STATE_ROOT="${WHATSAPP_PLUGIN_STATE_ROOT:-$HOME/.local/share/codex-whatsapp}"
HTTP_PORT="${WHATSAPP_MCP_HTTP_PORT:-18080}"
CLI_FRONT_DOOR="${WHATSAPP_CLI_FRONT_DOOR:-$HOME/.local/bin/whatsapp}"
VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$REPO_ROOT/package.json")"

if [[ -z "${CODESIGN_IDENTITY:-}" ]]; then
  CODESIGN_IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null | sed -n 's/.*"\(Apple Development: [^"]*\)".*/\1/p' | head -n 1)"
  CODESIGN_IDENTITY="${CODESIGN_IDENTITY:--}"
fi

APP="$APP_DIR/WhatsApp Bridge.app"
STAGE="$APP_DIR/.WhatsApp Bridge.app.build-$$"
OLD="$APP_DIR/.WhatsApp Bridge.app.old-$$"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$APP_DIR" "$STAGE/Contents/MacOS" "$STAGE/Contents/Library/LaunchAgents"

echo "Building the bridge (Go)…"
(cd "$BRIDGE_SOURCE" && CGO_ENABLED=1 go build -trimpath -o "$STAGE/Contents/MacOS/whatsapp-bridge" .)

echo "Building the app (Swift)…"
swiftc -O -swift-version 5 -target "$(uname -m)-apple-macos13.0" \
  -o "$STAGE/Contents/MacOS/WhatsApp Bridge" "$APP_SOURCE/main.swift"

python3 - "$APP_SOURCE" "$STAGE" "$BUNDLE_ID" "$VERSION" "$HOME" "$STATE_ROOT" "$HTTP_PORT" "$CLI_FRONT_DOOR" <<'PY'
import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

source, stage, bundle_id, version, home, state_root, port, cli = sys.argv[1:]
values = {"BUNDLE_ID": bundle_id, "VERSION": version, "HOME": home, "STATE_ROOT": state_root, "HTTP_PORT": port, "CLI": cli}

def render(template, target):
    text = Path(source, template).read_text()
    for key, value in values.items():
        text = text.replace(f"@{key}@", escape(value))
    if re.search(r"@[A-Z_]+@", text):
        raise SystemExit(f"unfilled placeholder in {template}")
    Path(target).write_text(text)

render("Info.plist", Path(stage, "Contents/Info.plist"))
render("LaunchAgent.plist", Path(stage, f"Contents/Library/LaunchAgents/{bundle_id}.plist"))
PY
plutil -lint "$STAGE/Contents/Info.plist" "$STAGE/Contents/Library/LaunchAgents/$BUNDLE_ID.plist" >/dev/null

echo "Signing with ${CODESIGN_IDENTITY}…"
codesign --force --timestamp=none --sign "$CODESIGN_IDENTITY" --identifier "$BUNDLE_ID.bridge" "$STAGE/Contents/MacOS/whatsapp-bridge"
codesign --force --timestamp=none --sign "$CODESIGN_IDENTITY" "$STAGE"
codesign --verify --strict --deep "$STAGE"

# Swap the bundle in place; a running bridge keeps its old binary until restarted.
if [[ -e "$APP" ]]; then
  mv "$APP" "$OLD"
fi
mv "$STAGE" "$APP"
rm -rf "$OLD"
echo "Installed $APP ($VERSION)"

domain="gui/$(id -u)"
job="$(launchctl print "$domain/$BUNDLE_ID" 2>/dev/null || true)"
if [[ "$job" == *"parent bundle identifier = $BUNDLE_ID"* ]]; then
  launchctl kickstart -k "$domain/$BUNDLE_ID"
  echo "Restarted the registered agent $BUNDLE_ID"
else
  echo "Register the agent by opening the app, or run:"
  echo "  \"$APP/Contents/MacOS/WhatsApp Bridge\" register"
fi

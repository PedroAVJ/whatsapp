#!/bin/zsh

set -euo pipefail

source "$(cd "$(dirname "$0")" && pwd)/common_env.sh"

require_command python3

exec python3 "$PLUGIN_ROOT/scripts/whatsapp_cli.py" "$@"

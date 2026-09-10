#!/usr/bin/env bash
# Foreground Thai TTS sidecar — for testing before you enable the systemd unit.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "$HERE/.env" ]] && set -a && source "$HERE/.env" && set +a
exec "${JARVIS_VENV:-$HERE/.venv}/bin/python" "$HERE/tts_server.py"

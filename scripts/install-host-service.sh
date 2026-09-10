#!/usr/bin/env bash
# Install the voice server as a systemd user service on the host.
#   ./scripts/install-host-service.sh [/path/to/jarvis_ai]
set -euo pipefail
JA="${1:-$HOME/jarvis_ai}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$JA/server/server.py" ]] || { echo "not a jarvis_ai checkout: $JA" >&2; exit 1; }
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
sed "s|@@JARVIS_AI@@|$JA|g" "$HERE/host/systemd/jarvis-voice.service" > "$UNIT_DIR/jarvis-voice.service"
cp "$HERE/host/systemd/hermes-dashboard.service" "$UNIT_DIR/hermes-dashboard.service"
systemctl --user daemon-reload
echo "installed jarvis-voice.service, hermes-dashboard.service"
echo "  systemctl --user enable --now jarvis-voice hermes-dashboard"
echo "  loginctl enable-linger \$USER"
echo
echo "The dashboard needs its web UI built once (npm is required):"
echo "  hermes dashboard --port 9119 --host 127.0.0.1 --no-open   # builds, then Ctrl-C"

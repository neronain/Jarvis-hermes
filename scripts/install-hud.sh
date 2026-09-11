#!/usr/bin/env bash
# Install the Flight Deck HUD (v2) into a jarvis_ai checkout, or put v1 back.
#
#   ./scripts/install-hud.sh [/path/to/jarvis_ai]
#   ./scripts/install-hud.sh --rollback [/path/to/jarvis_ai]
#
# v2 is our own page rather than upstream's with four patchers cut into it.
# Upstream's index.html is kept beside it as index.upstream.html, so rolling
# back is a copy rather than a git operation — which matters at 2 a.m. when the
# thing you want is the HUD that worked, not an explanation.
set -euo pipefail

ROLLBACK=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --rollback) ROLLBACK=1 ;;
    *) ARGS+=("$a") ;;
  esac
done

JA="${ARGS[0]:-${JARVIS_AI_DIR:-$HOME/jarvis_ai}}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HUD="$JA/server/hud"

[[ -f "$JA/server/server.py" ]] || { echo "not a jarvis_ai checkout: $JA" >&2; exit 1; }
[[ -d "$HUD" ]] || { echo "no hud directory: $HUD" >&2; exit 1; }

log() { printf '\033[36m→\033[0m %s\n' "$*"; }

if [[ "$ROLLBACK" == "1" ]]; then
  if [[ ! -f "$HUD/index.upstream.html" ]]; then
    echo "nothing to roll back to: $HUD/index.upstream.html is missing" >&2
    exit 1
  fi
  log "restoring upstream's HUD"
  cp "$HUD/index.upstream.html" "$HUD/index.html"
  rm -f "$HUD/app.js" "$HUD/vad.js"
  # Upstream's page needs the patchers' features spliced back into it.
  log "re-running the patchers over it"
  python3 "$HERE/host/patches/apply_all.py" "$JA"
  cat <<'MSG'

v1 is back. Restart the server to serve it:
  systemctl --user restart jarvis-voice
MSG
  exit 0
fi

# Keep upstream's page once, before the first install overwrites it. .orig is
# the patchers' backup and gets rewritten by them; this one is ours and does not.
if [[ ! -f "$HUD/index.upstream.html" ]]; then
  SRC="$HUD/index.html"
  [[ -f "$HUD/index.html.orig" ]] && SRC="$HUD/index.html.orig"
  log "keeping upstream's HUD as index.upstream.html"
  cp "$SRC" "$HUD/index.upstream.html"
fi

log "installing the Flight Deck"
cp "$HERE/host/hud/index.html" "$HUD/index.html"
cp "$HERE/host/hud/app.js"     "$HUD/app.js"
cp "$HERE/host/hud/vad.js"     "$HUD/vad.js"

# The server patches still apply — only the HUD half of them is now redundant.
log "applying the server patches"
python3 "$HERE/host/patches/apply_all.py" "$JA"

cat <<'MSG'

Flight Deck installed. Restart the server, then reload the HUD:
  systemctl --user restart jarvis-voice

Roll back at any time with:
  ./scripts/install-hud.sh --rollback
MSG

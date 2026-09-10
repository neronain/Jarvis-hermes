#!/usr/bin/env bash
# Generate a short, typeable HUD token, store it, and restart the voice server.
#
#   ./scripts/new-hud-token.sh
#
# 64 random bits in a 15-character grouped form, from an alphabet with no
# 0/O/1/l/I — you have to type this on a phone keyboard, and a token nobody
# can read gets pasted into a chat window instead of typed. That is a bigger
# risk than the entropy difference: the HUD is reachable only from the LAN or
# the tailnet, and each attempt is a full WebSocket handshake, so brute force
# is not the threat model. Rotate with this script whenever it leaks.
set -euo pipefail

ENV_FILE="${JARVIS_ENV_FILE:-$HOME/.hermes/.env}"
ALPHABET="abcdefghjkmnpqrstuvwxyz23456789"   # no 0 O 1 l I

gen_group() {
  local out="" i c
  for ((i = 0; i < 5; i++)); do
    c=$(( $(od -An -N2 -tu2 < /dev/urandom | tr -d ' ') % ${#ALPHABET} ))
    out+="${ALPHABET:$c:1}"
  done
  printf '%s' "$out"
}

TOKEN="$(gen_group)-$(gen_group)-$(gen_group)"

[[ -f "$ENV_FILE" ]] || { echo "no env file at $ENV_FILE" >&2; exit 1; }
cp -p "$ENV_FILE" "${ENV_FILE}.bak-token-$(date +%Y%m%d_%H%M%S)"

if grep -qE '^JARVIS_HUD_TOKEN=' "$ENV_FILE"; then
  # sed -i with a random value is safe here: the alphabet has no sed metachars.
  sed -i.tmp -E "s|^JARVIS_HUD_TOKEN=.*|JARVIS_HUD_TOKEN=${TOKEN}|" "$ENV_FILE"
  rm -f "${ENV_FILE}.tmp"
else
  printf 'JARVIS_HUD_TOKEN=%s\n' "$TOKEN" >> "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"

if systemctl --user is-active --quiet jarvis-voice 2>/dev/null; then
  systemctl --user restart jarvis-voice
  echo "restarted jarvis-voice"
else
  echo "NOTE: restart the voice server for this to take effect"
fi

cat <<MSG

  New HUD token:  ${TOKEN}

  Open the HUD and paste it when asked, or append it once:
    https://<host>:8766/hud/?token=${TOKEN}

  The token is stored as a cookie after the first load, so you type it once
  per device. Anyone with it can drive an agent that has terminal access —
  treat it like a password.
MSG

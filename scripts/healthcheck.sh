#!/usr/bin/env bash
# One-shot health check across the whole pipeline.
#   ./scripts/healthcheck.sh [gpu-node-host]
set -uo pipefail

NODE="${1:-${JARVIS_GPU_NODE:-100.84.136.110}}"
STT_PORT="${JARVIS_STT_PORT:-8768}"
TTS_PORT="${JARVIS_TTS_PORT:-8769}"
STATS_PORT="${JARVIS_STATS_PORT:-8767}"
HERMES="${HERMES_API:-http://127.0.0.1:8642}"

pass=0; fail=0
chk() { # name url [extra curl args...]
  local name="$1" url="$2"; shift 2
  local code
  code=$(curl -s -m 8 -o /tmp/jarvis-hc.$$ -w '%{http_code}' "$@" "$url" 2>/dev/null)
  if [[ "$code" == "200" ]]; then
    printf '\033[32m✓\033[0m %-22s %s\n' "$name" "$(head -c 120 /tmp/jarvis-hc.$$ | tr -d '\n')"
    pass=$((pass+1))
  else
    printf '\033[31m✗\033[0m %-22s HTTP %s\n' "$name" "${code:-timeout}"
    fail=$((fail+1))
  fi
  rm -f /tmp/jarvis-hc.$$
}

echo "GPU node: $NODE"
ping -c1 -W2 "$NODE" >/dev/null 2>&1 \
  && printf '\033[32m✓\033[0m %-22s reachable\n' "network" \
  || printf '\033[31m✗\033[0m %-22s unreachable\n' "network"

chk "STT sidecar"  "http://$NODE:$STT_PORT/health"
chk "TTS sidecar"  "http://$NODE:$TTS_PORT/health"
chk "node stats"   "http://$NODE:$STATS_PORT/stats"
chk "hermes api"   "$HERMES/health" -H "Authorization: Bearer ${API_SERVER_KEY:-}"

# The node's output rate, the host's voice.sample_rate and the HUD's TTS_RATE
# have to be the same number. They are set in three different files, and when
# they drift nothing errors — the voice simply plays at the wrong speed, which
# reads as a bad model rather than as a bad config.
SERVER_YAML="${JARVIS_SERVER_YAML:-$HOME/jarvis_ai/server/config/server.yaml}"
HUD_HTML="${JARVIS_HUD_HTML:-$HOME/jarvis_ai/server/hud/index.html}"
node_rate=$(curl -s -m 8 "http://$NODE:$TTS_PORT/health" \
  | sed -n 's/.*"pcm_rate":[[:space:]]*\([0-9]*\).*/\1/p')
host_rate=$(sed -n '/^voice:/,/^[a-z]/s/^[[:space:]]*sample_rate:[[:space:]]*\([0-9]*\).*/\1/p' \
  "$SERVER_YAML" 2>/dev/null | head -1)
hud_rate=$(sed -n 's/.*window\.TTS_RATE[[:space:]]*||[[:space:]]*\([0-9]*\).*/\1/p' \
  "$HUD_HTML" 2>/dev/null | head -1)
if [[ -n "$node_rate" && -n "$host_rate" && -n "$hud_rate" \
      && "$node_rate" == "$host_rate" && "$host_rate" == "$hud_rate" ]]; then
  printf '\033[32m✓\033[0m %-22s %s Hz everywhere\n' "audio rate" "$node_rate"
  pass=$((pass+1))
else
  printf '\033[31m✗\033[0m %-22s node=%s host=%s hud=%s\n' \
    "audio rate" "${node_rate:-?}" "${host_rate:-?}" "${hud_rate:-?}"
  fail=$((fail+1))
fi

echo
echo "$pass passed, $fail failed"
[[ "$fail" -eq 0 ]]

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

echo
echo "$pass passed, $fail failed"
[[ "$fail" -eq 0 ]]

#!/usr/bin/env bash
# End-to-end smoke test: synthesise Thai speech on the GPU node, feed the
# result back to the STT sidecar, and check the text survives the round trip.
#
#   ./scripts/smoke-test.sh [gpu-node-host]
#
# Requires: python3 with `requests` and `numpy`, plus the tokens in the env.
set -euo pipefail

NODE="${1:-${JARVIS_GPU_NODE:-100.84.136.110}}"
TTS_PORT="${JARVIS_TTS_PORT:-8769}"
STT_PORT="${JARVIS_STT_PORT:-8768}"
TEXT="${JARVIS_SMOKE_TEXT:-สวัสดีครับ ระบบจาร์วิสพร้อมทำงานแล้ว}"

python3 - "$NODE" "$TTS_PORT" "$STT_PORT" "$TEXT" <<'PYEOF'
import os, sys, time
import requests

node, tts_port, stt_port, text = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
tts_tok = os.environ.get("JARVIS_TTS_TOKEN", "")
stt_tok = os.environ.get("JARVIS_STT_TOKEN", "")

print(f"1/3 synthesising: {text!r}")
t0 = time.time()
r = requests.post(
    f"http://{node}:{tts_port}/tts",
    json={"text": text, "format": "pcm16"},
    headers={"X-Jarvis-Token": tts_tok} if tts_tok else {},
    timeout=60,
)
r.raise_for_status()
pcm = r.content
tts_ms = (time.time() - t0) * 1000
rate = int(r.headers.get("X-Jarvis-Sample-Rate", "16000"))
secs = len(pcm) / 2 / rate
print(f"    {len(pcm)} bytes, {secs:.2f}s audio @ {rate} Hz, took {tts_ms:.0f} ms")

print("2/3 transcribing it back")
t0 = time.time()
r = requests.post(
    f"http://{node}:{stt_port}/stt",
    data=pcm,
    headers={"X-Jarvis-Token": stt_tok} if stt_tok else {},
    timeout=60,
)
r.raise_for_status()
out = r.json()
stt_ms = (time.time() - t0) * 1000
print(f"    got: {out.get('text')!r} ({stt_ms:.0f} ms)")

print("3/3 checking round trip")
# Exact equality is the wrong bar — TTS drops spaces and STT adds punctuation.
# Character overlap catches "the pipeline works" without failing on cosmetics.
a = set(text.replace(" ", ""))
b = set(str(out.get("text", "")).replace(" ", ""))
overlap = len(a & b) / len(a) if a else 0.0
print(f"    character overlap: {overlap:.0%}")
if overlap < 0.5:
    print("FAIL: transcription does not resemble the input")
    sys.exit(1)
print(f"\nPASS — total round trip {tts_ms + stt_ms:.0f} ms")
PYEOF

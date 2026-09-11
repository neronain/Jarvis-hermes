#!/usr/bin/env python3
"""Pay the model's prefill before the user speaks, not after.

Measured on this deployment, from the server's own turn log:

    turn 1  llm_time_to_first_token  29.9 s
    turn 2  llm_time_to_first_token   0.64 s

Nothing about the second question was easier. What changed is that the ~90 KB
of system prompt and tool schemas in front of it had already been through the
model once. The first turn of every cold process is prefill, not thinking, and
the user pays for it by talking into a microphone and waiting half a minute —
which is the whole difference between a voice assistant and a form.

So spend it while nobody is waiting. A throwaway turn on a throwaway session
warms the same cache: the model's prefix cache is keyed on content, and the
expensive prefix is byte-identical across sessions. Using a separate session is
the point rather than an accident — the user's own conversation must not end up
with a priming turn in it.

Warms at startup, when a client connects, and on a heartbeat while one is
connected. Never while the server is idle: an unattended process re-warming all
night is a bill, not a feature.

    python apply_voicemode.py /path/to/jarvis_ai

Idempotent; writes .orig backups on first run. Run apply_all.py instead of this
directly — the patchers share a .orig base and undo each other in isolation.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

MARKER = "# --- jarvis-hermes: voice mode warm-up ---"
HUD_MARKER = "window.TTS_RATE"

BLOCK = '''
''' + MARKER + '''
# See the module docstring of host/patches/apply_voicemode.py for the numbers.
_VM: dict = {"at": 0.0, "session": None, "turns": 0, "running": False,
             "last": "", "cold": True}
VM_TTL = float(os.environ.get("JARVIS_HERMES_WARM_TTL", "600"))
VM_HEARTBEAT = float(os.environ.get("JARVIS_HERMES_WARM_HEARTBEAT", "60"))
# Short, and explicit about tools: a warm-up that triggers a web search costs
# more than the prefill it was meant to save.
VM_PROMPT = os.environ.get(
    "JARVIS_HERMES_WARM_PROMPT",
    "ตอบด้วยคำว่า พร้อม เพียงคำเดียว ห้ามเรียกใช้เครื่องมือใด ๆ")


# Hermes refuses a second session with a title it already has
# ("Title already in use by session api_..."), so reusing one fixed title
# worked exactly once and then 400'd on every restart — the warm-up was dead
# from the first redeploy and said so only in the log. Each warm session gets
# its own title, and is retired once its own history starts costing more than
# the prefill it was opened to save.
VM_SESSION_TURNS = int(os.environ.get("JARVIS_HERMES_WARM_SESSION_TURNS", "20"))


def _vm_new_session(pipeline) -> str:
    r = requests.post(
        f"{pipeline.hermes.base}/api/sessions",
        headers=pipeline.hermes.headers(),
        json={"title": f"jarvis-warmup-{int(time.time())}"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    return (data.get("session") or data).get("id")


def _vm_warm_sync() -> str:
    pipeline = get_pipeline()
    h = (pipeline.cfg.get("hermes") or {})
    if (h.get("provider") or "hermes") != "hermes":
        return "skipped (brain is not hermes)"
    if _VM["session"] is None or _VM["turns"] >= VM_SESSION_TURNS:
        _VM["session"] = _vm_new_session(pipeline)
        _VM["turns"] = 0
    _VM["turns"] += 1
    t0 = time.perf_counter()
    run_id = ""
    for kind, value in pipeline.hermes.chat_stream_events(_VM["session"], VM_PROMPT, 180.0):
        if kind == "run":
            run_id = value
        elif kind == "text" and value.strip():
            # A first token means the prefix is in the cache — which is the
            # entire point of the exercise. What follows is the model writing a
            # word we are going to throw away, so stop the run instead of
            # paying for it.
            if run_id:
                try:
                    pipeline.hermes.stop_run(run_id)
                except Exception:
                    pass
            break
    return f"{time.perf_counter() - t0:.1f}s to first token"


async def _vm_warm(reason: str, force: bool = False) -> None:
    if _VM["running"]:
        return
    if not force and _VM["at"] and time.time() - _VM["at"] < VM_TTL:
        return
    _VM["running"] = True
    try:
        note = await asyncio.to_thread(_vm_warm_sync)
        _VM["at"] = time.time()
        _VM["cold"] = False
        _VM["last"] = note
        print(f"Hermes prefix warm ({reason}): {note}", flush=True)
    except Exception as exc:
        # A failed warm-up must never cost anyone a turn. The next real
        # utterance simply pays the prefill it would have paid anyway.
        _VM["session"] = None      # mint a fresh one next time rather than retry a dead id
        _VM["last"] = f"failed: {exc}"
        print(f"Hermes warm-up failed ({reason}): {exc}", flush=True)
    finally:
        _VM["running"] = False


def _vm_touch(reason: str) -> None:
    """Fire and forget from a request handler; never block the caller."""
    try:
        asyncio.get_running_loop().create_task(_vm_warm(reason))
    except RuntimeError:
        pass


_VM_STARTED = False


@app.on_event("startup")
async def _vm_startup() -> None:
    # This hook fires once per uvicorn listener and there are four of them.
    global _VM_STARTED
    if _VM_STARTED:
        return
    _VM_STARTED = True

    async def loop() -> None:
        await _vm_warm("startup")
        while True:
            await asyncio.sleep(VM_HEARTBEAT)
            if WS_CLIENTS:
                await _vm_warm("heartbeat")

    asyncio.create_task(loop())


@app.get("/api/warm")
async def _vm_status() -> JSONResponse:
    age = (time.time() - _VM["at"]) if _VM["at"] else None
    return JSONResponse({
        "warm": age is not None and age < VM_TTL,
        "age_seconds": round(age, 1) if age is not None else None,
        "ttl_seconds": VM_TTL,
        "heartbeat_seconds": VM_HEARTBEAT,
        "last": _VM["last"],
        "session_turns": _VM["turns"],
    })


'''

# --- the audio path -------------------------------------------------------
#
# F5-TTS-TH generates at 24 kHz and the pipeline was delivering 16 kHz, which
# measured 30% less energy in 2-4 kHz than the native output — the band Thai
# consonants live in. The HUD made that unavoidable by pinning its AudioContext
# to 16 kHz, so nothing downstream could have carried more.
#
# Only the PLAYBACK side moves. Capture stays 16 kHz because that is what the
# STT wants, and the worklet already has the branch for a context running at
# another rate; it was simply never taken.
#
# Three numbers have to agree or speech plays at the wrong speed: the node's
# JARVIS_TTS_PCM_RATE, the host's voice.sample_rate, and TTS_RATE here.
# scripts/healthcheck.sh compares them.
HUD_RATE_OLD = 'try{ audioCtx=new (window.AudioContext||window.webkitAudioContext)({sampleRate:16000}); }'
HUD_RATE_NEW = (
    '// Playback runs at the synthesiser\'s own rate; the capture worklet\n'
    '  // resamples to 16 kHz for the STT on its way out.\n'
    '  // On window, not a local const: playChunk and the ack loader read it\n'
    '  // too, and they are not inside this function.\n'
    '  window.TTS_RATE = window.TTS_RATE || 24000;\n'
    '  try{ audioCtx=new (window.AudioContext||window.webkitAudioContext)({sampleRate:window.TTS_RATE}); }'
)
HUD_BUF_OLD = 'createBuffer(1,f32.length,16000)'
HUD_BUF_NEW = 'createBuffer(1,f32.length,window.TTS_RATE)'

CONNECT_OLD = (
    '    await ws.send_json({"type": "status", "message": "Hermes voice server connected."})'
)
CONNECT_NEW = (
    '    await ws.send_json({"type": "status", "message": "Hermes voice server connected."})\n'
    '    # A client opening the HUD is the strongest signal there is that\n'
    '    # somebody is about to speak. Warm now, not when they do.\n'
    '    _vm_touch("client connected")'
)

STARTUP_ANCHOR = '@app.on_event("startup")\nasync def warm_pipeline() -> None:'


def _patch(path: Path, pairs: list[tuple[str, str, int]], marker: str) -> str:
    """pairs are (old, new, count); count 0 means every occurrence."""
    src = path.read_text(encoding="utf-8")
    if marker in src:
        return "already patched"
    for old, _, _ in pairs:
        if old not in src:
            return f"anchor not found: {old.strip()[:60]}"
    backup = path.with_suffix(path.suffix + ".orig")
    if not backup.exists():
        shutil.copy2(path, backup)
    for old, new, count in pairs:
        src = src.replace(old, new) if count == 0 else src.replace(old, new, count)
    path.write_text(src, encoding="utf-8")
    return "patched"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    root = Path(argv[1]).expanduser().resolve()
    server = root / "server" / "server.py"
    if not server.exists():
        print(f"missing: {server}", file=sys.stderr)
        return 1

    print("server.py:", _patch(server, [
        (STARTUP_ANCHOR, BLOCK.lstrip("\n") + STARTUP_ANCHOR, 1),
        (CONNECT_OLD, CONNECT_NEW, 1),
    ], MARKER))

    hud = root / "server" / "hud" / "index.html"
    # The Flight Deck already creates its AudioContext at the server's rate.
    if hud.exists() and not (root / "server" / "hud" / "app.js").exists():
        print("hud:", _patch(hud, [
            (HUD_RATE_OLD, HUD_RATE_NEW, 1),
            (HUD_BUF_OLD, HUD_BUF_NEW, 0),   # playback and the ack clips both
        ], HUD_MARKER))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

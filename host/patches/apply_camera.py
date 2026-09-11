#!/usr/bin/env python3
"""Let the assistant look at something through the camera.

Checked before building any of it, because the answer decided whether it was
worth building at all:

  the model has vision. vllm-spark-01/gemma4-26b-uncensored was sent an image
  directly and described it, so this is not a feature waiting on a model swap.

  Hermes' session API already takes images. It normalises `image_url` and
  `input_image` parts, including data: URLs, and rejects everything else with
  a named error. Request bodies cap at 10 MB, which a 1280px JPEG is nowhere
  near.

So the work is only plumbing: the HUD captures a frame, POSTs it here, and
this puts it into the same Hermes session the voice conversation uses — so
"what does this say?" lands in the conversation rather than beside it, and the
answer is spoken like any other.

    python apply_camera.py /path/to/jarvis_ai

Idempotent; writes .orig backups on first run. Run apply_all.py instead of
this directly — the patchers share a .orig base.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

MARKER = "# --- jarvis-hermes: camera ---"

BLOCK = '''
''' + MARKER + '''
# A frame is a data: URL from a canvas. Anything larger than this is a photo
# nobody needed at that size — Hermes caps a request at 10 MB and the model
# gains nothing from the extra pixels.
MAX_FRAME_BYTES = int(os.environ.get("JARVIS_FRAME_MAX_BYTES", "4000000"))
LOOK_PROMPT = os.environ.get(
    "JARVIS_LOOK_PROMPT",
    "ผู้ใช้ยกกล้องให้ดูสิ่งนี้ ช่วยดูแล้วตอบสั้น ๆ เป็นภาษาไทย "
    "ถ้าเป็นเอกสารให้อ่านข้อความสำคัญออกมา")


def _look_sync(image_url: str, question: str, conversation: str) -> dict:
    pipeline = get_pipeline()
    h = (pipeline.cfg.get("hermes") or {})
    sid = pipeline.hermes.get_session_id(conversation)
    parts = [
        {"type": "text", "text": (question or LOOK_PROMPT)},
        {"type": "image_url", "image_url": {"url": image_url}},
    ]
    # The turn context rides in front of an image turn too — it is the same
    # conversation, and the same rules about how to speak apply to it.
    try:
        parts[0]["text"] = build_turn_context(pipeline.cfg, sid) + "\\n" + parts[0]["text"]
    except NameError:
        pass
    out = {"text": "", "tools": []}
    for kind, value in pipeline.hermes.chat_stream_events(
            sid, parts, float(h.get("timeout", 240))):
        if kind == "text":
            out["text"] += value
        elif kind == "tool":
            out["tools"].append(json.loads(value))
    return out


@app.post("/api/look")
async def look(request: Request) -> JSONResponse:
    """One camera frame, and what the assistant makes of it.

    Body: {"image": "data:image/jpeg;base64,...", "question": "...", "speak": true}
    """
    body = await request.json()
    image = (body.get("image") or "").strip()
    if not image.startswith("data:image/"):
        return JSONResponse({"error": "image must be a data:image/... URL"}, status_code=400)
    if len(image) > MAX_FRAME_BYTES:
        return JSONResponse(
            {"error": f"frame too large ({len(image)//1024} KB); "
                      f"cap is {MAX_FRAME_BYTES//1024} KB"}, status_code=413)

    conversation = body.get("conversation") or (CFG.get("hermes") or {}).get(
        "conversation", "jarvis-main")
    try:
        out = await asyncio.to_thread(
            _look_sync, image, (body.get("question") or "").strip(), conversation)
    except Exception as exc:
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=502)

    text = (out.get("text") or "").strip()
    if text and body.get("speak", True):
        # Spoken to everyone watching, the way a voice turn is. Failing to
        # speak must not fail the answer — it is already on the screen.
        try:
            asyncio.get_running_loop().create_task(_speak_to_all(text))
        except Exception:
            pass
    return JSONResponse({"text": text, "tools": out.get("tools", [])})


async def _speak_to_all(text: str) -> None:
    pipeline = get_pipeline()
    timing = TurnTiming(turn_id=-1)
    try:
        chunks = await asyncio.to_thread(
            lambda: list(pipeline.tts_chunks_sync(pipeline._clean_for_tts(text), timing)))
    except Exception as exc:
        print(f"look: tts failed: {exc}", flush=True)
        return
    for client in list(WS_CLIENTS):
        try:
            await client.send_json({"type": "agent_status", "state": "speaking"})
            for chunk in chunks:
                await client.send_bytes(chunk)
            await client.send_json({"type": "agent_status", "state": "stopped"})
        except Exception:
            WS_CLIENTS.discard(client)


'''

ANCHOR = '@app.post("/api/summon")'


def _patch(path: Path, pairs, marker: str) -> str:
    src = path.read_text(encoding="utf-8")
    if marker in src:
        return "already patched"
    for old, _ in pairs:
        if old not in src:
            return f"anchor not found: {old.strip()[:60]}"
    backup = path.with_suffix(path.suffix + ".orig")
    if not backup.exists():
        shutil.copy2(path, backup)
    for old, new in pairs:
        src = src.replace(old, new, 1)
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
    print("server.py:", _patch(server, [(ANCHOR, BLOCK.lstrip("\n") + ANCHOR)], MARKER))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

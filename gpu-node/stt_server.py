#!/usr/bin/env python3
"""Jarvis GPU STT sidecar — faster-whisper on an NVIDIA GPU node (Linux).

Wire-compatible with the upstream jarvis_ai worker (``POST /stt`` taking raw
int16 16 kHz mono PCM), so the voice server's ``stt.remote`` block talks to this
unmodified. Differences from upstream:

- Linux/CUDA only — no Windows ``add_dll_directory`` shim.
- Defaults to ``large-v3`` and Thai, with ``language=auto`` still available.
- Reports decode timing and a rolling error count on ``/health`` for the HUD.

API
---
GET  /health -> {"status":"ok", ...}
POST /stt    -> {"text": "...", "language": "th", "duration": 1.9, "latency": 0.21}
       body:    raw little-endian int16 PCM, mono, 16 kHz
       headers: X-Jarvis-Token, optional X-Jarvis-Language to override per request

Auth: ``X-Jarvis-Token`` must equal ``JARVIS_STT_TOKEN`` when that is set.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager

import numpy as np
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

LOG = logging.getLogger("jarvis.stt")

MODEL_NAME = os.environ.get("JARVIS_STT_MODEL", "large-v3")
DEVICE = os.environ.get("JARVIS_STT_DEVICE", "cuda")
COMPUTE_TYPE = os.environ.get("JARVIS_STT_COMPUTE", "float16")
TOKEN = os.environ.get("JARVIS_STT_TOKEN", "")
PORT = int(os.environ.get("JARVIS_STT_PORT", "8768"))
HOST = os.environ.get("JARVIS_STT_HOST", "0.0.0.0")
SAMPLE_RATE = int(os.environ.get("JARVIS_STT_RATE", "16000"))
# "th" pins Thai (faster + more accurate than autodetect on short clips);
# "auto" lets Whisper decide, which is what you want for mixed Thai/English.
LANGUAGE = os.environ.get("JARVIS_STT_LANGUAGE", "th")
BEAM_SIZE = int(os.environ.get("JARVIS_STT_BEAM", "5"))
VAD_FILTER = os.environ.get("JARVIS_STT_VAD", "1") == "1"

_model = None
_lock = threading.Lock()
_stats = {"requests": 0, "errors": 0, "audio_seconds": 0.0, "decode_seconds": 0.0}


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _model
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from faster_whisper import WhisperModel

    LOG.info("loading %s on %s (%s) ...", MODEL_NAME, DEVICE, COMPUTE_TYPE)
    t0 = time.time()
    _model = WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE_TYPE)
    LOG.info("model ready in %.1fs", time.time() - t0)

    if os.environ.get("JARVIS_STT_WARMUP", "1") == "1":
        # Half a second of silence is enough to trigger kernel compilation.
        silence = np.zeros(SAMPLE_RATE // 2, dtype=np.float32)
        try:
            with _lock:
                list(_model.transcribe(silence, beam_size=1)[0])
            LOG.info("warmup complete")
        except Exception:
            LOG.exception("warmup failed (serving anyway)")
    yield


app = FastAPI(title="Jarvis GPU STT (faster-whisper)", version="1.0.0", lifespan=_lifespan)


def _authorised(request: Request) -> bool:
    return not TOKEN or request.headers.get("x-jarvis-token") == TOKEN


@app.get("/health")
async def health() -> dict:
    avg = (_stats["decode_seconds"] / _stats["requests"]) if _stats["requests"] else 0.0
    return {
        "status": "ok" if _model is not None else "loading",
        "model": MODEL_NAME,
        "device": DEVICE,
        "compute_type": COMPUTE_TYPE,
        "language": LANGUAGE,
        "sample_rate": SAMPLE_RATE,
        "avg_decode_seconds": round(avg, 3),
        "stats": dict(_stats),
    }


@app.post("/stt")
async def stt(request: Request):
    if not _authorised(request):
        return Response(status_code=401, content="auth required")
    if _model is None:
        return JSONResponse(status_code=503, content={"error": "model still loading"})

    body = await request.body()
    if not body:
        return JSONResponse(status_code=400, content={"error": "empty body"})
    if len(body) % 2:
        return JSONResponse(status_code=400, content={"error": "body is not int16 PCM"})

    audio = np.frombuffer(body, dtype="<i2").astype(np.float32) / 32768.0
    duration = audio.size / float(SAMPLE_RATE)

    language = request.headers.get("x-jarvis-language") or LANGUAGE
    lang_arg = None if language.lower() in ("auto", "") else language

    t0 = time.time()
    try:
        with _lock:
            segments, info = _model.transcribe(
                audio,
                language=lang_arg,
                beam_size=BEAM_SIZE,
                vad_filter=VAD_FILTER,
                condition_on_previous_text=False,
            )
            text = "".join(seg.text for seg in segments).strip()
    except Exception as exc:
        _stats["errors"] += 1
        LOG.exception("transcription failed")
        return JSONResponse(status_code=500, content={"error": f"transcription failed: {exc}"})

    elapsed = time.time() - t0
    _stats["requests"] += 1
    _stats["audio_seconds"] += duration
    _stats["decode_seconds"] += elapsed
    LOG.info("stt %.2fs audio -> %d chars in %.2fs", duration, len(text), elapsed)

    return {
        "text": text,
        "language": getattr(info, "language", language),
        "duration": round(duration, 3),
        "latency": round(elapsed, 3),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")

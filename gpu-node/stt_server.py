#!/usr/bin/env python3
"""Jarvis GPU STT sidecar — faster-whisper on an NVIDIA GPU node (Linux).

Wire-compatible with the upstream jarvis_ai worker (``POST /stt`` taking raw
int16 16 kHz mono PCM), so the voice server's ``stt.remote`` block talks to this
unmodified. Differences from upstream:

- Linux/CUDA only — no Windows ``add_dll_directory`` shim.
- Defaults to ``large-v3`` and Thai, with ``language=auto`` still available.
- Reports decode timing and a rolling error count on ``/health`` for the HUD.
- Refuses to hand back a transcript it does not believe (see "the silence
  gate" below), because the caller turns every transcript into a spoken reply.

API
---
GET  /health -> {"status":"ok", ...}
POST /stt    -> {"text": "...", "language": "th", "duration": 1.9, "latency": 0.21,
                 "dropped": null, "no_speech_prob": 0.02, "avg_logprob": -0.31}
       ``text`` is "" and ``dropped`` names the reason when the gate rejects
       the clip; callers that already skip an empty transcript need no change.
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

# --- the silence gate -------------------------------------------------------
#
# Whisper does not return "nothing" for a clip with nothing in it. Handed room
# tone, a keyboard, or a breath, it invents the most likely thing a person
# would have said — and in Thai that is a short politeness particle. The
# assistant then answers it, so the room hears the agent say "ครับ" or "อืม" to
# a user who has not spoken. From a live session: a clip of noise came back as
# "เติมมาเตือน", which is not a sentence at all.
#
# Two independent signals, because either alone is wrong:
#
#   no_speech_prob  Whisper's own estimate that the segment is not speech.
#                   High and the audio was never speech, whatever the decoder
#                   wrote down.
#   avg_logprob     how sure the decoder is of what it wrote. An invented
#                   sentence scores far below a heard one.
#
# and a text test for the specific case both miss: clean audio of the user
# saying only "ครับ". That is backchannel — the listener acknowledging the
# speaker — and answering it interrupts the very turn it was acknowledging.
NO_SPEECH_MAX = float(os.environ.get("JARVIS_STT_NO_SPEECH_MAX", "0.6"))
MIN_LOGPROB = float(os.environ.get("JARVIS_STT_MIN_LOGPROB", "-1.0"))
DROP_FILLER = os.environ.get("JARVIS_STT_DROP_FILLER", "1") == "1"

# Bare acknowledgements, and the phrases Whisper reaches for when a Thai clip
# holds no speech at all. Only ever matched against the WHOLE transcript:
# "ครับ" alone is backchannel, "ครับ ผมเข้าใจแล้ว" is a turn.
_FILLER = {
    "ครับ", "ครับผม", "คร้าบ", "ค่ะ", "คะ", "ค่า", "จ้า", "จ้ะ", "ฮะ", "ฮ่ะ",
    "อืม", "อืมม", "อือ", "อ่า", "เอ่อ", "เออ", "อ่าฮะ", "อ๋อ",
    "โอเค", "ok", "okay", "อ่ะ", "นะ", "นะครับ", "นะคะ",
    # Whisper's stock Thai hallucinations for silence.
    "ขอบคุณครับ", "ขอบคุณค่ะ", "สวัสดีครับ", "สวัสดีค่ะ",
    "ขอบคุณที่รับชม", "ขอบคุณสำหรับการรับชม",
}
_PUNCT = " \t\r\n.,!?ๆฯ\u0e46\u2026\"'"


def _is_filler(text: str) -> bool:
    """True when the whole transcript is one bare acknowledgement."""
    t = text.strip(_PUNCT).lower()
    if not t:
        return True
    if t in _FILLER:
        return True
    # "ครับ ครับ", "อืม อืม" — the same particle repeated is still nothing said.
    parts = [w for w in t.replace(",", " ").split() if w]
    return bool(parts) and len(parts) <= 3 and all(w.strip(_PUNCT) in _FILLER for w in parts)

_model = None
_lock = threading.Lock()
_stats = {"requests": 0, "errors": 0, "audio_seconds": 0.0, "decode_seconds": 0.0,
          "dropped": 0}


def _preload_cuda_libs() -> None:
    """Make the pip-installed cuBLAS/cuDNN visible to ctranslate2.

    faster-whisper's backend dlopens ``libcublas.so.12`` and ``libcudnn*.so.9``
    by soname. pip puts them under ``site-packages/nvidia/*/lib``, which is on
    no loader search path, so the import succeeds and the *first transcription*
    dies with "Library libcublas.so.12 is not found or cannot be loaded".

    Setting LD_LIBRARY_PATH from inside the process is too late — the loader
    reads it once at exec. Opening each library by absolute path with
    RTLD_GLOBAL puts it in the global symbol table, so ctranslate2's later
    dlopen by soname resolves to the already-loaded copy.

    Inter-library dependencies (cublas needs cublasLt, cudnn's engines need its
    graph/ops cores) mean load order matters and isn't documented, so failures
    are retried until a pass stops making progress.
    """
    import ctypes
    import glob
    import sysconfig

    roots = {sysconfig.get_paths()[k] for k in ("purelib", "platlib")}
    pending = sorted({
        path
        for root in roots
        for sub in ("cublas", "cudnn")
        for path in glob.glob(os.path.join(root, "nvidia", sub, "lib", "*.so*"))
    })
    if not pending:
        return  # system CUDA, or a conda install — nothing to do

    loaded = 0
    while pending:
        failed = []
        for path in pending:
            try:
                ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
                loaded += 1
            except OSError:
                failed.append(path)
        if len(failed) == len(pending):
            break  # no progress; the rest genuinely can't load
        pending = failed
    LOG.info("preloaded %d CUDA libraries from site-packages", loaded)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _model
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _preload_cuda_libs()
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
            segs = list(segments)
            text = "".join(seg.text for seg in segs).strip()
    except Exception as exc:
        _stats["errors"] += 1
        LOG.exception("transcription failed")
        return JSONResponse(status_code=500, content={"error": f"transcription failed: {exc}"})

    elapsed = time.time() - t0
    _stats["requests"] += 1
    _stats["audio_seconds"] += duration
    _stats["decode_seconds"] += elapsed

    no_speech = max((getattr(x, "no_speech_prob", 0.0) or 0.0) for x in segs) if segs else 1.0
    logprob = (sum((getattr(x, "avg_logprob", 0.0) or 0.0) for x in segs) / len(segs)
               if segs else 0.0)

    dropped = None
    if not text:
        dropped = "empty"
    elif no_speech >= NO_SPEECH_MAX:
        dropped = "no_speech"
    elif logprob <= MIN_LOGPROB:
        dropped = "low_confidence"
    elif DROP_FILLER and _is_filler(text):
        dropped = "backchannel"

    if dropped and dropped != "empty":
        _stats["dropped"] += 1
        # The rejected text is logged, never returned: it is the only way to
        # tune the thresholds, and returning it would defeat the gate.
        LOG.info("stt dropped (%s) %.2fs no_speech=%.2f logprob=%.2f: %r",
                 dropped, duration, no_speech, logprob, text[:80])
    else:
        LOG.info("stt %.2fs audio -> %d chars in %.2fs", duration, len(text), elapsed)

    return {
        "text": "" if dropped else text,
        "language": getattr(info, "language", language),
        "duration": round(duration, 3),
        "latency": round(elapsed, 3),
        "dropped": dropped,
        "no_speech_prob": round(no_speech, 3),
        "avg_logprob": round(logprob, 3),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")

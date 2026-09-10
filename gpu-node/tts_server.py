#!/usr/bin/env python3
"""Jarvis Thai TTS sidecar — F5-TTS-TH on an NVIDIA GPU node.

Exposes the Thai voice of the assistant over HTTP so the voice pipeline host
(which has no CUDA) can synthesise speech on a GPU machine instead.

F5-TTS is a zero-shot voice-cloning model: every request is conditioned on a
short reference clip plus its transcript. Voices are therefore *configuration*,
not weights — see ``voices.yaml``.

API
---
GET  /health                 -> {"status": "ok", ...}
GET  /voices                 -> {"voices": [...], "default": "..."}
POST /tts                    -> audio bytes
       {"text": "...", "voice": "jarvis", "speed": 1.0,
        "step": 32, "cfg": 2.0, "format": "wav"}
       format: "wav"   24 kHz mono WAV (F5-TTS native)
               "pcm16" raw little-endian int16 @ ``JARVIS_TTS_PCM_RATE``
                       (default 16000) — matches the voice server's
                       ``output_format: pcm_16000`` contract.

Auth: ``X-Jarvis-Token`` must equal ``JARVIS_TTS_TOKEN`` when that is set.

Run
---
    python tts_server.py            # honours the JARVIS_TTS_* env vars below
"""
from __future__ import annotations

import io
import logging
import os
import re
import threading
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import uvicorn
import yaml
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

LOG = logging.getLogger("jarvis.tts")

MODEL_VERSION = os.environ.get("JARVIS_TTS_MODEL", "v2")
TOKEN = os.environ.get("JARVIS_TTS_TOKEN", "")
PORT = int(os.environ.get("JARVIS_TTS_PORT", "8769"))
HOST = os.environ.get("JARVIS_TTS_HOST", "0.0.0.0")
VOICES_FILE = Path(os.environ.get("JARVIS_TTS_VOICES", Path(__file__).parent / "voices.yaml"))
PCM_RATE = int(os.environ.get("JARVIS_TTS_PCM_RATE", "16000"))
NATIVE_RATE = 24000  # F5-TTS-TH output rate

# Generation defaults. `step` trades latency for quality; 32 is the upstream
# default, 16 roughly halves latency with a small quality cost.
DEFAULT_STEP = int(os.environ.get("JARVIS_TTS_STEP", "32"))
DEFAULT_CFG = float(os.environ.get("JARVIS_TTS_CFG", "2.0"))
DEFAULT_SPEED = float(os.environ.get("JARVIS_TTS_SPEED", "1.0"))
# f5_tts_th.infer() re-splits anything longer than its own `max_chars` (default
# 100) on raw character count. We already split on sentence boundaries, which
# is the better cut, so this is raised above our own cap to stop the library
# adding a second, worse split inside each sentence.
DEFAULT_MAX_CHARS = int(os.environ.get("JARVIS_TTS_MAX_CHARS", "250"))

# One model, one GPU: serialise inference so concurrent HUD requests queue
# instead of racing for VRAM.
_infer_lock = threading.Lock()
_tts = None
_voices: Dict[str, Dict[str, Any]] = {}
_default_voice = ""
_stats = {"requests": 0, "chars": 0, "errors": 0, "total_seconds": 0.0}


def _load_voices() -> None:
    """Read voices.yaml into memory, resolving relative ref_audio paths."""
    global _voices, _default_voice
    if not VOICES_FILE.exists():
        raise SystemExit(
            f"voices file not found: {VOICES_FILE}\n"
            "Copy voices.example.yaml to voices.yaml and point it at a reference clip."
        )
    raw = yaml.safe_load(VOICES_FILE.read_text(encoding="utf-8")) or {}
    entries = raw.get("voices") or {}
    if not entries:
        raise SystemExit(f"no voices defined in {VOICES_FILE}")

    resolved: Dict[str, Dict[str, Any]] = {}
    for name, cfg in entries.items():
        if not isinstance(cfg, dict):
            continue
        ref_audio = Path(str(cfg.get("ref_audio", ""))).expanduser()
        if not ref_audio.is_absolute():
            ref_audio = (VOICES_FILE.parent / ref_audio).resolve()
        if not ref_audio.exists():
            raise SystemExit(f"voice '{name}': ref_audio not found: {ref_audio}")
        ref_text = str(cfg.get("ref_text", "")).strip()
        if not ref_text:
            raise SystemExit(f"voice '{name}': ref_text is required (transcript of ref_audio)")
        resolved[name] = {
            "ref_audio": str(ref_audio),
            "ref_text": ref_text,
            "speed": float(cfg.get("speed", DEFAULT_SPEED)),
            "description": str(cfg.get("description", "")),
        }

    _voices = resolved
    _default_voice = str(raw.get("default") or next(iter(resolved)))
    if _default_voice not in _voices:
        raise SystemExit(f"default voice '{_default_voice}' is not defined in {VOICES_FILE}")


def _load_model() -> None:
    global _tts
    from f5_tts_th.tts import TTS  # imported late: pulls in torch + CUDA

    LOG.info("loading F5-TTS-TH model=%s ...", MODEL_VERSION)
    t0 = time.time()
    _tts = TTS(model=MODEL_VERSION)
    LOG.info("model ready in %.1fs", time.time() - t0)


def _warmup() -> None:
    """First inference compiles kernels; pay that cost at boot, not on the first word."""
    try:
        voice = _voices[_default_voice]
        with _infer_lock:
            _tts.infer(
                ref_audio=voice["ref_audio"],
                ref_text=voice["ref_text"],
                gen_text="ระบบพร้อมทำงาน",
                step=DEFAULT_STEP,
                cfg=DEFAULT_CFG,
                speed=voice["speed"],
                max_chars=DEFAULT_MAX_CHARS,
            )
        LOG.info("warmup complete")
    except Exception:
        LOG.exception("warmup failed (serving anyway)")


def _to_wav_bytes(wav: np.ndarray, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(_to_int16(wav).tobytes())
    return buf.getvalue()


def _to_int16(wav: np.ndarray) -> np.ndarray:
    arr = np.asarray(wav, dtype=np.float32).reshape(-1)
    peak = float(np.max(np.abs(arr))) if arr.size else 0.0
    # F5-TTS returns roughly [-1, 1]; guard against the occasional overshoot
    # so loud sentences clip cleanly instead of wrapping around.
    if peak > 1.0:
        arr = arr / peak
    return np.clip(arr * 32767.0, -32768, 32767).astype("<i2")


def _resample(wav: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear resample. Speech at 24k->16k needs no fancy filter to stay intelligible."""
    if src_rate == dst_rate:
        return np.asarray(wav, dtype=np.float32).reshape(-1)
    arr = np.asarray(wav, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return arr
    n_out = int(round(arr.size * dst_rate / src_rate))
    src_idx = np.linspace(0.0, arr.size - 1.0, num=n_out, dtype=np.float64)
    return np.interp(src_idx, np.arange(arr.size, dtype=np.float64), arr).astype(np.float32)


_SENTENCE_RE = re.compile(r"[^.!?。！？\n]+[.!?。！？\n]?")


def split_sentences(text: str, max_chars: int = 220) -> List[str]:
    """Split into synthesis-sized chunks.

    F5-TTS generates a whole utterance before returning, so latency scales with
    input length. The host streams sentence by sentence; this is the same split
    exposed for callers that want the server to do it.
    """
    chunks: List[str] = []
    for raw in _SENTENCE_RE.findall(text or ""):
        s = raw.strip()
        if not s:
            continue
        while len(s) > max_chars:
            cut = s.rfind(" ", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            chunks.append(s[:cut].strip())
            s = s[cut:].strip()
        if s:
            chunks.append(s)
    return chunks


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _load_voices()
    _load_model()
    if os.environ.get("JARVIS_TTS_WARMUP", "1") == "1":
        _warmup()
    yield


app = FastAPI(title="Jarvis Thai TTS (F5-TTS-TH)", version="1.0.0", lifespan=_lifespan)


def _authorised(request: Request) -> bool:
    return not TOKEN or request.headers.get("x-jarvis-token") == TOKEN


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok" if _tts is not None else "loading",
        "model": f"F5-TTS-TH-{MODEL_VERSION}",
        "device": "cuda",
        "max_chars": DEFAULT_MAX_CHARS,
        "native_rate": NATIVE_RATE,
        "pcm_rate": PCM_RATE,
        "voices": sorted(_voices),
        "default_voice": _default_voice,
        "stats": dict(_stats),
    }


@app.get("/voices")
async def voices() -> dict:
    return {
        "default": _default_voice,
        "voices": [
            {"name": n, "description": v["description"], "speed": v["speed"]}
            for n, v in sorted(_voices.items())
        ],
    }


@app.post("/tts")
async def tts(request: Request):
    if not _authorised(request):
        return Response(status_code=401, content="auth required")
    if _tts is None:
        return JSONResponse(status_code=503, content={"error": "model still loading"})

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "body must be JSON"})

    text = str(body.get("text") or "").strip()
    if not text:
        return JSONResponse(status_code=400, content={"error": "text is required"})

    voice_name = str(body.get("voice") or _default_voice)
    voice = _voices.get(voice_name)
    if voice is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"unknown voice '{voice_name}'", "available": sorted(_voices)},
        )

    fmt = str(body.get("format") or "wav").lower()
    if fmt not in ("wav", "pcm16"):
        return JSONResponse(status_code=400, content={"error": "format must be 'wav' or 'pcm16'"})

    step = int(body.get("step") or DEFAULT_STEP)
    cfg = float(body.get("cfg") or DEFAULT_CFG)
    speed = float(body.get("speed") or voice["speed"])
    max_chars = int(body.get("max_chars") or DEFAULT_MAX_CHARS)

    t0 = time.time()
    try:
        with _infer_lock:
            wav = _tts.infer(
                ref_audio=voice["ref_audio"],
                ref_text=voice["ref_text"],
                gen_text=text,
                step=step,
                cfg=cfg,
                speed=speed,
                max_chars=max_chars,
            )
    except Exception as exc:
        _stats["errors"] += 1
        LOG.exception("inference failed")
        return JSONResponse(status_code=500, content={"error": f"inference failed: {exc}"})

    elapsed = time.time() - t0
    _stats["requests"] += 1
    _stats["chars"] += len(text)
    _stats["total_seconds"] += elapsed

    arr = np.asarray(wav, dtype=np.float32).reshape(-1)
    audio_seconds = arr.size / float(NATIVE_RATE) if arr.size else 0.0
    headers = {
        "X-Jarvis-Latency": f"{elapsed:.3f}",
        "X-Jarvis-Audio-Seconds": f"{audio_seconds:.3f}",
        "X-Jarvis-Voice": voice_name,
    }
    LOG.info("tts voice=%s chars=%d %.2fs audio in %.2fs", voice_name, len(text), audio_seconds, elapsed)

    if fmt == "pcm16":
        pcm = _to_int16(_resample(arr, NATIVE_RATE, PCM_RATE)).tobytes()
        headers["X-Jarvis-Sample-Rate"] = str(PCM_RATE)
        return Response(content=pcm, media_type="application/octet-stream", headers=headers)

    headers["X-Jarvis-Sample-Rate"] = str(NATIVE_RATE)
    return Response(content=_to_wav_bytes(arr, NATIVE_RATE), media_type="audio/wav", headers=headers)


@app.post("/split")
async def split(request: Request):
    """Expose the sentence splitter so the host can chunk identically."""
    if not _authorised(request):
        return Response(status_code=401, content="auth required")
    body = await request.json()
    return {"chunks": split_sentences(str(body.get("text") or ""))}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")

#!/usr/bin/env python3
"""F5-TTS-TH voice provider for the Jarvis voice pipeline.

Upstream jarvis_ai speaks through ElevenLabs. This provider swaps that for the
Thai F5-TTS sidecar on the GPU node, keeping the same contract the server
expects: an iterator of 16 kHz mono int16 PCM chunks, yielded sentence by
sentence so playback starts before the whole reply is synthesised.

Drop-in use inside the voice server::

    from f5_tts_provider import F5TTSProvider
    provider = F5TTSProvider.from_config(cfg["voice"])
    for pcm in provider.stream(reply_text):
        await websocket.send_bytes(pcm)

Standalone check::

    python f5_tts_provider.py --url http://100.84.136.110:8769 \
        --text "สวัสดีครับ ระบบพร้อมทำงานแล้ว" --out /tmp/out.wav
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import wave
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional

import requests

LOG = logging.getLogger("jarvis.voice.f5")

# Mirrors gpu-node/tts_server.py:split_sentences so host-side and server-side
# chunking agree. Kept local so the host needs no import from the GPU node.
_SENTENCE_RE = re.compile(r"[^.!?。！？\n]+[.!?。！？\n]?")


def split_sentences(text: str, max_chars: int = 220) -> List[str]:
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


@dataclass
class F5TTSProvider:
    """Synthesise Thai speech on the GPU node over HTTP."""

    url: str
    voice: str = "jarvis"
    token: str = ""
    speed: float = 1.0
    step: int = 32
    cfg: float = 2.0
    timeout: float = 30.0
    sample_rate: int = 16000
    max_chars: int = 220
    session: requests.Session = field(default_factory=requests.Session, repr=False)

    @classmethod
    def from_config(cls, voice_cfg: Dict[str, Any]) -> "F5TTSProvider":
        """Build from the ``voice:`` block of server.yaml.

        Secrets come from the environment, never from the YAML — same rule the
        rest of the pipeline follows.
        """
        token_env = voice_cfg.get("token_env") or "JARVIS_TTS_TOKEN"
        return cls(
            url=str(voice_cfg["url"]).rstrip("/"),
            voice=str(voice_cfg.get("voice_name") or "jarvis"),
            token=os.environ.get(token_env, ""),
            speed=float(voice_cfg.get("speed", 1.0)),
            step=int(voice_cfg.get("step", 32)),
            cfg=float(voice_cfg.get("cfg", 2.0)),
            timeout=float(voice_cfg.get("timeout", 30)),
            sample_rate=int(voice_cfg.get("sample_rate", 16000)),
        )

    # -- transport ---------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["X-Jarvis-Token"] = self.token
        return h

    def health(self) -> Dict[str, Any]:
        r = self.session.get(f"{self.url}/health", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def voices(self) -> Dict[str, Any]:
        r = self.session.get(f"{self.url}/voices", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def synthesize(self, text: str) -> bytes:
        """One chunk of text -> raw int16 PCM at ``self.sample_rate``."""
        payload = {
            "text": text,
            "voice": self.voice,
            "speed": self.speed,
            "step": self.step,
            "cfg": self.cfg,
            "format": "pcm16",
        }
        r = self.session.post(
            f"{self.url}/tts", json=payload, headers=self._headers(), timeout=self.timeout
        )
        if r.status_code != 200:
            raise RuntimeError(f"TTS {r.status_code}: {r.text[:200]}")
        return r.content

    # -- pipeline API ------------------------------------------------------

    def stream(self, text: str) -> Iterator[bytes]:
        """Yield PCM per sentence so the HUD can start playing immediately.

        A failed sentence is logged and skipped rather than killing the whole
        reply — losing one sentence beats losing the turn.
        """
        for chunk in split_sentences(text, self.max_chars):
            try:
                yield self.synthesize(chunk)
            except Exception:
                LOG.exception("sentence failed, skipping: %r", chunk[:60])

    def to_wav(self, text: str, path: str) -> None:
        pcm = b"".join(self.stream(text))
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes(pcm)


def _main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="F5-TTS-TH provider smoke test")
    ap.add_argument("--url", default=os.environ.get("JARVIS_TTS_URL", "http://100.84.136.110:8769"))
    ap.add_argument("--voice", default="jarvis")
    ap.add_argument("--text", default="สวัสดีครับ ระบบจาร์วิสพร้อมทำงานแล้ว")
    ap.add_argument("--out", default="jarvis-tts-test.wav")
    ap.add_argument("--token-env", default="JARVIS_TTS_TOKEN")
    args = ap.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = F5TTSProvider(url=args.url.rstrip("/"), voice=args.voice,
                      token=os.environ.get(args.token_env, ""))
    try:
        LOG.info("health: %s", p.health())
    except Exception as exc:
        LOG.error("cannot reach TTS sidecar at %s: %s", p.url, exc)
        return 1
    p.to_wav(args.text, args.out)
    LOG.info("wrote %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(_main())

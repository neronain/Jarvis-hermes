#!/usr/bin/env python3
"""Try Thai spellings of a word and see which one the voice says best.

Adding a loanword to the lexicon is guesswork until you hear it. This closes
the loop: each candidate spelling is synthesised on the TTS sidecar and fed
straight back to the STT sidecar, so you can compare what came out.

    python scripts/tune-lexicon.py docker ด็อกเกอร์ ดอกเกอร์ ด๊อกเก้อ
    python scripts/tune-lexicon.py --save docker ด๊อกเก้อ

Read the result as a ranking, not a verdict. Whisper's Thai vocabulary does
not contain most English loanwords either, so a clean round trip is strong
evidence and a messy one is weak evidence — for anything close, listen with
--keep and decide by ear.
"""
from __future__ import annotations

import argparse
import difflib
import os
import sys
import wave
from pathlib import Path

import requests

DEFAULT_NODE = os.environ.get("JARVIS_GPU_NODE", "100.113.214.111")


def synth(node: str, text: str, token: str, port: int) -> bytes:
    r = requests.post(
        f"http://{node}:{port}/tts",
        json={"text": text, "format": "pcm16", "normalize": False},
        headers={"X-Jarvis-Token": token} if token else {},
        timeout=120,
    )
    r.raise_for_status()
    return r.content


def transcribe(node: str, pcm: bytes, token: str, port: int) -> str:
    r = requests.post(
        f"http://{node}:{port}/stt", data=pcm,
        headers={"X-Jarvis-Token": token} if token else {},
        timeout=120,
    )
    r.raise_for_status()
    return r.json().get("text", "").strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("word", help="the source word, e.g. docker")
    ap.add_argument("spellings", nargs="+", help="Thai spellings to compare")
    ap.add_argument("--node", default=DEFAULT_NODE)
    ap.add_argument("--tts-port", type=int, default=8769)
    ap.add_argument("--stt-port", type=int, default=8768)
    ap.add_argument("--carrier", default="{}",
                    help="sentence to say it in, {} marks the slot")
    ap.add_argument("--keep", metavar="DIR",
                    help="write each candidate to DIR so you can listen")
    ap.add_argument("--save", action="store_true",
                    help="write the best candidate into gpu-node/thai_lexicon.yaml")
    args = ap.parse_args()

    tts_token = os.environ.get("JARVIS_TTS_TOKEN", "")
    stt_token = os.environ.get("JARVIS_STT_TOKEN", "")
    if args.keep:
        Path(args.keep).mkdir(parents=True, exist_ok=True)

    results = []
    for spelling in args.spellings:
        text = args.carrier.replace("{}", spelling)
        try:
            pcm = synth(args.node, text, tts_token, args.tts_port)
            heard = transcribe(args.node, pcm, stt_token, args.stt_port)
        except Exception as exc:
            print(f"  {spelling:20s} -> failed: {exc}")
            continue
        # Similarity between what we asked for and what came back. A proxy,
        # not truth — see the module docstring.
        score = difflib.SequenceMatcher(None, spelling, heard).ratio()
        results.append((score, spelling, heard))
        print(f"  {spelling:20s} -> {heard:24s} ({score:.0%})")
        if args.keep:
            out = Path(args.keep) / f"{args.word}-{spelling}.wav"
            with wave.open(str(out), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
                w.writeframes(pcm)

    if not results:
        return 1
    results.sort(reverse=True)
    best = results[0]
    print(f"\n  best: {best[1]}  ({best[0]:.0%} match)")
    if args.keep:
        print(f"  clips in {args.keep} — trust your ears over the percentage")

    if args.save:
        import yaml
        p = Path(__file__).resolve().parents[1] / "gpu-node" / "thai_lexicon.yaml"
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        data.setdefault("words", {})[args.word] = best[1]
        p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                     encoding="utf-8")
        print(f"  saved {args.word}: {best[1]} -> {p.name}")
        print("  NOTE: this rewrites the file through yaml.safe_dump, so the "
              "comments in it are lost. Prefer editing by hand.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

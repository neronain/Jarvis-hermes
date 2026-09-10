#!/usr/bin/env python3
"""Wire the F5-TTS-TH provider into an upstream jarvis_ai checkout.

Upstream's ``VoicePipelineServer.tts_chunks_sync`` talks to ElevenLabs
unconditionally. Rather than fork the file, this inserts a branch at the top
that delegates to ``F5TTSProvider`` when ``voice.provider`` is ``f5_tts_th``,
leaving the ElevenLabs path untouched for anyone who wants it back.

Idempotent: running twice is a no-op. A ``.orig`` backup is written the first
time so the change can be reverted with a copy.

    python apply_f5_tts.py /path/to/jarvis_ai
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

MARKER = "# --- jarvis-hermes: F5-TTS-TH provider ---"
MARKER_END = "# --- end jarvis-hermes ---"

ANCHOR = '''    def tts_chunks_sync(self, text: str, timing: TurnTiming) -> Iterator[bytes]:
        voice = self.cfg["voice"]
'''

INSERT = '''    def tts_chunks_sync(self, text: str, timing: TurnTiming) -> Iterator[bytes]:
        voice = self.cfg["voice"]
        ''' + MARKER + '''
        # Thai voice on a GPU node instead of ElevenLabs. Kept as an early
        # return so the upstream path below stays byte-for-byte intact.
        if voice.get("provider") == "f5_tts_th":
            yield from self._f5_tts_chunks(text, timing, voice)
            return
        ''' + MARKER_END + '''
'''

METHOD = '''
    def _f5_tts_chunks(self, text: str, timing: TurnTiming, voice: dict) -> Iterator[bytes]:
        """Stream PCM from the F5-TTS-TH sidecar, sentence by sentence.

        F5-TTS has no native streaming — it returns a whole utterance — so the
        provider splits on sentence boundaries and this yields each one as it
        arrives. That is what keeps the first syllable about a second away
        instead of waiting for the entire reply to synthesise.
        """
        from f5_tts_provider import F5TTSProvider

        if getattr(self, "_f5_provider", None) is None:
            self._f5_provider = F5TTSProvider.from_config(voice)

        timing.tts_model = "f5-tts-th"
        timing.voice_id = self._f5_provider.voice
        timing.tts_request_start_monotonic = (
            timing.tts_request_start_monotonic or time.perf_counter()
        )
        record_usage(tts_chars=len(text))

        for chunk in self._f5_provider.stream(text):
            if not chunk:
                continue
            if timing.first_tts_audio_byte_monotonic is None:
                timing.first_tts_audio_byte_monotonic = time.perf_counter()
            yield chunk
'''

TAIL_ANCHOR = "    # ------------------------------------------------------------- Turn flow"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2

    root = Path(argv[1]).expanduser().resolve()
    target = root / "server" / "server.py"
    if not target.exists():
        print(f"not a jarvis_ai checkout: {target} missing", file=sys.stderr)
        return 1

    src = target.read_text(encoding="utf-8")
    if MARKER in src:
        print("already patched - nothing to do")
        return 0

    if ANCHOR not in src or TAIL_ANCHOR not in src:
        print(
            "could not find the anchors in tts_chunks_sync - upstream has moved.\n"
            "Patch by hand: add an early return for voice.provider == 'f5_tts_th'\n"
            "that yields from F5TTSProvider.stream(text).",
            file=sys.stderr,
        )
        return 1

    backup = target.with_suffix(".py.orig")
    if not backup.exists():
        shutil.copy2(target, backup)
        print(f"backed up {backup.name}")

    out = src.replace(ANCHOR, INSERT, 1).replace(TAIL_ANCHOR, METHOD + "\n" + TAIL_ANCHOR, 1)
    target.write_text(out, encoding="utf-8")
    print(f"patched {target}")

    if not (root / "server" / "f5_tts_provider.py").exists():
        print("NOTE: copy host/adapters/f5_tts_provider.py into server/", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

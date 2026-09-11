#!/usr/bin/env python3
"""Actually send the voice instructions, and the real date, with every turn.

`server.yaml` has had a careful `hermes.instructions` block since the first
deploy — no markdown, no bullets, answer in one to three sentences, read
numbers as words, never speak a secret. `server.py` reads it nowhere. grep for
`instruction` in the whole file and there are no hits; the only system prompt
it touches belongs to the Anthropic fallback path. Every one of those rules has
been dead config, which is why the assistant keeps replying in bold headings
and emoji and why so much of the Thai normaliser exists to clean up after it.

The same gap explains a bug that survived two attempted fixes. The date in the
agent's context is `Conversation started:`, stamped when the session was
created — and the voice session is persistent by design, reused for weeks. It
said 10 September for as long as that session had existed, so the assistant
confidently reported the wrong day. Setting Hermes' timezone was necessary and
not sufficient: nothing was telling it what today is.

So a short context line rides in front of the transcript on every turn:

    [บริบท: วันศุกร์ที่ 11 กันยายน 2569 เวลา 23:54 น. (Asia/Bangkok) …]
    <what the user said>

It costs about forty tokens a turn and nothing in cache terms — the user's
words are new text every turn anyway. The instructions themselves are sent
once per session rather than on every turn, since repeating them is what would
actually cost something.

    python apply_turn_context.py /path/to/jarvis_ai

Idempotent; writes .orig backups on first run. Run apply_all.py instead of
this directly — the patchers share a .orig base.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

MARKER = "# --- jarvis-hermes: turn context ---"

BLOCK = '''
''' + MARKER + '''
# Thai month names and the Buddhist year, because that is how the date will be
# spoken back. Deriving it here rather than asking the model to convert means
# it cannot get the +543 wrong.
_TH_MONTHS = ["", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
              "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]
_TH_DAYS = ["วันจันทร์", "วันอังคาร", "วันพุธ", "วันพฤหัสบดี", "วันศุกร์",
            "วันเสาร์", "วันอาทิตย์"]
_TURN_CONTEXT_SENT: set = set()


def _now_line() -> str:
    """The real wall clock, in the words the answer will use."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(os.environ.get("JARVIS_TIMEZONE", "Asia/Bangkok"))
        now = datetime.datetime.now(tz)
    except Exception:
        now = datetime.datetime.now().astimezone()
    return (f"{_TH_DAYS[now.weekday()]}ที่ {now.day} {_TH_MONTHS[now.month]} "
            f"{now.year + 543} เวลา {now:%H:%M} น.")


def build_turn_context(cfg: dict, session_id: str) -> str:
    """The block that rides in front of one turn's transcript.

    The instructions go in once per session; the clock goes in every turn,
    because that is the part that changes and the part the agent gets wrong.
    """
    h = (cfg.get("hermes") or {})
    parts = [f"[บริบท: ตอนนี้{_now_line()} เขตเวลา Asia/Bangkok"]
    instructions = (h.get("instructions") or "").strip()
    if instructions and session_id not in _TURN_CONTEXT_SENT:
        _TURN_CONTEXT_SENT.add(session_id)
        parts.append(" " + " ".join(instructions.split()))
    parts.append("]")
    return "".join(parts)


'''

CALL_OLD = '''        timeout = float(h.get("timeout", 240))
        try:
            it = self.hermes.chat_stream_events(session_id, transcript, timeout)'''
CALL_NEW = '''        timeout = float(h.get("timeout", 240))
        # --- jarvis-hermes: turn context ---
        # The agent is told what today is, and (once per session) how to speak
        # for a voice channel. Both were configured and neither was ever sent.
        transcript = build_turn_context(self.cfg, session_id) + "\\n" + transcript
        try:
            it = self.hermes.chat_stream_events(session_id, transcript, timeout)'''

# Typed turns go through /api/chat, which calls Hermes directly rather than
# through _hermes_turn — so without this they were the one path that still had
# no idea what today is.
CHAT_OLD = """            for kind, value in HERMES.chat_stream_events(sid, text, timeout):"""
CHAT_NEW = """            framed = build_turn_context(CFG, sid) + "\\n" + text
            for kind, value in HERMES.chat_stream_events(sid, framed, timeout):"""

STARTUP_ANCHOR = '@app.on_event("startup")\nasync def warm_pipeline() -> None:'


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

    src = server.read_text(encoding="utf-8")
    if "import datetime" not in src:
        src = src.replace("import json\n", "import datetime\nimport json\n", 1)
        server.write_text(src, encoding="utf-8")

    print("server.py:", _patch(server, [
        (STARTUP_ANCHOR, BLOCK.lstrip("\n") + STARTUP_ANCHOR),
        (CALL_OLD, CALL_NEW),
        (CHAT_OLD, CHAT_NEW),
    ], MARKER))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

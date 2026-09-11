#!/usr/bin/env python3
"""Regenerate the environment-variable table in docs/CONFIGURATION.md.

Hand-maintained lists of settings go stale the first time somebody is in a
hurry, and a settings doc that is missing the setting you need is worse than
no doc — you conclude the knob does not exist. This reads the defaults out of
the source instead.

    python3 scripts/env-table.py          # print the table
    python3 scripts/env-table.py --check  # non-zero if the doc is out of date
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOTS = ["gpu-node", "host", "scripts"]
PY_GET = re.compile(
    r'os\.environ\.get\(\s*["\'](JARVIS_[A-Z0-9_]+)["\']\s*'
    r'(?:,\s*("([^"]*)"|\'([^\']*)\'|[^)]*))?\)')
SH_GET = re.compile(r'\$\{(JARVIS_[A-Z0-9_]+):-([^}]*)\}')
MARK_START = "## ตัวแปรทั้งหมด"
MARK_END = "ส่วนที่ขึ้นต้นด้วย"


def collect() -> list[tuple[str, str, str]]:
    found: dict[str, tuple[str, str]] = {}
    for r in ROOTS:
        for f in (ROOT / r).rglob("*"):
            if f.suffix not in (".py", ".sh") or not f.is_file():
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            for m in PY_GET.finditer(text):
                default = (m.group(3) if m.group(3) is not None else
                           m.group(4) if m.group(4) is not None else (m.group(2) or ""))
                found.setdefault(m.group(1), (default.strip(), str(f)))
            for m in SH_GET.finditer(text):
                found.setdefault(m.group(1), (m.group(2).strip(), str(f)))
    rows = []
    for name in sorted(found):
        default, src = found[name]
        default = default or "—"
        if len(default) > 46:
            default = default[:43] + "…"
        side = ("GPU node" if "gpu-node" in src
                else "host" if "/host/" in src else "สคริปต์")
        rows.append((name, default, side))
    return rows


def render(rows) -> str:
    out = ["| ตัวแปร | ค่าเริ่มต้น | ฝั่ง |", "|---|---|---|"]
    out += [f"| `{n}` | `{d}` | {s} |" for n, d, s in rows]
    return "\n".join(out)


def main(argv: list[str]) -> int:
    table = render(collect())
    doc = ROOT / "docs" / "CONFIGURATION.md"
    if "--check" not in argv:
        print(table)
        return 0
    text = doc.read_text(encoding="utf-8")
    try:
        body = text.split(MARK_START, 1)[1].split(MARK_END, 1)[0]
    except IndexError:
        print("table markers not found in CONFIGURATION.md", file=sys.stderr)
        return 1
    missing = [n for n, _, _ in collect() if f"`{n}`" not in body]
    if missing:
        print("CONFIGURATION.md is missing: " + ", ".join(missing), file=sys.stderr)
        return 1
    print(f"all {len(collect())} variables documented")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

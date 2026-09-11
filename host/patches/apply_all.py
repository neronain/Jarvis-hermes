#!/usr/bin/env python3
"""Apply every jarvis_ai patch, in order, from a clean base.

The individual patchers each rebuild from the shared ``.orig`` snapshot when
they find the file already modified — which is correct in isolation and wrong
together: re-running one of them silently reverts the others. That is not a
theoretical hazard, it is what happened; re-applying the hands-free patch put
the HUD's MODELS LOADOUT panel back to upstream's hardcoded values.

So patching is a single operation over the whole set rather than something you
do one file at a time. Restore from .orig once, then apply everything.

    python apply_all.py /path/to/jarvis_ai

Adding a patcher means adding it to PATCHES below, nowhere else.
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

# Order matters only where anchors overlap; today they don't. Kept explicit so
# a future conflict is resolved here rather than by import order.
PATCHES = ["apply_f5_tts.py", "apply_hud_fixes.py", "apply_handsfree.py",
           "apply_voicemode.py"]

# Every file any patcher touches, so the base is clean before the first one runs.
TOUCHED = ["server/server.py", "server/hud/index.html"]


def _restore(root: Path) -> None:
    for rel in TOUCHED:
        target = root / rel
        backup = target.with_suffix(target.suffix + ".orig")
        if backup.exists():
            shutil.copy2(backup, target)
            print(f"  restored {rel} from .orig")


def _run(patcher: Path, root: Path) -> int:
    spec = importlib.util.spec_from_file_location(patcher.stem, patcher)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main(["", str(root)])


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    root = Path(argv[1]).expanduser().resolve()
    if not (root / "server" / "server.py").exists():
        print(f"not a jarvis_ai checkout: {root}", file=sys.stderr)
        return 1

    here = Path(__file__).resolve().parent
    print("restoring the base")
    _restore(root)

    for name in PATCHES:
        patcher = here / name
        if not patcher.exists():
            print(f"missing patcher: {name}", file=sys.stderr)
            return 1
        print(f"\n--- {name} ---")
        rc = _run(patcher, root)
        if rc != 0:
            print(f"{name} failed with {rc}", file=sys.stderr)
            return rc

    print("\nall patches applied")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

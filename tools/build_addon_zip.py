#!/usr/bin/env python3
"""Build ``dist/blenderbridge_addon.zip`` - the file to install in Blender."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "blenderbridge_addon"
TARGET = ROOT / "dist" / "blenderbridge_addon.zip"

# Everything Blender needs. __pycache__ and editor files are skipped.
SKIP_DIRS = {"__pycache__", ".git", ".idea", ".vscode"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".blend1", ".DS_Store"}


def main() -> int:
    if not SOURCE.is_dir():
        print(f"error: {SOURCE} not found", file=sys.stderr)
        return 1
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(TARGET, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(SOURCE.rglob("*")):
            if path.is_dir() or any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.suffix in SKIP_SUFFIXES:
                continue
            arcname = Path("blenderbridge_addon") / path.relative_to(SOURCE)
            zf.write(path, arcname.as_posix())
            count += 1
    size = TARGET.stat().st_size
    print(f"built {TARGET} ({count} files, {size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Copy the pure-Python core modules into the Blender add-on bundle.

The add-on must be installable as a single zip and must not depend on the
connector being importable, so the shared modules are copied.  Run this after
changing anything in ``blenderbridge/{protocol,geometry,regions,relations}.py``
and commit the result - ``tests/test_shared_sync.py`` fails if you forget.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHARED = ["protocol.py", "geometry.py", "regions.py", "relations.py"]

HEADER = """# AUTO-GENERATED - do not edit.
# Source: blenderbridge/{name}
# Edit the source file and run: python tools/sync_shared.py
# (comments, so ``from __future__ import annotations`` stays the first statement)

"""


def main() -> int:
    src_dir = ROOT / "blenderbridge"
    dst_dir = ROOT / "blenderbridge_addon" / "shared"
    dst_dir.mkdir(parents=True, exist_ok=True)
    digests = {}
    for name in SHARED:
        # Read as bytes and normalise CRLF so the digest is identical on
        # Windows and Linux checkouts.
        source = (src_dir / name).read_bytes().replace(b"\r\n", b"\n")
        target = dst_dir / name
        target.write_bytes(HEADER.format(name=name).encode("utf-8") + source)
        digests[name] = hashlib.sha256(source).hexdigest()
        print(f"synced {name}")
    # a tiny copy of the catalogue so the add-on can verify tool parity
    sys.path.insert(0, str(ROOT))
    from blenderbridge import tools as catalogue  # noqa: E402

    cat_path = dst_dir / "tools_catalogue.py"
    entries = [(name, spec.risk, spec.mutating) for name, spec in sorted(catalogue.TOOLS.items())]
    with open(cat_path, "w", encoding="utf-8") as fh:
        fh.write('"""AUTO-GENERATED tool catalogue (names, risk, mutating)."""\n\n')
        fh.write("TOOLS = {\n")
        for name, risk, mutating in entries:
            fh.write(f"    {name!r}: ({risk!r}, {mutating!r}),\n")
        fh.write("}\n\n\n")
        fh.write("def names():\n    return sorted(TOOLS)\n\n\n")
        fh.write("def risk_of(name, default='high'):\n"
                 "    entry = TOOLS.get(name)\n"
                 "    return entry[0] if entry else default\n")
    print(f"synced tools_catalogue.py ({len(entries)} tools)")

    init = dst_dir / "__init__.py"
    init.write_text('"""Shared pure-Python modules (generated copies)."""\n', encoding="utf-8")
    (dst_dir / "SYNC_DIGESTS.json").write_bytes(
        (__import__("json").dumps(digests, indent=2) + "\n").encode("utf-8"))
    print(f"{len(SHARED)} files synced to {dst_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

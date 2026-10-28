"""The add-on ships generated copies of the shared modules - keep them in sync."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHARED = ROOT / "blenderbridge_addon" / "shared"
SOURCE = ROOT / "blenderbridge"
FILES = ["protocol.py", "geometry.py", "regions.py", "relations.py"]


def test_copies_match_source():
    for name in FILES:
        source = (SOURCE / name).read_text(encoding="utf-8").replace("\r\n", "\n")
        target = (SHARED / name).read_text(encoding="utf-8").replace("\r\n", "\n")
        assert target.rstrip().endswith(source.rstrip()), (
            f"{name} in blenderbridge_addon/shared is stale - run "
            f"'python tools/sync_shared.py' and commit the result"
        )


def test_digest_file_matches():
    digests = json.loads((SHARED / "SYNC_DIGESTS.json").read_text(encoding="utf-8"))
    for name in FILES:
        # Newlines are normalised so a CRLF checkout (Windows, core.autocrlf)
        # produces the same digest as an LF checkout.
        raw = (SOURCE / name).read_bytes().replace(b"\r\n", b"\n")
        actual = hashlib.sha256(raw).hexdigest()
        assert digests.get(name) == actual, f"{name} digest mismatch - re-run sync_shared.py"


def test_tool_catalogue_in_addon_matches_connector():
    from blenderbridge import tools

    text = (SHARED / "tools_catalogue.py").read_text(encoding="utf-8")
    for name in tools.names():
        assert f"'{name}'" in text, f"{name} missing from the add-on catalogue"

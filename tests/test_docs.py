"""The documentation must not drift away from the implementation.

These tests are deliberately cheap: they parse the markdown and check every
claim that can be checked mechanically - tool names, JSON snippets, file
references, generated reference pages.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from blenderbridge import tools

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DOC_FILES = sorted(p for p in DOCS.glob("*.md")) + [ROOT / "README.md"]

TOOL_REF = re.compile(r"`([a-z][a-z0-9_]+\.[a-z][a-z0-9_.]*)`")


def _read(path):
    return path.read_text(encoding="utf-8")


def test_docs_exist_and_are_not_stubs():
    for name in ("protocol.md", "tools.md", "security.md", "relay.md",
                 "roblox.md", "workflows.md"):
        path = DOCS / name
        assert path.is_file(), f"docs/{name} fehlt"
        assert len(_read(path)) > 2000, f"docs/{name} ist zu dünn"
    readme = _read(ROOT / "README.md")
    assert len(readme) > 5000
    assert "BlenderBridge" in readme


@pytest.mark.parametrize("path", DOC_FILES, ids=lambda p: p.name)
def test_referenced_tool_names_are_real(path):
    text = _read(path)
    referenced = set(TOOL_REF.findall(text))
    known = set(tools.names())
    # only judge names that look like a tool we own
    candidates = {n for n in referenced if n.split(".")[0] in
                  {k.split(".")[0] for k in known}}
    unknown = {n for n in candidates if n not in known}
    assert not unknown, f"{path.name} nennt unbekannte Werkzeuge: {sorted(unknown)}"


@pytest.mark.parametrize("path", DOC_FILES, ids=lambda p: p.name)
def test_json_examples_are_valid(path):
    text = _read(path)
    blocks = re.findall(r"```jsonc?\n(.*?)```", text, flags=re.S)
    checked = 0
    for block in blocks:
        body = block.strip()
        if not body.startswith(("{", "[")):
            continue
        # allow // comments in jsonc blocks
        cleaned = re.sub(r"^\s*//.*$", "", body, flags=re.M)
        cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
        # A documentation block may contain one value or a sequence of values
        # (the socket examples show three event lines in a row).  Decode
        # greedily and require that nothing but whitespace is left over.
        decoder = json.JSONDecoder()
        pos = 0
        while pos < len(cleaned):
            while pos < len(cleaned) and cleaned[pos] in " \t\r\n":
                pos += 1
            if pos >= len(cleaned):
                break
            try:
                _, pos = decoder.raw_decode(cleaned, pos)
            except ValueError as exc:
                pytest.fail(f"{path.name}: kaputter JSON-Block ({exc}):\n{body[:300]}")
        checked += 1
    assert checked >= 0


def test_json_examples_with_tool_field_name_real_tools():
    for path in DOC_FILES:
        text = _read(path)
        for block in re.findall(r"```jsonc?\n(.*?)```", text, flags=re.S):
            for match in re.finditer(r'"tool"\s*:\s*"([^"]+)"', block):
                name = match.group(1)
                assert name in set(tools.names()), f"{path.name}: unbekanntes Werkzeug {name}"


def test_documented_tool_count_matches():
    readme = _read(ROOT / "README.md")
    assert "67" in readme
    assert len(tools.names()) == 67, (
        "Der README nennt 67 Werkzeuge - wenn sich das ändert, README und "
        "docs/tools.md neu erzeugen.")


def test_generated_tool_reference_is_up_to_date():
    target = DOCS / "tools.md"
    before = _read(target)
    result = subprocess.run([sys.executable, str(ROOT / "tools" / "gen_docs.py")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    after = _read(target)
    assert before == after, ("docs/tools.md war veraltet - 'python tools/gen_docs.py' "
                             "ausführen und committen")


def test_readme_links_to_existing_files():
    readme = _read(ROOT / "README.md")
    for match in re.finditer(r"`(docs/[\w./-]+)`", readme):
        rel = match.group(1)
        assert (ROOT / rel).exists(), f"README verweist auf fehlende Datei {rel}"


def test_docs_do_not_promise_unverified_automation():
    """Guard against 'the AI does it all by itself' marketing creeping in."""
    for path in DOC_FILES:
        text = _read(path).lower()
        for forbidden in ("garantiert fehlerfrei", "vollautomatisch perfekt",
                          "100% genau", "perfekt automatisch"):
            assert forbidden not in text, f"{path.name} enthält eine leere Behauptung"

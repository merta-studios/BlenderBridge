"""Guards for the packaging inputs: nothing here needs PyInstaller or Windows."""

import importlib
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "blenderbridge.spec"


def test_spec_exists_and_has_required_pieces():
    text = SPEC.read_text(encoding="utf-8")
    assert "console=True" in text
    assert 'name="BlenderBridge"' in text
    assert "ui.html" in text, "the web UI must be bundled"
    assert "packaging" in text and "entry.py" in text


def test_spec_hidden_imports_are_real_modules():
    text = SPEC.read_text(encoding="utf-8")
    block = text.split("hiddenimports=[", 1)[1].split("]", 1)[0]
    modules = re.findall(r'"([\w\.]+)"', block)
    assert len(modules) >= 10
    for name in modules:
        if name.startswith("blenderbridge"):
            importlib.import_module(name)


def test_entry_point_imports():
    text = (ROOT / "packaging" / "entry.py").read_text(encoding="utf-8")
    assert "blenderbridge.app" in text
    assert "freeze_support" in text


def test_ui_html_is_shipped_next_to_the_package():
    ui = ROOT / "blenderbridge" / "ui.html"
    assert ui.is_file()
    html = ui.read_text(encoding="utf-8")
    assert "__BB_BOOTSTRAP__" in html
    assert "NOT-AUS" in html


def test_addon_zip_can_be_built(tmp_path):
    import subprocess
    import sys

    script = ROOT / "tools" / "build_addon_zip.py"
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    target = ROOT / "dist" / "blenderbridge_addon.zip"
    assert target.is_file()
    with zipfile.ZipFile(target) as zf:
        names = set(zf.namelist())
    required = {
        "blenderbridge_addon/__init__.py",
        "blenderbridge_addon/server.py",
        "blenderbridge_addon/helpers.py",
        "blenderbridge_addon/facts.py",
        "blenderbridge_addon/ui.py",
        "blenderbridge_addon/ops/mesh.py",
        "blenderbridge_addon/ops/relation.py",
        "blenderbridge_addon/ops/preview.py",
        "blenderbridge_addon/shared/protocol.py",
        "blenderbridge_addon/shared/relations.py",
        "blenderbridge_addon/shared/tools_catalogue.py",
    }
    missing = required - names
    assert not missing, f"missing from add-on zip: {missing}"
    assert not any("__pycache__" in n for n in names)


def test_bl_info_is_parseable():
    text = (ROOT / "blenderbridge_addon" / "__init__.py").read_text(encoding="utf-8")
    assert '"name": "BlenderBridge"' in text
    assert '"blender": (4, 0, 0)' in text


def test_workflows_exist_and_are_valid_yaml():
    try:
        import yaml  # optional dependency
    except ImportError:
        yaml = None
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    names = {w.name for w in workflows}
    assert {"ci.yml", "build-windows.yml", "release.yml"} <= names
    for workflow in workflows:
        text = workflow.read_text(encoding="utf-8")
        assert "runs-on:" in text
        if yaml:
            data = yaml.safe_load(text)
            assert isinstance(data, dict) and "jobs" in data

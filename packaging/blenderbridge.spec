# PyInstaller spec for the Windows connector.
#
# Build (from the repository root):
#     pyinstaller packaging/blenderbridge.spec --clean --noconfirm
#
# Result: dist/BlenderBridge.exe  (single file, console window shows the status)
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # the web UI is loaded from disk at runtime
        (str(ROOT / "blenderbridge" / "ui.html"), "blenderbridge"),
    ],
    hiddenimports=[
        "blenderbridge.api",
        "blenderbridge.app",
        "blenderbridge.audit",
        "blenderbridge.config",
        "blenderbridge.fakeaddon",
        "blenderbridge.jobs",
        "blenderbridge.pairing",
        "blenderbridge.protocol",
        "blenderbridge.proxy",
        "blenderbridge.relations",
        "blenderbridge.relay",
        "blenderbridge.regions",
        "blenderbridge.security",
        "blenderbridge.selftest",
        "blenderbridge.sealing",
        "blenderbridge.tunnel",
        "blenderbridge.geometry",
        "http.server",
        "ssl",
        "ctypes.wintypes",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "PIL", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="BlenderBridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # the console prints the URL, the token and errors
    disable_windowed_traceback=False,
    icon=None,
)

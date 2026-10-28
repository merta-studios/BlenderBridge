#!/usr/bin/env python3
"""Fetch / verify a genuine ``cloudflared`` binary - or just inspect what you have.

BlenderBridge never downloads executables on its own: the tunnel simply stays
off until a binary exists.  This helper is the deliberate, manual step so that
"the program downloaded something" can never happen behind your back.

Usage
-----
    # what would be used right now?
    python tools/get_cloudflared.py --check

    # download the official release for this OS into ./bin and print the hash
    python tools/get_cloudflared.py

    # verify a file you downloaded by hand against a published checksum
    python tools/get_cloudflared.py --check --file C:\\tools\\cloudflared.exe \\
        --expect 4f3a...   # sha256 from the Cloudflare release page

Only URLs under github.com/cloudflare/cloudflared are used, https only, and the
binary is written with a printed SHA-256 so you can compare it with the value
Cloudflare publishes (release notes / checksums file).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / "bin"
API = "https://api.github.com/repos/cloudflare/cloudflared/releases/latest"
ALLOWED_HOSTS = {"github.com", "objects.githubusercontent.com",
                 "api.github.com", "release-assets.githubusercontent.com"}


def asset_name() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        return "cloudflared-windows-amd64.exe" if machine in ("amd64", "x86_64") else \
               "cloudflared-windows-386.exe"
    if system == "darwin":
        return "cloudflared-darwin-amd64.tgz" if machine in ("x86_64", "amd64") else \
               "cloudflared-darwin-arm64.tgz"
    return "cloudflared-linux-amd64" if machine in ("x86_64", "amd64") else \
           "cloudflared-linux-arm64"


def binary_name() -> str:
    return "cloudflared.exe" if os.name == "nt" else "cloudflared"


def candidate_paths() -> list:
    """Where the connector looks for the binary (first hit wins)."""
    out = []
    override = os.environ.get("BB_CLOUDFLARED")
    if override:
        out.append(Path(override))
    out.append(ROOT / "bin" / binary_name())
    out.append(ROOT / binary_name())
    out.append(Path(sys.executable).resolve().parent / binary_name())
    which = shutil.which("cloudflared")
    if which:
        out.append(Path(which))
    return out


def find_binary() -> Path | None:
    for path in candidate_paths():
        if path.is_file():
            return path
    return None


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def version_of(path: Path) -> str:
    import subprocess
    try:
        out = subprocess.run([str(path), "--version"], capture_output=True, text=True,
                             timeout=15)
        return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else "?"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"nicht ausführbar ({exc})"


def latest_release() -> dict:
    req = urllib.request.Request(API, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "BlenderBridge-get_cloudflared"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        import json
        return json.loads(resp.read())


def download(url: str, target: Path) -> None:
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if not url.startswith("https://") or host not in ALLOWED_HOSTS:
        raise SystemExit(f"verweigert: URL zeigt nicht auf GitHub ({url})")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    print(f"lade {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "BlenderBridge-get_cloudflared"})
    with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as fh:
        while True:
            chunk = resp.read(1024 * 128)
            if not chunk:
                break
            fh.write(chunk)
    tmp.replace(target)
    if os.name != "nt":
        target.chmod(0o755)


def cmd_check(args) -> int:
    path = Path(args.file) if args.file else find_binary()
    if not path:
        print("Kein cloudflared gefunden. Gesucht in:")
        for candidate in candidate_paths():
            print(f"  - {candidate}")
        print("\nDer externe Zugang bleibt aus, bis eine geprüfte Datei vorliegt.")
        print("Installieren:  python tools/get_cloudflared.py")
        return 1
    if not path.is_file():
        print(f"nicht gefunden: {path}")
        return 1
    digest = sha256_of(path)
    print(f"Datei:    {path}")
    print(f"Größe:    {path.stat().st_size / 1_048_576:.1f} MB")
    print(f"SHA-256:  {digest}")
    print(f"Version:  {version_of(path)}")
    if args.expect:
        expected = args.expect.strip().lower()
        if expected == digest:
            print("Prüfsumme stimmt überein.")
            return 0
        print(f"PRÜFSUMME STIMMT NICHT (erwartet {expected}) - Datei nicht verwenden!")
        return 2
    print("\nVergleiche die Prüfsumme mit der Angabe im Cloudflare-Release.")
    return 0


def cmd_install(args) -> int:
    target = Path(args.dir) / binary_name()
    if target.exists() and not args.force:
        print(f"{target} existiert bereits - mit --force überschreiben.")
        return cmd_check(argparse.Namespace(file=str(target), expect=args.expect))
    try:
        release = latest_release()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"konnte die Release-Liste nicht laden: {exc}")
        print("Lade die Datei manuell von https://github.com/cloudflare/cloudflared/releases")
        return 1
    wanted = asset_name()
    asset = next((a for a in release.get("assets", []) if a.get("name") == wanted), None)
    if not asset:
        print(f"Release {release.get('tag_name')} enthält {wanted} nicht.")
        print("Verfügbar: " + ", ".join(a.get("name", "?") for a in release.get("assets", [])))
        return 1
    if wanted.endswith(".tgz"):
        print("macOS-Archiv: bitte entpacken und 'cloudflared' nach bin/ kopieren.")
        return 1
    try:
        download(asset["browser_download_url"], target)
    except (urllib.error.URLError, OSError) as exc:
        print(f"\nDownload fehlgeschlagen: {exc}")
        print("Das kann an einer Firewall/Proxy liegen. Alternative: Datei im Browser von")
        print("https://github.com/cloudflare/cloudflared/releases laden und nach")
        print(f"{target} kopieren, danach:  python tools/get_cloudflared.py --check")
        return 1
    print(f"\nGespeichert: {target}")
    print(f"SHA-256:     {sha256_of(target)}")
    print(f"Version:     {version_of(target)}")
    print("\nNächster Schritt: in BlenderBridge unter 'Externer Zugang' einschalten")
    print("(oder cloudflared_path in den Einstellungen auf diese Datei zeigen lassen).")
    print("Die Prüfsumme bitte mit der Cloudflare-Release-Seite vergleichen.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="cloudflared holen/prüfen (nur offizielle Quellen)")
    p.add_argument("--check", action="store_true", help="nur prüfen, nichts laden")
    p.add_argument("--file", default="", help="zu prüfende Datei")
    p.add_argument("--expect", default="", help="erwartete SHA-256")
    p.add_argument("--dir", default=str(DEFAULT_DIR), help="Zielordner (Standard: ./bin)")
    p.add_argument("--force", action="store_true", help="vorhandene Datei überschreiben")
    args = p.parse_args(argv)
    if args.check or args.file:
        return cmd_check(args)
    return cmd_install(args)


if __name__ == "__main__":
    sys.exit(main())

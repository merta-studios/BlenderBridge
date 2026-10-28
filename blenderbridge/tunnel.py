"""Optional public access through Cloudflare Quick Tunnels.

What this does
--------------
Runs ``cloudflared tunnel --url http://127.0.0.1:<port>`` and therefore publishes
the local API to an HTTPS URL of the form ``https://<random>.trycloudflare.com``.
The HTTPS leg ends at Cloudflare; the hop from Cloudflare to your machine is
Cloudflare's own tunnel.  **Anyone with the URL can reach the API**, which is why
the API still requires an agent key for every call, the tunnel is off by default
and the UI shows a large warning while it is open.

What this deliberately does *not* do
------------------------------------
* No automatic download of ``cloudflared`` from an unverified URL.  See
  ``tools/get_cloudflared.py`` for a downloader that verifies the SHA-256 you
  pass in, and the README for the manual route.
* No attempt to hide the fact that a tunnel is public: ``status()["public"]``
  is always true while running.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from . import protocol, security

URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]{2,60}\.trycloudflare\.com", re.IGNORECASE)
VERSION_RE = re.compile(r"cloudflared version ([\w.\-]+)", re.IGNORECASE)


def cloudflared_candidates(configured: str = "", app_dir: Optional[Path] = None) -> List[Path]:
    out: List[Path] = []
    if configured:
        out.append(Path(configured).expanduser())
    base = app_dir or Path.cwd()
    exe = "cloudflared.exe" if platform.system() == "Windows" else "cloudflared"
    out += [base / "bin" / exe, base / exe, base.parent / "bin" / exe]
    found = shutil.which("cloudflared")
    if found:
        out.append(Path(found))
    return out


def find_cloudflared(configured: str = "", app_dir: Optional[Path] = None) -> Optional[Path]:
    for candidate in cloudflared_candidates(configured, app_dir):
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def probe_cloudflared(path: Path, timeout: float = 10.0) -> Dict[str, Any]:
    """Run ``cloudflared --version`` and report what we found."""
    try:
        proc = subprocess.run([str(path), "--version"], capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": security.redact_text(str(exc), 200), "path": str(path)}
    output = (proc.stdout or "") + (proc.stderr or "")
    match = VERSION_RE.search(output)
    return {
        "ok": proc.returncode == 0,
        "path": str(path),
        "version": match.group(1) if match else output.strip()[:120],
        "verified": bool(match),
        "note": "" if match else "Version konnte nicht erkannt werden - ist das wirklich cloudflared?",
    }


class CloudflaredTunnel:
    """State machine around one cloudflared process."""

    def __init__(self, *, local_port: int, config, app_dir: Optional[Path] = None,
                 debug=None):
        self.local_port = int(local_port)
        self.config = config
        self.app_dir = app_dir
        self.debug = debug
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._state = "stopped"
        self._url = ""
        self._error = ""
        self._logs: Deque[str] = deque(maxlen=200)
        self._started_at = 0.0
        self._health: Dict[str, Any] = {}
        self._binary: Optional[Path] = None

    # -- info --------------------------------------------------------------
    @property
    def active_host(self) -> str:
        if not self._url:
            return ""
        return self._url.split("://", 1)[-1].split("/")[0]

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "provider": "cloudflared",
                "state": self._state,
                "url": self._url,
                "host": self.active_host,
                "public": self._state == "running",
                "error": self._error,
                "since": protocol.now_iso(self._started_at) if self._started_at else None,
                "uptime_s": int(time.time() - self._started_at) if self._started_at else 0,
                "health": dict(self._health),
                "logs": list(self._logs)[-25:],
                "binary": str(self._binary) if self._binary else "",
                "warning": ("Diese URL ist öffentlich erreichbar. Jeder Aufruf braucht "
                            "trotzdem einen gültigen Agent-Schlüssel."
                            if self._state == "running" else ""),
            }

    def binary_status(self) -> Dict[str, Any]:
        path = find_cloudflared(getattr(self.config, "cloudflared_path", ""), self.app_dir)
        if not path:
            return {
                "found": False,
                "hint": ("cloudflared nicht gefunden. Entweder in den Ordner 'bin' neben die "
                         "EXE legen (cloudflared.exe) oder unter Einstellungen den Pfad setzen."),
                "candidates": [str(p) for p in
                               cloudflared_candidates(getattr(self.config, "cloudflared_path", ""),
                                                      self.app_dir)],
            }
        info = probe_cloudflared(path)
        info["found"] = True
        return info

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> Dict[str, Any]:
        with self._lock:
            if self._state == "running":
                return self.status()
            self._state = "starting"
            self._error = ""
            self._url = ""
        try:
            binary = find_cloudflared(getattr(self.config, "cloudflared_path", ""), self.app_dir)
            if not binary:
                raise RuntimeError(
                    "cloudflared wurde nicht gefunden. Siehe README, Abschnitt "
                    "'Externer Zugang' - dort steht der geprüfte Downloadweg.")
            info = probe_cloudflared(binary)
            if not info.get("ok"):
                raise RuntimeError(f"cloudflared reagiert nicht wie erwartet: {info.get('error')}")
            self._binary = binary
            cmd = [
                str(binary), "tunnel", "--url", f"http://127.0.0.1:{self.local_port}",
                "--no-autoupdate", "--loglevel", "info",
            ]
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, creationflags=creationflags,
            )
            with self._lock:
                self._proc = proc
                self._started_at = time.time()
            self._reader = threading.Thread(target=self._read_output, args=(proc,),
                                            name="bb-tunnel", daemon=True)
            self._reader.start()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._state = "error"
                self._error = security.redact_text(str(exc), 300)
            self._log(f"Start fehlgeschlagen: {exc}")
            return self.status()

        deadline = time.time() + 40
        while time.time() < deadline:
            with self._lock:
                if self._url:
                    break
                if self._proc and self._proc.poll() is not None:
                    self._state = "error"
                    self._error = "cloudflared wurde sofort beendet: " + " | ".join(
                        list(self._logs)[-3:])
                    return self.status()
            time.sleep(0.25)

        with self._lock:
            if not self._url:
                self._state = "error"
                self._error = ("Keine Tunnel-URL erhalten. Häufigste Ursachen: keine "
                               "Internetverbindung, ausgehende Verbindungen blockiert "
                               "(Firewall/Proxy) oder cloudflared veraltet.")
                proc = self._proc
            else:
                self._state = "running"
                proc = None
        if proc is not None:
            self.stop()
            return self.status()

        self._health = self._check_health()
        with self._lock:
            if not self._health.get("ok"):
                self._error = self._health.get("detail", "")
        self._log(f"Tunnel läuft: {self._url}")
        return self.status()

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            proc, self._proc = self._proc, None
            self._reader = None
            self._state = "stopped"
            self._url = ""
            self._started_at = 0.0
            self._health = {}
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except OSError:
                pass
        self._log("Tunnel gestoppt")
        return self.status()

    # -- internals ---------------------------------------------------------
    def _read_output(self, proc: subprocess.Popen) -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                self._log(line)
                match = URL_RE.search(line)
                if match:
                    with self._lock:
                        if not self._url:
                            self._url = match.group(0)
        except (OSError, ValueError):
            pass

    def _check_health(self) -> Dict[str, Any]:
        """Prove the URL really works from the machine itself."""
        with self._lock:
            url = self._url
        if not url:
            return {"ok": False, "detail": "keine URL"}
        try:
            req = urllib.request.Request(url + "/v1/health",
                                         headers={"User-Agent": "BlenderBridge-healthcheck"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                body = resp.read(400).decode("utf-8", "replace")
            ok = resp.status == 200 and "BlenderBridge" in body
            return {"ok": ok, "status": resp.status,
                    "detail": "" if ok else "unerwartete Antwort über den Tunnel"}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "detail": ("Tunnel-URL wurde erzeugt, ist aber von hier nicht erreichbar: "
                           + security.redact_text(str(exc), 160)),
            }

    def _log(self, line: str) -> None:
        text = security.redact_text(line or "", 400)
        with self._lock:
            self._logs.append(text)
        if self.debug:
            self.debug.add("cloudflared: " + text, "debug")


class DisabledTunnel:
    """Used when the user never enables external access."""

    def __init__(self):
        self.active_host = ""

    def status(self) -> Dict[str, Any]:
        return {"provider": "none", "state": "disabled", "url": "", "public": False,
                "error": "", "warning": "", "logs": [], "health": {}}

    def binary_status(self) -> Dict[str, Any]:
        return {"found": False, "hint": "Externer Zugang ist nicht konfiguriert."}

    def start(self) -> Dict[str, Any]:
        raise RuntimeError("externer Zugang ist deaktiviert")

    def stop(self) -> Dict[str, Any]:
        return self.status()

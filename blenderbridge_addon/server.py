"""The bridge server inside Blender.

Threading contract (the important part)
--------------------------------------
Blender's data is not thread safe.  Therefore:

* a background thread only *reads* the socket and *writes* bytes back;
* every request is put on a queue;
* ``bpy.app.timers`` drains that queue **in Blender's main thread** and runs the
  tool there;
* the response is handed back to the socket from the same main-thread tick.

Nothing touches ``bpy.data`` from the socket thread.  Rendering blocks the UI
for its duration - that is a Blender design constraint, so previews are small.
"""

from __future__ import annotations

import hmac
import json
import os
import platform
import queue
import socket
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import bpy

from . import helpers as H
from .shared import protocol
from .shared.protocol import ErrorCode, ProtocolError

APP_NAME = "BlenderBridge"
TOKEN_FILE = "addon_token.txt"
MAX_LINE = 8 * 1024 * 1024
MAX_RESPONSE = 12 * 1024 * 1024

# ---------------------------------------------------------------------------
# Preferences helper (works with and without the add-on being registered)
# ---------------------------------------------------------------------------


class _FallbackPrefs:
    """Used when the add-on is imported directly (headless tests, scripted use)."""

    def __init__(self) -> None:
        self.host = "127.0.0.1"
        self.port = 9876
        self.token = ""
        self.auto_start = False
        self.allow_script_mode = False
        self.log_requests = True
        self.verbose_errors = False


FALLBACK_PREFS = _FallbackPrefs()


def prefs():
    package = __package__ or "blenderbridge_addon"
    try:
        addon = bpy.context.preferences.addons.get(package)
        if addon and addon.preferences:
            return addon.preferences
    except Exception:  # noqa: BLE001
        pass
    return FALLBACK_PREFS


# ---------------------------------------------------------------------------
# Pairing file (shared with the Windows connector)
# ---------------------------------------------------------------------------


def config_dir() -> Path:
    override = os.environ.get("BLENDERBRIDGE_HOME")
    if override:
        return Path(override).expanduser()
    if platform.system() == "Windows":
        base = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
        return Path(base) / APP_NAME
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / APP_NAME.lower()


def load_or_create_token(preferred: str = "") -> str:
    if preferred:
        return preferred
    path = config_dir() / TOKEN_FILE
    try:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if len(value) >= 24:
                return value
    except OSError:
        pass
    import secrets
    value = secrets.token_urlsafe(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    except OSError:
        pass
    return value


# ---------------------------------------------------------------------------
# Job context + registry
# ---------------------------------------------------------------------------


@dataclass
class JobContext:
    request_id: str
    tool: str
    meta: Dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False
    deadline: float = 0.0
    cancel_event: threading.Event = field(default_factory=threading.Event)
    started: float = field(default_factory=time.time)

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise ProtocolError(ErrorCode.CANCELLED, "Auftrag wurde abgebrochen")
        if self.deadline and time.time() > self.deadline:
            raise ProtocolError(ErrorCode.TIMEOUT,
                                f"Zeitlimit von {self.deadline - self.started:.0f}s überschritten")


class ToolRegistry:
    def __init__(self) -> None:
        self.tools: Dict[str, Callable[[Dict[str, Any], JobContext], Dict[str, Any]]] = {}
        self.notes: List[str] = []

    def register_module(self, module) -> None:
        for name, fn in getattr(module, "TOOLS", {}).items():
            if name in self.tools:
                self.notes.append(f"doppelt registriert: {name}")
            self.tools[name] = fn

    def names(self) -> List[str]:
        return sorted(self.tools)

    def dispatch(self, name: str, params: Dict[str, Any], ctx: JobContext) -> Dict[str, Any]:
        fn = self.tools.get(name)
        if fn is None:
            raise ProtocolError(ErrorCode.TOOL_UNKNOWN, f"unbekanntes Werkzeug: {name}",
                                {"available": self.names()})
        if not isinstance(params, dict):
            raise ProtocolError(ErrorCode.PARAM_INVALID, "params muss ein Objekt sein")
        return fn(params, ctx)


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    from .ops import check, file, mesh, meta, objects, preview, region, relation, scene, script
    for module in (scene, objects, mesh, region, meta, relation, preview, check, file, script):
        registry.register_module(module)
    # parity check against the shared catalogue (the connector relies on it)
    catalogue = set(protocol_tool_names())
    registered = set(registry.tools)
    missing = sorted(catalogue - registered)
    extra = sorted(registered - catalogue)
    if missing:
        registry.notes.append(f"nicht implementiert: {', '.join(missing)}")
    if extra:
        registry.notes.append(f"nicht im Katalog: {', '.join(extra)}")
    return registry


def protocol_tool_names() -> List[str]:
    """Tool names from the shared catalogue shipped next to the add-on."""
    try:
        from .shared import tools_catalogue  # generated file if present
        return tools_catalogue.names()
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------


class BridgeServer:
    def __init__(self) -> None:
        self.registry: Optional[ToolRegistry] = None
        self.sock: Optional[socket.socket] = None
        self.thread: Optional[threading.Thread] = None
        self.queue: "queue.Queue[Tuple[socket.socket, Dict[str, Any]]]" = queue.Queue(maxsize=64)
        self.running = False
        self.host = "127.0.0.1"
        self.port = 0
        self.token = ""
        self.started_at = 0.0
        self.requests = 0
        self.errors = 0
        self.last_error = ""
        self.last_request = ""
        self.clients: List[socket.socket] = []
        self.active: Dict[str, JobContext] = {}
        self.log: List[str] = []

    # -- lifecycle ---------------------------------------------------------
    def start(self, host: str = "127.0.0.1", port: int = 9876, token: str = "") -> Dict[str, Any]:
        if self.running:
            return self.status()
        self.registry = build_registry()
        self.token = load_or_create_token(token)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            sock.close()
            self.last_error = f"Port {port} ist belegt: {exc}"
            raise RuntimeError(self.last_error)
        sock.listen(8)
        sock.settimeout(0.5)
        self.sock = sock
        self.host = host
        self.port = sock.getsockname()[1]
        self.running = True
        self.started_at = time.time()
        self.thread = threading.Thread(target=self._accept_loop, name="bb-addon-server",
                                       daemon=True)
        self.thread.start()
        _ensure_timer()
        self._log(f"Server gestartet auf {self.host}:{self.port}")
        return self.status()

    def stop(self) -> Dict[str, Any]:
        self.running = False
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        for client in list(self.clients):
            try:
                client.close()
            except OSError:
                pass
        self.clients.clear()
        self.sock = None
        self._log("Server gestoppt")
        return self.status()

    def status(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "host": self.host,
            "port": self.port,
            "uptime_s": int(time.time() - self.started_at) if self.started_at else 0,
            "requests": self.requests,
            "errors": self.errors,
            "last_error": self.last_error,
            "last_request": self.last_request,
            "queue": self.queue.qsize(),
            "active": list(self.active.keys()),
            "clients": len(self.clients),
            "tools": len(self.registry.tools) if self.registry else 0,
            "token_hint": (self.token[:4] + "…" + self.token[-4:]) if self.token else "",
            "script_mode": bool(prefs().allow_script_mode),
            "notes": self.registry.notes if self.registry else [],
        }

    # -- socket side (background thread) -----------------------------------
    def _accept_loop(self) -> None:
        while self.running and self.sock is not None:
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.clients.append(conn)
            threading.Thread(target=self._client_loop, args=(conn,), daemon=True).start()

    def _client_loop(self, conn: socket.socket) -> None:
        buffer = b""
        conn.settimeout(None)
        try:
            while self.running:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > MAX_LINE:
                    self._send(conn, protocol.addon_fail(
                        "?", ErrorCode.PAYLOAD_TOO_LARGE, "Anfrage zu groß"))
                    break
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    self._handle_line(conn, line)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            if conn in self.clients:
                self.clients.remove(conn)

    def _handle_line(self, conn: socket.socket, line: bytes) -> None:
        try:
            obj = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send(conn, protocol.addon_fail("?", ErrorCode.BAD_REQUEST, "ungültiges JSON"))
            return
        request_id = str(obj.get("id", "?"))
        if not hmac.compare_digest(str(obj.get("auth", "")), self.token):
            self.errors += 1
            self._send(conn, protocol.addon_fail(request_id, ErrorCode.UNAUTHORIZED,
                                                 "ungültiger Token"))
            return
        try:
            request = protocol.validate_addon_request(obj)
        except ProtocolError as exc:
            self.errors += 1
            self._send(conn, protocol.addon_fail(request_id, exc.code, exc.message))
            return
        try:
            self.queue.put_nowait((conn, request))
        except queue.Full:
            self._send(conn, protocol.addon_fail(request_id, ErrorCode.BLENDER_BUSY,
                                                 "Add-on ist ausgelastet"))

    def _send(self, conn: socket.socket, payload: Dict[str, Any]) -> None:
        try:
            data = (json.dumps(payload, default=str) + "\n").encode("utf-8")
            if len(data) > MAX_RESPONSE:
                data = (json.dumps(protocol.addon_fail(
                    str(payload.get("id", "?")), ErrorCode.PAYLOAD_TOO_LARGE,
                    "Antwort zu groß - mit limit/offset abfragen")) + "\n").encode("utf-8")
            conn.sendall(data)
        except OSError:
            pass

    # -- main thread -------------------------------------------------------
    def pump(self, budget_s: float = 0.35) -> bool:
        """Called from a bpy timer: execute queued requests in the main thread."""
        if not self.running or self.registry is None:
            return True
        deadline = time.time() + budget_s
        processed = 0
        while time.time() < deadline and processed < 4:
            try:
                conn, request = self.queue.get_nowait()
            except queue.Empty:
                break
            processed += 1
            self._execute(conn, request)
        return True

    def _execute(self, conn: socket.socket, request: Dict[str, Any]) -> None:
        tool = request["tool"]
        request_id = request["id"]
        started = time.time()
        self.requests += 1
        self.last_request = f"{tool} ({request_id})"
        ctx = JobContext(request_id=request_id, tool=tool, meta=request.get("meta") or {},
                         dry_run=bool(request.get("dry_run")),
                         deadline=started + float(request.get("timeout_s") or 120))
        if tool == "_control.cancel":
            target = str((request.get("params") or {}).get("target") or "")
            cancelled = False
            for rid, active in list(self.active.items()):
                if rid == target or not target:
                    active.cancel_event.set()
                    cancelled = True
            self._send(conn, protocol.addon_ok(request_id, {"cancelled": cancelled},
                                               duration_ms=int((time.time() - started) * 1000)))
            return
        if tool == "_control.ping":
            self._send(conn, protocol.addon_ok(request_id, {"pong": True},
                                               duration_ms=int((time.time() - started) * 1000)))
            return
        if tool == "_control.status":
            self._send(conn, protocol.addon_ok(request_id, {
                "blender_version": bpy.app.version_string,
                "blender_version_full": ".".join(str(v) for v in bpy.app.version),
                "addon_version": H.ADDON_VERSION,
                "protocol": "1.0",
                "platform": platform.platform()[:80],
                "file": bpy.data.filepath,
                "scene": bpy.context.scene.name if bpy.context.scene else "",
                "tools": self.registry.names() if self.registry else [],
                "server": self.status(),
            }, duration_ms=int((time.time() - started) * 1000)))
            return

        self.active[request_id] = ctx
        try:
            if prefs().log_requests:
                self._log(f"{tool} …")
            result = self.registry.dispatch(tool, request.get("params") or {}, ctx)
            warnings = []
            assumptions = []
            if isinstance(result, dict):
                warnings = list(result.pop("warnings", []) or [])
                assumptions = list(result.pop("assumptions", []) or [])
            duration = int((time.time() - started) * 1000)
            envelope = protocol.addon_ok(request_id, result, warnings=warnings,
                                        assumptions=assumptions, duration_ms=duration)
            # artifacts inside the result are passed through to the connector
            if isinstance(result, dict) and result.get("artifacts"):
                envelope["artifacts"] = result["artifacts"]
            self._send(conn, envelope)
            self._log(f"{tool} ok ({duration} ms)")
        except ProtocolError as exc:
            self.errors += 1
            self.last_error = f"{tool}: {exc.message}"
            self._send(conn, protocol.addon_fail(request_id, exc.code, exc.message,
                                                 **(exc.details or {})))
            self._log(f"{tool} FEHLER {exc.code}: {exc.message}")
        except Exception as exc:  # noqa: BLE001 - never kill Blender
            self.errors += 1
            detail = traceback.format_exc()[-1800:]
            self.last_error = f"{tool}: {H.short_error(exc)}"
            self._send(conn, protocol.addon_fail(
                request_id, ErrorCode.BLENDER_ERROR, H.short_error(exc, 500),
                trace=detail))
            self._log(f"{tool} AUSNAHME: {H.short_error(exc)}")
        finally:
            self.active.pop(request_id, None)

    def _log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        entry = f"{stamp} {message}"
        self.log.append(entry)
        del self.log[:-200]


STATE = BridgeServer()


def _ensure_timer() -> None:
    if not bpy.app.timers.is_registered(_timer_tick):
        bpy.app.timers.register(_timer_tick, first_interval=0.05, persistent=True)


def _timer_tick() -> Optional[float]:
    try:
        STATE.pump()
    except Exception as exc:  # noqa: BLE001
        STATE.last_error = H.short_error(exc)
    return 0.05 if STATE.running else None


def start_server(host: str = "127.0.0.1", port: int = 9876, token: str = "") -> Dict[str, Any]:
    return STATE.start(host, port, token)


def stop_server() -> Dict[str, Any]:
    return STATE.stop()

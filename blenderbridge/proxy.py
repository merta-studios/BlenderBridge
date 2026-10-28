"""Loopback link to the Blender add-on (newline delimited JSON over TCP).

The connector is the *client*; the add-on listens on 127.0.0.1 only.  Both sides
share a token that the connector generates and the user copies into Blender
once (or the Blender side generates and the connector receives, whichever the
user prefers - see ``addon_token_mode`` in the docs).

Threading model: one reader thread, one keepalive thread, a lock around writes.
Requests are matched to responses by id, so several callers may be waiting while
a long render is in flight.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any, Callable, Dict, Optional

from . import protocol, security
from .protocol import ErrorCode, ProtocolError


class BlenderLink:
    def __init__(self, host: str, port: int, token: str,
                 on_state_change: Optional[Callable[[Dict[str, Any]], None]] = None,
                 keepalive_s: float = 5.0, debug=None):
        self.host = host
        self.port = int(port)
        self.token = token
        self.on_state_change = on_state_change
        self.keepalive_s = keepalive_s
        self.debug = debug

        self._sock: Optional[socket.socket] = None
        self._rfile = None
        self._lock = threading.RLock()
        self._pending: Dict[str, "_Pending"] = {}
        self._stop = threading.Event()
        self._threads: list = []
        self._connected = False
        self._info: Dict[str, Any] = {}
        self._last_error = ""
        self._last_contact = 0.0
        self._latency_ms: Optional[float] = None
        self._state_version = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        t1 = threading.Thread(target=self._connect_loop, name="bb-link-connect", daemon=True)
        t2 = threading.Thread(target=self._keepalive_loop, name="bb-link-keepalive", daemon=True)
        self._threads = [t1, t2]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        self._drop("connector shutting down")
        for t in self._threads:
            if t.is_alive():
                t.join(timeout=2.0)
        self._threads = []

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "connected": self._connected,
                "host": self.host,
                "port": self.port,
                "blender_version": self._info.get("blender_version", ""),
                "addon_version": self._info.get("addon_version", ""),
                "protocol": self._info.get("protocol", ""),
                "platform": self._info.get("platform", ""),
                "file": self._info.get("file", ""),
                "latency_ms": self._latency_ms,
                "last_error": self._last_error,
                "last_contact": protocol.now_iso(self._last_contact) if self._last_contact else None,
                "state_version": self._state_version,
            }

    # -- calls -------------------------------------------------------------
    def call(self, tool: str, params: Optional[Dict[str, Any]] = None, *,
             timeout: float = protocol.DEFAULT_JOB_TIMEOUT,
             dry_run: bool = False, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send one request and wait for its response.

        Returns the raw add-on response (``{"ok": True, ...}`` or
        ``{"ok": False, "error": {...}}``).  Transport problems raise
        :class:`ProtocolError`.
        """
        if not self.connected:
            # one short grace attempt: Blender may have restarted
            if not self._try_connect_once():
                raise ProtocolError(ErrorCode.BLENDER_UNAVAILABLE,
                                    "Blender ist nicht verbunden. Add-on starten und Server aktivieren.",
                                    {"host": self.host, "port": self.port})
        return self._request(tool, params, timeout=timeout, dry_run=dry_run, meta=meta)

    def _request(self, tool: str, params: Optional[Dict[str, Any]] = None, *,
                 timeout: float = protocol.DEFAULT_JOB_TIMEOUT,
                 dry_run: bool = False, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send a request on an existing socket (never triggers a reconnect)."""
        request = protocol.addon_request(tool, params or {}, token=self.token,
                                         timeout_s=timeout, dry_run=dry_run, meta=meta)
        rid = request["id"]
        pending = _Pending()
        with self._lock:
            self._pending[rid] = pending
        try:
            self._send_raw(request)
        except OSError as exc:
            with self._lock:
                self._pending.pop(rid, None)
            self._drop(f"send failed: {exc}")
            raise ProtocolError(ErrorCode.BLENDER_UNAVAILABLE,
                                f"Senden an Blender fehlgeschlagen: {exc}")

        if not pending.event.wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            # best effort cancel so Blender does not keep working for nobody
            try:
                self._send_raw(protocol.addon_request("_control.cancel", {"target": rid},
                                                       token=self.token, timeout_s=5.0))
            except OSError:
                pass
            raise ProtocolError(ErrorCode.TIMEOUT,
                                f"Blender hat innerhalb von {timeout:.0f}s nicht geantwortet.",
                                {"tool": tool, "request_id": rid})
        if pending.error:
            raise pending.error
        return pending.response or {"ok": False, "error": protocol.error(
            ErrorCode.INTERNAL, "leere Antwort von Blender")}

    # -- internals ---------------------------------------------------------
    def _try_connect_once(self) -> bool:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=2.0)
        except OSError as exc:
            with self._lock:
                self._last_error = security.redact_text(str(exc), 200)
            return False
        sock.settimeout(None)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        rfile = sock.makefile("rb")
        with self._lock:
            self._sock = sock
            self._rfile = rfile
        threading.Thread(target=self._reader_loop, args=(sock, rfile),
                         name="bb-link-reader", daemon=True).start()
        # authenticate with the shared token; the add-on answers _control.status
        try:
            hello = self._request("_control.status", {}, timeout=5.0)
        except ProtocolError as exc:
            self._drop(f"handshake failed: {exc.message}")
            return False
        if not hello.get("ok"):
            err = hello.get("error") or {}
            self._drop(f"handshake rejected: {err.get('code')} {err.get('message')}")
            return False
        self._set_connected(True, hello.get("result") or {})
        return True

    def _connect_loop(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            if not self.connected:
                if self._try_connect_once():
                    backoff = 0.5
                else:
                    time.sleep(backoff)
                    backoff = min(backoff * 1.6, 5.0)
                    continue
            time.sleep(0.4)

    def _reader_loop(self, sock: socket.socket, rfile) -> None:
        try:
            while not self._stop.is_set():
                line = rfile.readline()
                if not line:
                    break
                self._last_contact = time.time()
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                except ValueError:
                    if self.debug:
                        self.debug.add("ungültige Zeile vom Add-on verworfen", "warn")
                    continue
                self._dispatch(msg)
        except (OSError, ValueError) as exc:
            self._drop(f"reader error: {exc}")
        finally:
            try:
                rfile.close()
            except OSError:
                pass
            self._drop("connection closed")

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        rid = msg.get("id")
        if rid is None:
            # unsolicited event from the add-on (progress, selection change)
            if self.debug and msg.get("event"):
                self.debug.add(f"Blender-Event: {msg.get('event')}")
            return
        with self._lock:
            pending = self._pending.pop(rid, None)
        if pending is None:
            return
        if msg.get("ok") is False:
            err = msg.get("error") or {}
            pending.error = ProtocolError(
                str(err.get("code", ErrorCode.BLENDER_ERROR)),
                str(err.get("message", "Blender meldete einen Fehler")),
                err.get("details") or {},
            )
        else:
            pending.response = msg
        pending.event.set()

    def _send_raw(self, payload: Dict[str, Any]) -> None:
        data = (json.dumps(payload, default=str) + "\n").encode("utf-8")
        with self._lock:
            sock = self._sock
            if sock is None:
                raise OSError("socket is not connected")
            sock.sendall(data)

    def _keepalive_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(self.keepalive_s)
            if not self.connected:
                continue
            started = time.time()
            try:
                resp = self.call("_control.ping", {}, timeout=max(2.0, self.keepalive_s))
            except ProtocolError as exc:
                if exc.code in (ErrorCode.TIMEOUT, ErrorCode.BLENDER_UNAVAILABLE):
                    self._drop(f"keepalive failed: {exc.message}")
                continue
            if resp.get("ok"):
                with self._lock:
                    self._latency_ms = round((time.time() - started) * 1000, 1)
            else:
                self._drop("keepalive rejected")

    def _set_connected(self, value: bool, info: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            changed = self._connected != value
            self._connected = value
            if info:
                self._info = self._normalise_info(info)
                self._last_error = ""
            if changed:
                self._state_version += 1
            state = self.status()
        if self.debug and changed:
            self.debug.add("Blender verbunden" if value else "Blender getrennt",
                           "info" if value else "warn")
        if self.on_state_change and changed:
            try:
                self.on_state_change(state)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _normalise_info(info: Dict[str, Any]) -> Dict[str, Any]:
        keep = ("blender_version", "addon_version", "protocol", "platform", "file",
                "blender_version_full", "scene", "tools", "capabilities")
        out = {k: info.get(k) for k in keep if k in info}
        if isinstance(info.get("file"), str):
            out["file"] = security.redact_text(info["file"], 260)
        return out

    def _drop(self, reason: str) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
            rfile, self._rfile = self._rfile, None
            pending = list(self._pending.values())
            self._pending.clear()
            was_connected = self._connected
            self._connected = False
            if was_connected:
                self._state_version += 1
        for p in pending:
            p.error = ProtocolError(ErrorCode.BLENDER_UNAVAILABLE,
                                    f"Verbindung zu Blender verloren ({security.redact_text(reason, 120)})")
            p.event.set()
        # Unblock the reader thread first: closing a makefile() view from another
        # thread waits for the in-flight read, which is exactly the deadlock we
        # want to avoid.  shutdown() makes readline() return immediately.
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if rfile is not None:
            try:
                rfile.close()
            except (OSError, ValueError):
                pass
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if was_connected:
            with self._lock:
                state = self.status()
            if self.on_state_change:
                try:
                    self.on_state_change(state)
                except Exception:  # noqa: BLE001
                    pass


class _Pending:
    __slots__ = ("event", "response", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: Optional[Dict[str, Any]] = None
        self.error: Optional[ProtocolError] = None

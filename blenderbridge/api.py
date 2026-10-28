"""HTTP API + local web UI.

Two roles:

``ui``
    the browser UI served from ``/`` on loopback.  Gets a per-start session
    token that is embedded into the page.  May change settings, manage agent
    keys, start/stop the tunnel and press the panic button.

``agent``
    an AI agent (or you, from a script) using an agent key.  May read status,
    list tools, submit jobs, answer approvals and download artifacts.  May
    *not* change settings, create keys or open a tunnel - the blast radius of a
    stolen agent key stays small and reviewable.

Every state changing request additionally needs the ``X-BB-Client`` header,
which a plain HTML form cannot set: that closes the classic CSRF hole.  The
``Host`` header is validated as well, which blocks DNS rebinding.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import ssl
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import protocol, security, tools
from .protocol import ErrorCode, ProtocolError

MAX_BODY = 4 * 1024 * 1024
ALLOWED_ARTIFACT_EXT = {".png", ".jpg", ".jpeg", ".json", ".txt", ".glb", ".gltf",
                        ".fbx", ".blend", ".md", ".csv"}


class AppContext:
    """Everything the HTTP layer needs, assembled by the connector."""

    def __init__(self, *, config, store, jobs, link, keys, audit, tunnel=None,
                 relay=None, version: str = "0.0.0", ui_token: str = "",
                 debug=None, panic_hook=None, config_changed=None, limiter=None,
                 addon_token: str = ""):
        self.config = config
        self.store = store
        self.jobs = jobs
        self.link = link
        self.keys = keys
        self.audit = audit
        self.tunnel = tunnel
        self.relay = relay
        self.version = version
        self.ui_token = ui_token
        self.debug = debug
        self.panic_hook = panic_hook
        self.config_changed = config_changed
        self.limiter = limiter
        self.addon_token = addon_token
        self.started_at = time.time()
        self.app_dir = Path(__file__).resolve().parent


class ApiServer:
    def __init__(self, ctx: AppContext, host: str = "127.0.0.1", port: int = 8765,
                 tls_cert: Optional[str] = None, tls_key: Optional[str] = None):
        self.ctx = ctx
        self.host = host
        self.port = int(port)
        self.tls_cert = tls_cert
        self.tls_key = tls_key
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._allowed_hosts = self._build_allowed_hosts()

    def _build_allowed_hosts(self) -> set:
        hosts = {"127.0.0.1", "localhost", "::1", "[::1]", socket.gethostname().lower()}
        for h in (self.ctx.config.allowed_hosts or []):
            hosts.add(str(h).lower().strip())
        return hosts

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> Tuple[str, int]:
        handler = _make_handler(self)
        last_error: Optional[Exception] = None
        for port in range(self.port, self.port + 12):
            try:
                httpd = ThreadingHTTPServer((self.host, port), handler)
            except OSError as exc:
                last_error = exc
                continue
            httpd.daemon_threads = True
            if self.tls_cert and self.tls_key:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(self.tls_cert, self.tls_key)
                httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
            self._httpd = httpd
            self.port = port
            self._thread = threading.Thread(target=httpd.serve_forever,
                                            name="bb-api", daemon=True)
            self._thread.start()
            return self.host, port
        raise RuntimeError(f"kein freier Port gefunden: {last_error}")

    def stop(self) -> None:
        if self._httpd:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._httpd = None

    @property
    def scheme(self) -> str:
        return "https" if (self.tls_cert and self.tls_key) else "http"


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------


def _make_handler(server: ApiServer):
    ctx = server.ctx

    class Handler(BaseHTTPRequestHandler):
        server_version = "BlenderBridge"
        protocol_version = "HTTP/1.1"

        # ---- plumbing ----
        def log_message(self, fmt: str, *args) -> None:  # noqa: D401
            if ctx.debug:
                ctx.debug.add("http " + (fmt % args), "debug")

        def _send(self, status: int, payload: Any, *, content_type: str = "application/json",
                  extra_headers: Optional[Dict[str, str]] = None) -> None:
            if isinstance(payload, (dict, list)):
                body = json.dumps(payload, default=str).encode("utf-8")
            elif isinstance(payload, bytes):
                body = payload
            else:
                body = str(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _error(self, status: int, code: str, message: str, **details) -> None:
            self._send(status, {"ok": False, "error": protocol.error(code, message, **details)})

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ProtocolError(ErrorCode.PAYLOAD_TOO_LARGE,
                                    f"Anfrage zu groß (max {MAX_BODY // 1024 // 1024} MB)")
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ProtocolError(ErrorCode.BAD_REQUEST, "Body ist kein gültiges JSON")
            if not isinstance(data, dict):
                raise ProtocolError(ErrorCode.BAD_REQUEST, "Body muss ein JSON-Objekt sein")
            return data

        def _check_host(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
            allowed = server._allowed_hosts
            if host in allowed:
                return True
            # allow the cloudflare tunnel host that is currently active
            if ctx.tunnel and getattr(ctx.tunnel, "active_host", ""):
                if host == ctx.tunnel.active_host.lower():
                    return True
            if ctx.config.allowed_hosts and host in {h.lower() for h in ctx.config.allowed_hosts}:
                return True
            self._error(HTTPStatus.FORBIDDEN, ErrorCode.FORBIDDEN,
                        "Host nicht erlaubt (Schutz gegen DNS-Rebinding)",
                        host=host, allowed=sorted(allowed))
            return False

        def _role(self) -> Optional[str]:
            """Return 'ui', 'agent' or None after having sent an error."""
            header = self.headers.get("Authorization") or ""
            token = header[7:].strip() if header.lower().startswith("bearer ") else ""
            if not token:
                token = (self.headers.get("X-BB-Token") or "").strip()
            if token and ctx.ui_token and security.constant_time_equals(token, ctx.ui_token):
                if not self._rate_ok("ui:" + token[:8]):
                    return None
                return "ui"
            if token:
                rec = ctx.keys.verify(token)
                if rec:
                    if not self._rate_ok("agent:" + rec.key_id):
                        return None
                    return "agent"
                if ctx.ui_token and security.constant_time_equals(token, ctx.ui_token):
                    return "ui"
            self._send(HTTPStatus.UNAUTHORIZED,
                       {"ok": False, "error": protocol.error(
                           ErrorCode.UNAUTHORIZED,
                           "Fehlender oder ungültiger Token. Header: Authorization: Bearer <key>")},
                       extra_headers={"WWW-Authenticate": 'Bearer realm="BlenderBridge"'})
            return None

        def _rate_ok(self, bucket: str) -> bool:
            if ctx.limiter is None:
                return True
            if ctx.limiter.allow(bucket):
                return True
            self._send(HTTPStatus.TOO_MANY_REQUESTS,
                       {"ok": False, "error": protocol.error(
                           ErrorCode.RATE_LIMITED,
                           "Zu viele Anfragen. Bitte kurz warten.",
                           per_minute=ctx.limiter.per_minute)})
            return False

        def _require_role(self, *roles: str) -> Optional[str]:
            role = self._role()
            if role is None:
                return None
            if roles and role not in roles:
                self._error(HTTPStatus.FORBIDDEN, ErrorCode.FORBIDDEN,
                            f"diese Aktion ist für Rolle '{role}' nicht erlaubt")
                return None
            return role

        def _check_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            host = (self.headers.get("Host") or "").lower()
            parsed = urllib.parse.urlparse(origin)
            if parsed.netloc.lower() == host:
                return True
            allowed = {f"http://{h}" for h in server._allowed_hosts} | {
                f"https://{h}" for h in server._allowed_hosts}
            if origin.lower() in allowed:
                return True
            self._error(HTTPStatus.FORBIDDEN, ErrorCode.FORBIDDEN,
                        "Origin nicht erlaubt", origin=origin)
            return False

        def _check_client_header(self) -> bool:
            if self.headers.get("X-BB-Client"):
                return True
            self._error(HTTPStatus.FORBIDDEN, ErrorCode.FORBIDDEN,
                        "Header 'X-BB-Client' fehlt (CSRF-Schutz). "
                        "Bei curl: -H 'X-BB-Client: 1'")
            return False

        # ---- verbs ----
        def do_GET(self) -> None:  # noqa: N802
            if not self._check_host():
                return
            try:
                path, _, query = self.path.partition("?")
                params = urllib.parse.parse_qs(query)
                if path in ("/", "/index.html"):
                    return self._serve_ui()
                if path == "/v1/health":
                    return self._send(HTTPStatus.OK, {
                        "ok": True, "app": "BlenderBridge", "version": ctx.version,
                        "protocol": protocol.PROTOCOL_VERSION,
                        "time": protocol.now_iso(),
                    })
                if path == "/v1/status":
                    if self._require_role() is None:
                        return
                    return self._send(HTTPStatus.OK, self._status_payload())
                if path == "/v1/tools":
                    if self._require_role() is None:
                        return
                    role = self._require_role() or "agent"
                    catalogue = tools.catalogue()
                    if role != "ui" and not ctx.config.allow_script_mode:
                        catalogue = [t for t in catalogue if t["name"] != "script.run"]
                    return self._send(HTTPStatus.OK, {
                        "ok": True,
                        "protocol": protocol.PROTOCOL_VERSION,
                        "count": len(catalogue),
                        "tools": catalogue,
                    })
                if path == "/v1/jobs":
                    if self._require_role() is None:
                        return
                    limit = int((params.get("limit") or ["50"])[0])
                    state = (params.get("state") or [None])[0]
                    return self._send(HTTPStatus.OK, {
                        "ok": True, "jobs": ctx.jobs.list_jobs(limit=limit, state=state)})
                if path == "/v1/approvals":
                    if self._require_role() is None:
                        return
                    return self._send(HTTPStatus.OK, {
                        "ok": True, "approvals": ctx.jobs.pending_approvals()})
                if path == "/v1/logs":
                    if self._require_role() is None:
                        return
                    limit = int((params.get("limit") or ["100"])[0])
                    return self._send(HTTPStatus.OK, {
                        "ok": True,
                        "audit": ctx.audit.recent(min(limit, 500)) if ctx.audit else [],
                        "debug": ctx.debug.lines(limit) if ctx.debug else [],
                    })
                if path == "/v1/keys":
                    if self._require_role("ui") is None:
                        return
                    return self._send(HTTPStatus.OK, {"ok": True, "keys": ctx.keys.list()})
                if path.startswith("/v1/jobs/"):
                    return self._job_get(path, params)
                if path == "/v1/tunnel":
                    if self._require_role() is None:
                        return
                    return self._send(HTTPStatus.OK, self._tunnel_payload())
                if path == "/v1/relay":
                    if self._require_role() is None:
                        return
                    return self._send(HTTPStatus.OK, self._relay_payload())
                return self._error(HTTPStatus.NOT_FOUND, ErrorCode.NOT_FOUND,
                                   f"unbekannter Pfad: {path}")
            except ProtocolError as exc:
                self._error(HTTPStatus.BAD_REQUEST, exc.code, exc.message, **exc.details)
            except Exception as exc:  # noqa: BLE001
                if ctx.debug:
                    ctx.debug.add(f"GET {self.path} failed: {exc}", "error")
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, ErrorCode.INTERNAL,
                            security.redact_text(str(exc)))

        def do_POST(self) -> None:  # noqa: N802
            if not self._check_host() or not self._check_origin():
                return
            try:
                path, _, _ = self.path.partition("?")
                if path == "/v1/jobs":
                    if self._require_role() is None:
                        return
                    if not self._check_client_header():
                        return
                    body = self._body()
                    job = ctx.jobs.submit(body, actor="agent")
                    status = HTTPStatus.ACCEPTED
                    return self._send(status, {"ok": True, "job": job})
                if path.startswith("/v1/jobs/"):
                    return self._job_post(path)
                if path == "/v1/trust":
                    if self._require_role("ui") is None:
                        return
                    if not self._check_client_header():
                        return
                    body = self._body()
                    seconds = body.get("seconds")
                    ctx.jobs.policy.grant_trust(int(seconds) if seconds is not None else None)
                    return self._send(HTTPStatus.OK, {
                        "ok": True, "trust_seconds_left": ctx.jobs.policy.trust_remaining()})
                if path == "/v1/panic":
                    if self._require_role() is None:
                        return
                    if not self._check_client_header():
                        return
                    body = self._body()
                    actions = self._panic(body)
                    return self._send(HTTPStatus.OK, {"ok": True, "actions": actions})
                if path == "/v1/keys":
                    if self._require_role("ui") is None:
                        return
                    if not self._check_client_header():
                        return
                    body = self._body()
                    label = str(body.get("label") or "agent")[:60]
                    key, rec = ctx.keys.create(label)
                    self._persist_keys()
                    if ctx.audit:
                        ctx.audit.write({"event": "agent_key_created", "key_id": rec.key_id,
                                         "label": label})
                    return self._send(HTTPStatus.CREATED, {
                        "ok": True,
                        "key": key,
                        "key_id": rec.key_id,
                        "warning": "Dieser Schlüssel wird nur einmal angezeigt. Jetzt kopieren!",
                    })
                if path == "/v1/config":
                    if self._require_role("ui") is None:
                        return
                    if not self._check_client_header():
                        return
                    body = self._body()
                    return self._update_config(body)
                if path == "/v1/tunnel":
                    if self._require_role("ui") is None:
                        return
                    if not self._check_client_header():
                        return
                    body = self._body()
                    action = str(body.get("action", "start")).lower()
                    if ctx.tunnel is None:
                        return self._error(HTTPStatus.SERVICE_UNAVAILABLE,
                                           ErrorCode.TUNNEL_ERROR, "Tunnel nicht konfiguriert")
                    if action == "start":
                        ctx.tunnel.start()
                    elif action == "stop":
                        ctx.tunnel.stop()
                    elif action == "restart":
                        ctx.tunnel.stop()
                        ctx.tunnel.start()
                    else:
                        return self._error(HTTPStatus.BAD_REQUEST, ErrorCode.BAD_REQUEST,
                                           "action muss start|stop|restart sein")
                    return self._send(HTTPStatus.OK, self._tunnel_payload())
                if path == "/v1/relay":
                    if self._require_role("ui") is None:
                        return
                    if not self._check_client_header():
                        return
                    if ctx.relay is None:
                        return self._error(HTTPStatus.SERVICE_UNAVAILABLE,
                                           ErrorCode.RELAY_ERROR, "Relay nicht konfiguriert")
                    body = self._body()
                    return self._send(HTTPStatus.OK, ctx.relay.handle_control(body))
                return self._error(HTTPStatus.NOT_FOUND, ErrorCode.NOT_FOUND,
                                   f"unbekannter Pfad: {path}")
            except ProtocolError as exc:
                status = HTTPStatus.BAD_REQUEST
                if exc.code == ErrorCode.TOOL_UNKNOWN:
                    status = HTTPStatus.BAD_REQUEST
                elif exc.code == ErrorCode.FORBIDDEN:
                    status = HTTPStatus.FORBIDDEN
                elif exc.code == ErrorCode.RATE_LIMITED:
                    status = HTTPStatus.TOO_MANY_REQUESTS
                self._error(status, exc.code, exc.message, **exc.details)
            except Exception as exc:  # noqa: BLE001
                if ctx.debug:
                    ctx.debug.add(f"POST {self.path} failed: {exc}", "error")
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, ErrorCode.INTERNAL,
                            security.redact_text(str(exc)))

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._check_host() or not self._check_origin():
                return
            path, _, _ = self.path.partition("?")
            if path.startswith("/v1/keys/"):
                if self._require_role("ui") is None:
                    return
                if not self._check_client_header():
                    return
                key_id = path.rsplit("/", 1)[-1]
                ok = ctx.keys.revoke(key_id)
                self._persist_keys()
                if ctx.audit:
                    ctx.audit.write({"event": "agent_key_revoked", "key_id": key_id, "ok": ok})
                return self._send(HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND,
                                  {"ok": ok, "revoked": key_id})
            if path == "/v1/trust":
                if self._require_role("ui") is None:
                    return
                ctx.jobs.policy.revoke_trust()
                return self._send(HTTPStatus.OK, {"ok": True})
            return self._error(HTTPStatus.NOT_FOUND, ErrorCode.NOT_FOUND, "unbekannter Pfad")

        # ---- specialised handlers ----
        def _serve_ui(self) -> None:
            ui_path = ctx.app_dir / "ui.html"
            try:
                html = ui_path.read_text(encoding="utf-8")
            except OSError:
                return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, ErrorCode.INTERNAL,
                                   "ui.html nicht gefunden")
            # initial state is embedded so the page works without a second round trip
            bootstrap = json.dumps({
                "token": ctx.ui_token,
                "status": self._status_payload(),
                "tools": tools.catalogue(),
                "version": ctx.version,
                "protocol": protocol.PROTOCOL_VERSION,
                "addon_token": ctx.addon_token,
            }, default=str).replace("</", "<\\/")
            html = html.replace("__BB_BOOTSTRAP__", bootstrap)
            return self._send(HTTPStatus.OK, html, content_type="text/html; charset=utf-8")

        def _status_payload(self) -> Dict[str, Any]:
            blender = ctx.link.status() if ctx.link else {"connected": False}
            return {
                "ok": True,
                "app": "BlenderBridge",
                "version": ctx.version,
                "protocol": protocol.PROTOCOL_VERSION,
                "uptime_s": int(time.time() - ctx.started_at),
                "blender": blender,
                "jobs": ctx.jobs.snapshot_state(),
                "approvals": ctx.jobs.pending_approvals(),
                "policy": {
                    "threshold": ctx.jobs.policy.threshold,
                    "trust_seconds_left": ctx.jobs.policy.trust_remaining(),
                    "script_mode": ctx.config.allow_script_mode,
                },
                "tunnel": self._tunnel_payload().get("tunnel"),
                "relay": self._relay_payload().get("relay"),
                "config": ctx.config.to_public_dict(),
            }

        def _tunnel_payload(self) -> Dict[str, Any]:
            if not ctx.tunnel:
                return {"ok": True, "tunnel": {"state": "disabled", "url": ""}}
            payload = ctx.tunnel.status()
            try:
                payload["binary_status"] = ctx.tunnel.binary_status()
            except Exception:  # noqa: BLE001
                payload["binary_status"] = {}
            return {"ok": True, "tunnel": payload}

        def _relay_payload(self) -> Dict[str, Any]:
            if not ctx.relay:
                return {"ok": True, "relay": {"state": "disabled"}}
            return {"ok": True, "relay": ctx.relay.status()}

        def _job_get(self, path: str, params: Dict[str, List[str]]) -> None:
            if self._require_role() is None:
                return
            parts = [p for p in path.split("/") if p]
            # /v1/jobs/<id>  or  /v1/jobs/<id>/artifacts/<name>
            if len(parts) == 3:
                job_id = parts[2]
                wait = float((params.get("wait") or ["0"])[0])
                if wait > 0:
                    job = ctx.jobs.wait(job_id, min(wait, 120.0))
                else:
                    job = ctx.jobs.get(job_id)
                return self._send(HTTPStatus.OK, {"ok": True, "job": job})
            if len(parts) == 5 and parts[3] == "artifacts":
                return self._serve_artifact(parts[2], parts[4])
            return self._error(HTTPStatus.NOT_FOUND, ErrorCode.NOT_FOUND, "unbekannter Pfad")

        def _serve_artifact(self, job_id: str, name: str) -> None:
            safe = Path(name).name
            if safe != name or safe.startswith("."):
                return self._error(HTTPStatus.BAD_REQUEST, ErrorCode.BAD_REQUEST,
                                   "ungültiger Dateiname")
            base = (ctx.jobs.job_dir / job_id).resolve()
            target = (base / safe).resolve()
            if base != target.parent or not target.is_file():
                return self._error(HTTPStatus.NOT_FOUND, ErrorCode.NOT_FOUND,
                                   "Datei nicht gefunden")
            if target.suffix.lower() not in ALLOWED_ARTIFACT_EXT:
                return self._error(HTTPStatus.FORBIDDEN, ErrorCode.FORBIDDEN,
                                   "Dateityp wird nicht ausgeliefert")
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            data = target.read_bytes()
            return self._send(HTTPStatus.OK, data, content_type=ctype)

        def _job_post(self, path: str) -> None:
            role = self._require_role()
            if role is None:
                return
            if not self._check_client_header():
                return
            parts = [p for p in path.split("/") if p]
            if len(parts) != 4:
                return self._error(HTTPStatus.NOT_FOUND, ErrorCode.NOT_FOUND, "unbekannter Pfad")
            job_id, action = parts[2], parts[3]
            body = self._body()
            if action in ("approve", "deny"):
                approved = action == "approve"
                job = ctx.jobs.approve(job_id, approved, str(body.get("reason", "")),
                                       actor=role)
                return self._send(HTTPStatus.OK, {"ok": True, "job": job})
            if action == "cancel":
                job = ctx.jobs.cancel(job_id, actor=role)
                return self._send(HTTPStatus.OK, {"ok": True, "job": job})
            return self._error(HTTPStatus.NOT_FOUND, ErrorCode.NOT_FOUND,
                               "action muss approve|deny|cancel sein")

        def _panic(self, body: Dict[str, Any]) -> Dict[str, Any]:
            actions: Dict[str, Any] = {}
            actions["jobs_cancelled"] = ctx.jobs.cancel_all(actor="panic")
            actions["trust_revoked"] = True
            ctx.jobs.policy.revoke_trust()
            if body.get("stop_tunnel", True) and ctx.tunnel:
                try:
                    ctx.tunnel.stop()
                    actions["tunnel"] = "stopped"
                except Exception as exc:  # noqa: BLE001
                    actions["tunnel"] = f"error: {security.redact_text(str(exc), 120)}"
            if body.get("revoke_keys"):
                revoked = 0
                for rec in ctx.keys.list():
                    if not rec["revoked"]:
                        ctx.keys.revoke(str(rec["key_id"]))
                        revoked += 1
                self._persist_keys()
                actions["keys_revoked"] = revoked
            if ctx.audit:
                ctx.audit.write({"event": "panic", **(security.redact(actions))})
            return actions

        def _update_config(self, body: Dict[str, Any]) -> None:
            editable = {
                "approval_threshold", "approval_timeout_s", "allow_script_mode",
                "script_requires_approval", "snapshot_before_risky", "tunnel_enabled",
                "allow_trust_window", "trust_window_s", "keep_jobs", "preview_max_px",
                "allowed_hosts", "rate_limit_per_min", "addon_port", "http_port",
                "relay_enabled", "relay_repo", "relay_issue", "cloudflared_path",
            }
            changed = {}
            secret_fields = {"relay_token", "relay_secret"}
            for key, value in body.items():
                if key in secret_fields:
                    ctx.store.set_secret(key, str(value or ""))
                    ctx.store.save()
                    changed[key] = "<gesetzt>" if value else "<gelöscht>"
                    continue
                if key not in editable:
                    continue
                setattr(ctx.config, key, value)
                changed[key] = value
            ctx.store.config = ctx.config
            ctx.store.save()
            ctx.jobs.policy.threshold = ctx.config.approval_threshold
            ctx.jobs.policy.allow_trust_window = ctx.config.allow_trust_window
            ctx.jobs.policy.trust_window_s = ctx.config.trust_window_s
            if ctx.audit:
                ctx.audit.write({"event": "config_changed", "changes": security.redact(changed)})
            if ctx.config_changed:
                try:
                    ctx.config_changed(ctx.config)
                except Exception:  # noqa: BLE001
                    pass
            return self._send(HTTPStatus.OK, {"ok": True, "changed": changed,
                                             "config": ctx.config.to_public_dict()})

        def _persist_keys(self) -> None:
            try:
                ctx.store.set_secret("agent_keys", json.dumps(ctx.keys.export()))
                ctx.store.save()
            except OSError as exc:
                if ctx.debug:
                    ctx.debug.add(f"konnte Schlüssel nicht speichern: {exc}", "error")

    return Handler

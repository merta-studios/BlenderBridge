"""Outbound relay: work around environments that cannot reach your PC.

Many AI agent sandboxes can only talk to a small allow-list of hosts (GitHub is
almost always on it).  A Cloudflare tunnel is useless there because the sandbox
cannot open a connection to ``*.trycloudflare.com``.

The relay inverts the direction: the connector *polls* a GitHub issue for
requests and *posts* results back as comments.  No inbound connection, no open
port, no public endpoint.  Trade-offs are documented in ``docs/relay.md``:
  * latency is the poll interval (a few seconds),
  * comment size is limited, so payloads are chunked,
  * the repository must be one you trust (payloads are additionally sealed).
"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import protocol, sealing, security

API_BASE = "https://api.github.com"
MAX_INLINE_ARTIFACT_BYTES = 280_000
USER_AGENT = "BlenderBridge-relay/1.0"


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int = 0, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class GitHubClient:
    """The four API calls the relay needs, with allow-listed hosts only."""

    def __init__(self, token: str, repo: str, issue: int, base_url: str = API_BASE,
                 timeout: float = 20.0):
        self.token = token
        self.repo = repo
        self.issue = int(issue)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request(self, method: str, path: str, data: Optional[Dict[str, Any]] = None,
                 params: Optional[Dict[str, str]] = None) -> Any:
        url = self._url(path)
        if params:
            url += "?" + urllib.parse.urlencode(params)
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", USER_AGENT)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(600).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            raise GitHubError(f"GitHub API {method} {path} -> HTTP {exc.code}",
                              status=exc.code, body=security.redact_text(detail, 400))
        except urllib.error.URLError as exc:
            raise GitHubError(f"GitHub nicht erreichbar: {exc.reason}")

    # -- operations --------------------------------------------------------
    def whoami(self) -> Dict[str, Any]:
        return self._request("GET", "/user")

    def issue_info(self) -> Dict[str, Any]:
        return self._request("GET", f"/repos/{self.repo}/issues/{self.issue}")

    def list_comments(self, since: Optional[str] = None, per_page: int = 50) -> List[Dict[str, Any]]:
        params = {"per_page": str(per_page), "sort": "created", "direction": "asc"}
        if since:
            params["since"] = since
        data = self._request("GET", f"/repos/{self.repo}/issues/{self.issue}/comments",
                            params=params)
        return data if isinstance(data, list) else []

    def create_comment(self, body: str) -> Dict[str, Any]:
        return self._request("POST", f"/repos/{self.repo}/issues/{self.issue}/comments",
                            data={"body": body})

    def check(self) -> Dict[str, Any]:
        """Verify token, repo and issue in one go - used by the UI test button."""
        try:
            me = self.whoami()
            issue = self.issue_info()
            return {
                "ok": True,
                "user": me.get("login", ""),
                "issue_title": issue.get("title", ""),
                "comments": issue.get("comments", 0),
            }
        except GitHubError as exc:
            return {"ok": False, "error": str(exc), "status": exc.status,
                    "hint": self._hint(exc.status)}

    @staticmethod
    def _hint(status: int) -> str:
        if status == 401:
            return "Token ungültig oder abgelaufen."
        if status == 403:
            return "Token hat keine Rechte für Issues in diesem Repository."
        if status == 404:
            return ("Repository/Issue nicht gefunden - oder das Token sieht private "
                    "Repositories nicht.")
        return ""


class RelayMailbox:
    """Polls the issue, executes requests and posts results."""

    MARKER_REQUEST = '"type":"request"'

    def __init__(self, *, client_factory, jobs, config_store, audit=None, debug=None,
                 artifact_root: Optional[Path] = None):
        self._client_factory = client_factory
        self.jobs = jobs
        self.store = config_store
        self.audit = audit
        self.debug = debug
        self.artifact_root = artifact_root
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._seen: set = set()
        self._since: Optional[str] = None
        self._last_error = ""
        self._last_poll = 0.0
        self._processed = 0
        self._posted = 0
        self._pending_parts: Dict[str, List[Dict[str, Any]]] = {}
        self._client: Optional[GitHubClient] = None

    # -- status ------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        cfg = self.store.config
        with self._lock:
            return {
                "state": "running" if (self._thread and self._thread.is_alive()) else "stopped",
                "enabled": bool(cfg.relay_enabled),
                "repo": cfg.relay_repo,
                "issue": cfg.relay_issue,
                "interval_s": cfg.relay_interval_s,
                "last_poll": protocol.now_iso(self._last_poll) if self._last_poll else None,
                "last_error": self._last_error,
                "processed": self._processed,
                "posted": self._posted,
                "secret_present": bool(self.store.get_secret("relay_secret")),
                "token_present": bool(self.store.get_secret("relay_token")),
            }

    # -- lifecycle ---------------------------------------------------------
    def _make_client(self) -> Optional[GitHubClient]:
        cfg = self.store.config
        token = self.store.get_secret("relay_token")
        if not (token and cfg.relay_repo and cfg.relay_issue):
            return None
        return self._client_factory(token, cfg.relay_repo, int(cfg.relay_issue))

    def start(self) -> Dict[str, Any]:
        client = self._make_client()
        if client is None:
            self._last_error = ("Relay ist nicht vollständig konfiguriert "
                                "(Token, Repository und Issue-Nummer fehlen).")
            return self.status()
        with self._lock:
            self._client = client
        if not self.store.get_secret("relay_secret"):
            self.store.set_secret("relay_secret", security.random_token(24))
            self.store.save()
        if self._thread and self._thread.is_alive():
            return self.status()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="bb-relay", daemon=True)
        self._thread.start()
        self._log("Relay gestartet")
        return self.status()

    def stop(self) -> Dict[str, Any]:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3.0)
        self._thread = None
        return self.status()

    def handle_control(self, body: Dict[str, Any]) -> Dict[str, Any]:
        action = str(body.get("action", "status")).lower()
        if action == "start":
            self.store.config.relay_enabled = True
            self.store.save()
            return {"ok": True, "relay": self.start()}
        if action == "stop":
            self.store.config.relay_enabled = False
            self.store.save()
            return {"ok": True, "relay": self.stop()}
        if action == "test":
            client = self._make_client()
            if client is None:
                return {"ok": False, "error": "Konfiguration unvollständig"}
            return client.check()
        if action == "rotate_secret":
            secret = security.random_token(24)
            self.store.set_secret("relay_secret", secret)
            self.store.save()
            return {"ok": True, "secret": secret,
                    "warning": "Neuer geheimer Schlüssel - der Agent braucht ihn erneut."}
        return {"ok": True, "relay": self.status()}

    def secret(self) -> str:
        return self.store.get_secret("relay_secret")

    # -- polling -----------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
                self._last_error = ""
            except GitHubError as exc:
                self._last_error = str(exc)
                self._log(f"GitHub-Fehler: {exc}", "warn")
                if exc.status in (401, 403, 404):
                    time.sleep(min(60.0, max(10.0, self.store.config.relay_interval_s * 5)))
                    continue
            except Exception as exc:  # noqa: BLE001
                self._last_error = security.redact_text(str(exc), 200)
                self._log(f"Relay-Fehler: {exc}", "warn")
            interval = max(1.0, float(self.store.config.relay_interval_s))
            self._stop.wait(interval)

    def _poll_once(self) -> None:
        client = self._client
        if client is None:
            return
        comments = client.list_comments(since=self._since)
        self._last_poll = time.time()
        for comment in comments:
            cid = comment.get("id")
            if cid in self._seen:
                continue
            self._seen.add(cid)
            body = str(comment.get("body") or "").strip()
            created = str(comment.get("created_at") or "")
            if created and (self._since is None or created > self._since):
                self._since = created
            if not body.startswith("{"):
                continue
            try:
                msg = json.loads(body)
            except ValueError:
                continue
            if int(msg.get("bb", 0)) != sealing.SEAL_VERSION:
                continue
            # anything that is not one of our own answers is treated as a request
            if msg.get("type") in ("result", "error", "pending_approval"):
                continue
            self._handle_request(client, msg, cid)

    def _handle_request(self, client: GitHubClient, msg: Dict[str, Any], comment_id: Any) -> None:
        message_id = str(msg.get("id", comment_id))
        if "parts" in msg:
            with self._lock:
                bucket = self._pending_parts.setdefault(message_id, [])
                bucket.append(msg)
                if len(bucket) < int(msg["parts"]):
                    return
                pieces = bucket
                self._pending_parts.pop(message_id, None)
        else:
            pieces = [msg]
        secret = self.secret()
        if not secret:
            self._post_error(client, message_id, "relay secret missing")
            return
        try:
            payload = sealing.open_message(pieces, secret)
        except sealing.SealError as exc:
            self._post_error(client, message_id, f"Entschlüsselung fehlgeschlagen: {exc}")
            return

        self._processed += 1
        self._log(f"Relay-Anfrage {message_id}: {payload.get('tool')}")
        if self.audit:
            self.audit.write({"event": "relay_request", "relay_id": message_id,
                              "tool": payload.get("tool")})
        try:
            job = self.jobs.submit(payload, actor="relay")
        except protocol.ProtocolError as exc:
            self._post_error(client, message_id, f"{exc.code}: {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            self._post_error(client, message_id, security.redact_text(str(exc), 200))
            return

        # Wait for a terminal state, then answer. Approval stays in the local UI.
        deadline = time.time() + min(900.0, float(payload.get("timeout_s", 300)) + 120)
        while time.time() < deadline:
            current = self.jobs.get(job["job_id"])
            if protocol.is_terminal(current["status"]):
                self._post_result(client, message_id, current)
                return
            if current["status"] == protocol.JobState.PENDING_APPROVAL:
                # tell the caller we are waiting for the human
                if not current.get("_relay_notified"):
                    self._post_waiting(client, message_id, job["job_id"])
                    with self._lock:
                        try:
                            self.jobs._jobs[job["job_id"]]["_relay_notified"] = True
                        except Exception:  # noqa: BLE001
                            pass
            time.sleep(2.0)
        self._post_error(client, message_id, "Zeitüberschreitung beim Warten auf das Ergebnis")

    # -- posting -----------------------------------------------------------
    def _post(self, client: GitHubClient, payload: Dict[str, Any]) -> None:
        secret = self.secret()
        if not secret:
            return
        for chunk in sealing.split_message(payload, secret):
            client.create_comment(chunk)
            self._posted += 1

    def _post_result(self, client: GitHubClient, message_id: str, job: Dict[str, Any]) -> None:
        artifacts = []
        for art in job.get("artifacts") or []:
            entry = {"name": art.get("name"), "kind": art.get("kind"),
                     "bytes": art.get("bytes")}
            if str(art.get("kind")) == "image" and art.get("url"):
                try:
                    path = self._artifact_path(job["job_id"], art["name"])
                    if path and path.stat().st_size <= MAX_INLINE_ARTIFACT_BYTES:
                        entry["base64"] = base64.b64encode(path.read_bytes()).decode("ascii")
                except OSError:
                    pass
            artifacts.append(entry)
        payload = {
            "type": "result",
            "id": message_id,
            "job_id": job["job_id"],
            "tool": job.get("tool"),
            "status": job.get("status"),
            "result": job.get("result"),
            "error": job.get("error"),
            "warnings": job.get("warnings", []),
            "assumptions": job.get("assumptions", []),
            "artifacts": artifacts,
            "finished_at": job.get("finished_at"),
        }
        try:
            self._post(client, payload)
        except GitHubError as exc:
            self._log(f"Ergebnis konnte nicht gepostet werden: {exc}", "warn")

    def _post_waiting(self, client: GitHubClient, message_id: str, job_id: str) -> None:
        try:
            self._post(client, {"type": "pending_approval", "id": message_id, "job_id": job_id,
                                "hint": "Die Aktion wartet im Connector-Fenster auf Freigabe."})
        except GitHubError:
            pass

    def _post_error(self, client: GitHubClient, message_id: str, message: str) -> None:
        try:
            self._post(client, {"type": "error", "id": message_id, "error": message})
        except GitHubError as exc:
            self._log(f"Fehler konnte nicht gepostet werden: {exc}", "warn")

    def _artifact_path(self, job_id: str, name: str) -> Optional[Path]:
        if not self.artifact_root:
            return None
        base = (Path(self.artifact_root) / job_id).resolve()
        target = (base / Path(name).name).resolve()
        return target if base == target.parent and target.is_file() else None

    def _log(self, message: str, level: str = "info") -> None:
        if self.debug:
            self.debug.add("relay: " + message, level)


def make_default_relay(*, jobs, config_store, audit=None, debug=None,
                       artifact_root: Optional[Path] = None) -> RelayMailbox:
    def factory(token: str, repo: str, issue: int) -> GitHubClient:
        return GitHubClient(token=token, repo=repo, issue=issue)

    return RelayMailbox(client_factory=factory, jobs=jobs, config_store=config_store,
                        audit=audit, debug=debug, artifact_root=artifact_root)

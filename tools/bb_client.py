#!/usr/bin/env python3
"""Small CLI client for the BlenderBridge HTTP API.

Why this exists: an agent (or you) can talk to the connector with nothing but
`urllib`.  This script is the reference implementation - copy what you need.

Examples
--------
    # what can I do?
    python tools/bb_client.py tools

    # is Blender connected?
    python tools/bb_client.py status

    # one call, human readable
    python tools/bb_client.py call scene.info

    # model something
    python tools/bb_client.py call object.probe --params '{"object":"Palme"}'
    python tools/bb_client.py call mesh.edit --params '{"object":"Palme",
        "region":"stamm","op":"taper","start_factor":1.0,"end_factor":0.6}'

    # render and download the images
    python tools/bb_client.py call preview.render --params '{"views":["perspective","front"]}' \
        --out ./renders

    # dry run: see what would happen without touching the scene
    python tools/bb_client.py call mesh.edit --params '{...}' --dry-run

The agent key is read from --key, $BLENDERBRIDGE_KEY, or the file
``agent_key.txt`` next to this script (or in the config directory).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

TERMINAL = {"succeeded", "failed", "denied", "cancelled", "timed_out"}


class BridgeError(RuntimeError):
    def __init__(self, code: str, message: str, details: Any = None):
        self.code = code
        self.message = message
        self.details = details
        super().__init__(f"{code}: {message}")


class BridgeClient:
    def __init__(self, base: str = "http://127.0.0.1:8765", key: str = "",
                 timeout: float = 30.0, verbose: bool = False):
        self.base = base.rstrip("/")
        self.key = key
        self.timeout = timeout
        self.verbose = verbose

    # -- plumbing ----------------------------------------------------------
    def _headers(self, body: bool = False) -> Dict[str, str]:
        h = {"X-BB-Client": "1", "Accept": "application/json"}
        if self.key:
            h["Authorization"] = f"Bearer {self.key}"
        if body:
            h["Content-Type"] = "application/json"
        return h

    def request(self, method: str, path: str, payload: Optional[Any] = None,
                timeout: Optional[float] = None) -> Dict[str, Any]:
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=self._headers(body=data is not None))
        if self.verbose:
            print(f"-> {method} {path}", file=sys.stderr)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                err = json.loads(raw)["error"]
            except (ValueError, KeyError):
                raise BridgeError("http_error", f"HTTP {exc.code}: {raw[:300]!r}") from None
            raise BridgeError(err.get("code", "error"), err.get("message", ""),
                              err.get("details")) from None
        except urllib.error.URLError as exc:
            raise BridgeError(
                "unreachable",
                f"Connector nicht erreichbar unter {self.base} ({exc.reason}). "
                f"Läuft BlenderBridge.exe?") from None
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise BridgeError("bad_response", f"keine gültige JSON-Antwort: {raw[:200]!r}") from None

    # -- API ---------------------------------------------------------------
    def health(self) -> Dict[str, Any]:
        return self.request("GET", "/v1/health")

    def status(self) -> Dict[str, Any]:
        return self.request("GET", "/v1/status")

    def tools(self) -> Dict[str, Any]:
        return self.request("GET", "/v1/tools")

    def submit(self, tool: str, params: Optional[Dict[str, Any]] = None,
               *, dry_run: bool = False, note: str = "",
               request_id: str = "", timeout_s: Optional[float] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"tool": tool, "params": params or {}}
        if dry_run:
            body["dry_run"] = True
        if note:
            body["note"] = note
        if request_id:
            body["request_id"] = request_id
        if timeout_s:
            body["timeout_s"] = timeout_s
        return self.request("POST", "/v1/jobs", body)["job"]

    @staticmethod
    def _jid(job: Dict[str, Any]) -> str:
        """The API calls it ``job_id``; tolerate ``id`` for older builds."""
        return str(job.get("job_id") or job.get("id"))

    def job(self, job_id: str, wait: float = 0.0) -> Dict[str, Any]:
        path = f"/v1/jobs/{urllib.parse.quote(job_id)}"
        if wait:
            path += f"?wait={int(wait)}"
        return self.request("GET", path, timeout=(wait + 30) if wait else None)["job"]

    def wait(self, job_id: str, timeout: float = 300.0) -> Dict[str, Any]:
        deadline = time.time() + timeout
        delay = 0.2
        while True:
            job = self.job(job_id, wait=min(30.0, max(1.0, deadline - time.time())))
            if job["status"] in TERMINAL:
                return job
            if time.time() > deadline:
                raise BridgeError("timeout", f"Auftrag {job_id} wurde nicht fertig "
                                             f"(Status: {job['status']})")
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)

    def call(self, tool: str, params: Optional[Dict[str, Any]] = None, *,
             timeout: float = 300.0, dry_run: bool = False,
             note: str = "") -> Dict[str, Any]:
        job = self.submit(tool, params, dry_run=dry_run, note=note)
        if self.verbose:
            print(f"   job {self._jid(job)} status={job['status']}", file=sys.stderr)
        job = self.wait(self._jid(job), timeout=timeout)
        if job["status"] != "succeeded":
            err = job.get("error") or {}
            raise BridgeError(err.get("code", job["status"]),
                              err.get("message", f"Auftrag {job['status']}"),
                              err.get("details"))
        return job.get("result", {})

    def download(self, job: Dict[str, Any], out_dir: Path) -> list:
        out_dir.mkdir(parents=True, exist_ok=True)
        saved = []
        for art in job.get("artifacts", []):
            name = art["name"]
            url = art.get("url") or f"/v1/jobs/{self._jid(job)}/artifacts/{name}"
            req = urllib.request.Request(self.base + url, headers=self._headers())
            with urllib.request.urlopen(req, timeout=60) as resp:
                blob = resp.read()
            target = out_dir / Path(name).name
            target.write_bytes(blob)
            saved.append(target)
        return saved


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_key(args) -> str:
    if args.key:
        return args.key
    if os.environ.get("BLENDERBRIDGE_KEY"):
        return os.environ["BLENDERBRIDGE_KEY"]
    candidates = [Path("agent_key.txt"), Path(__file__).resolve().parent / "agent_key.txt"]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "BlenderBridge" / "agent_key.txt")
    for path in candidates:
        try:
            if path.is_file():
                value = path.read_text(encoding="utf-8").strip()
                if value:
                    return value
        except OSError:
            pass
    return ""


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="BlenderBridge CLI-Client")
    p.add_argument("command", choices=["health", "status", "tools", "call", "jobs"],
                   help="was tun")
    p.add_argument("args", nargs="*", help="bei 'call': Werkzeugname")
    p.add_argument("--base", default=os.environ.get("BLENDERBRIDGE_URL", "http://127.0.0.1:8765"))
    p.add_argument("--key", default="", help="Agent-Schlüssel (bbk_…)")
    p.add_argument("--params", default="{}", help="JSON-Parameter für 'call'")
    p.add_argument("--dry-run", action="store_true", help="nur simulieren")
    p.add_argument("--note", default="", help="Notiz für den Freigabedialog")
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument("--out", default="", help="Ordner für Artefakte (PNG/GLB)")
    p.add_argument("--json", action="store_true", help="Rohantwort ausgeben")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    client = BridgeClient(args.base, _load_key(args), verbose=args.verbose)
    try:
        if args.command == "health":
            data = client.health()
        elif args.command == "status":
            data = client.status()
        elif args.command == "tools":
            data = client.tools()
            if not args.json:
                groups: Dict[str, list] = {}
                for tool in data.get("tools", []):
                    groups.setdefault(tool["name"].split(".", 1)[0], []).append(tool)
                print(f"{data.get('count', 0)} Werkzeuge, Protokoll {data.get('protocol', '?')}")
                for ns, items in sorted(groups.items()):
                    print(f"\n[{ns}]")
                    for tool in items:
                        print(f"  {tool['name']:<28} {tool['risk']:<9} {tool['summary']}")
                return 0
        elif args.command == "jobs":
            data = client.request("GET", "/v1/jobs?limit=20")
            if not args.json:
                for job in data.get("jobs", []):
                    jid = job.get("job_id") or job.get("id", "?")
                    print(f"{job['created_at']}  {job['status']:<16} {job['tool']:<24} {jid}")
                return 0
        else:  # call
            if not args.args:
                p.error("'call' braucht einen Werkzeugnamen, z.B. scene.info")
            params = json.loads(args.params)
            job = client.submit(args.args[0], params, dry_run=args.dry_run,
                                note=args.note)
            jid = BridgeClient._jid(job)
            print(f"Auftrag {jid} – Status {job['status']}", file=sys.stderr)
            if job["status"] == "pending_approval":
                print("Warte auf deine Freigabe im Browser …", file=sys.stderr)
            job = client.wait(jid, timeout=args.timeout)
            if args.out and job.get("artifacts"):
                saved = client.download(job, Path(args.out))
                print(f"{len(saved)} Datei(en) gespeichert in {Path(args.out).resolve()}",
                      file=sys.stderr)
            data = job.get("result") if job["status"] == "succeeded" else job
            if job["status"] != "succeeded":
                err = job.get("error") or {}
                print(f"FEHLER [{err.get('code', job['status'])}] {err.get('message', '')}",
                      file=sys.stderr)
                if err.get("details") and not args.json:
                    print(json.dumps(err["details"], ensure_ascii=False, indent=2),
                          file=sys.stderr)
                return 1
            if args.json:
                print(json.dumps(job, ensure_ascii=False, indent=2))
                return 0
    except BridgeError as exc:
        print(f"FEHLER [{exc.code}] {exc.message}", file=sys.stderr)
        if exc.details:
            print(json.dumps(exc.details, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

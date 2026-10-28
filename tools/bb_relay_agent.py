#!/usr/bin/env python3
"""Agent-side client for the BlenderBridge relay (GitHub issue as a mailbox).

Use this when the agent cannot reach the user's PC directly - for example inside
a sandbox that only allows `github.com`.  Nothing has to be opened on the user's
side: the connector polls the issue itself and answers there.

    # the user configures repo/issue/secret in the BlenderBridge UI
    export BB_RELAY_SECRET='the-same-secret'
    export GITHUB_TOKEN='github_pat_...'      # issues: read+write on that repo

    python bb_relay_agent.py --repo me/blender-bridge --issue 7 call scene.info
    python bb_relay_agent.py --repo me/blender-bridge --issue 7 call object.probe \
        --params '{"object":"Palme"}' --out ./results

    # render and keep the images
    python bb_relay_agent.py ... call preview.render --params '{"views":["perspective"]}' --out ./renders

Exit codes: 0 ok, 1 tool error, 2 network/configuration problem, 3 timeout.

The sealing code is a **copy** of ``blenderbridge/sealing.py`` on purpose: this
file must work on a machine that has neither the repository nor the connector.
``tests/test_relay_agent.py`` checks that both implementations interoperate.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

SEAL_VERSION = 1
NONCE_BYTES = 16
TAG_BYTES = 32
MAX_CHUNK_CHARS = 48_000
API = "https://api.github.com"


# ---------------------------------------------------------------------------
# Sealing (must stay byte-compatible with blenderbridge/sealing.py)
# ---------------------------------------------------------------------------


class SealError(ValueError):
    pass


def _keys(secret: str):
    raw = secret.encode("utf-8")
    if len(raw) < 16:
        raise SealError("shared secret must be at least 16 characters")
    enc = hashlib.sha256(b"bb-relay-enc|" + raw).digest()
    mac = hashlib.sha256(b"bb-relay-mac|" + raw).digest()
    return enc, mac


def _xor(data: bytes, key: bytes, nonce: bytes) -> bytes:
    out = bytearray(len(data))
    counter = 0
    pos = 0
    while pos < len(data):
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        take = min(len(block), len(data) - pos)
        for i in range(take):
            out[pos + i] = data[pos + i] ^ block[i]
        pos += take
        counter += 1
    return bytes(out)


def seal(data: bytes, secret: str) -> str:
    enc_key, mac_key = _keys(secret)
    nonce = os.urandom(NONCE_BYTES)
    cipher = _xor(data, enc_key, nonce)
    tag = hmac.new(mac_key, nonce + cipher, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + cipher + tag).decode("ascii")


def open_sealed(blob: str, secret: str) -> bytes:
    enc_key, mac_key = _keys(secret)
    raw = base64.urlsafe_b64decode(blob.encode("ascii"))
    if len(raw) < NONCE_BYTES + TAG_BYTES:
        raise SealError("payload is too short")
    nonce, rest = raw[:NONCE_BYTES], raw[NONCE_BYTES:]
    cipher, tag = rest[:-TAG_BYTES], rest[-TAG_BYTES:]
    if not hmac.compare_digest(tag, hmac.new(mac_key, nonce + cipher, hashlib.sha256).digest()):
        raise SealError("authentication failed - wrong secret or tampered payload")
    return _xor(cipher, enc_key, nonce)


def split_message(payload: Dict[str, Any], secret: str, max_chars: int = MAX_CHUNK_CHARS) -> List[str]:
    message_id = payload.get("id") or os.urandom(8).hex()
    body = {"bb": SEAL_VERSION, "id": message_id, "type": payload.get("type", "request"),
            "sealed": seal(json.dumps(payload, separators=(",", ":")).encode("utf-8"), secret)}
    whole = json.dumps(body, separators=(",", ":"))
    if len(whole) <= max_chars:
        return [whole]
    blob = body["sealed"]
    step = max_chars - 400
    pieces = [blob[i:i + step] for i in range(0, len(blob), step)]
    return [json.dumps({"bb": SEAL_VERSION, "id": message_id, "type": body["type"],
                        "part": i, "parts": len(pieces), "sealed": piece},
                       separators=(",", ":")) for i, piece in enumerate(pieces)]


def join_parts(pieces: List[Dict[str, Any]]) -> Dict[str, Any]:
    first = pieces[0]
    if "parts" not in first:
        return first
    parts = int(first["parts"])
    by_index = {int(p.get("part", 0)): p for p in pieces}
    missing = [i for i in range(parts) if i not in by_index]
    if missing:
        raise SealError(f"missing chunks: {missing}")
    merged = dict(first)
    merged.pop("part", None)
    merged.pop("parts", None)
    merged["sealed"] = "".join(str(by_index[i]["sealed"]) for i in range(parts))
    return merged


def open_message(pieces: List[Dict[str, Any]], secret: str) -> Dict[str, Any]:
    msg = join_parts(pieces)
    if int(msg.get("bb", 0)) != SEAL_VERSION:
        raise SealError("unsupported relay message version")
    return json.loads(open_sealed(str(msg["sealed"]), secret).decode("utf-8"))


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


class GitHub:
    def __init__(self, token: str, repo: str, issue: int, base: str = API):
        self.token = token
        self.repo = repo
        self.issue = issue
        self.base = base.rstrip("/")

    def _request(self, method: str, path: str, data: Optional[dict] = None):
        url = f"{self.base}/repos/{self.repo}{path}"
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "BlenderBridge-relay-agent",
            **({"Content-Type": "application/json"} if body else {}),
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace")
            hint = ""
            if exc.code in (401, 403):
                hint = ("\nTipp: Das Token braucht 'Issues: Read and write' für genau "
                        "dieses Repository.")
            elif exc.code == 404:
                hint = "\nTipp: Repo/Issue falsch oder das Token sieht das Repo nicht."
            raise SystemExit(f"GitHub {exc.code}: {detail}{hint}") from None
        except urllib.error.URLError as exc:
            raise SystemExit(f"GitHub nicht erreichbar: {exc.reason}") from None

    def comments(self, since: Optional[str] = None) -> List[dict]:
        path = f"/issues/{self.issue}/comments?per_page=100"
        if since:
            path += f"&since={since}"
        return self._request("GET", path) or []

    def comment(self, body: str) -> dict:
        return self._request("POST", f"/issues/{self.issue}/comments", {"body": body})


def _messages(comments: List[dict]) -> List[dict]:
    out = []
    for c in comments:
        body = str(c.get("body") or "").strip()
        if not body.startswith("{"):
            continue
        try:
            msg = json.loads(body)
        except ValueError:
            continue
        if int(msg.get("bb", 0)) != SEAL_VERSION:
            continue
        msg["_comment_id"] = c.get("id")
        out.append(msg)
    return out


def call(args) -> int:
    gh = GitHub(args.token, args.repo, args.issue, args.base)
    secret = args.secret
    params = json.loads(args.params or "{}")
    message_id = f"agent-{os.urandom(4).hex()}"
    payload = {
        "type": "request",
        "id": message_id,
        "tool": args.tool,
        "params": params,
        "note": args.note or "Anfrage über den Relay",
        "timeout_s": args.timeout,
    }
    if args.dry_run:
        payload["dry_run"] = True
    chunks = split_message(payload, secret)
    start = time.gmtime()
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.strptime(
        time.strftime("%Y-%m-%dT%H:%M:%S", start), "%Y-%m-%dT%H:%M:%S"))
    for chunk in chunks:
        gh.comment(chunk)
    print(f"Anfrage {message_id} gesendet ({len(chunks)} Kommentar(e)); "
          f"warte auf Antwort …", file=sys.stderr)

    deadline = time.time() + args.wait
    seen: Dict[str, List[dict]] = {}
    last_poll = 0.0
    while time.time() < deadline:
        if time.time() - last_poll >= 3.0:
            last_poll = time.time()
            for msg in _messages(gh.comments(since=since)):
                mid = str(msg.get("id"))
                if mid != message_id:
                    continue
                if msg.get("type") not in ("result", "error", "pending_approval"):
                    continue
                bucket = seen.setdefault(mid, [])
                bucket.append(msg)
                expected = int(msg.get("parts", 1) or 1)
                if len({int(p.get("part", 0)) for p in bucket}) < expected:
                    continue
                try:
                    answer = open_message(bucket, secret)
                except SealError as exc:
                    print(f"Antwort konnte nicht entschlüsselt werden: {exc}", file=sys.stderr)
                    return 2
                return _finish(answer, args)
        time.sleep(1.0)
    print(f"Keine Antwort innerhalb von {args.wait:.0f}s. Läuft der Connector mit "
          f"aktivem Relay?", file=sys.stderr)
    return 3


def _finish(answer: Dict[str, Any], args) -> int:
    kind = answer.get("type")
    if kind == "pending_approval":
        print("Der Auftrag wartet auf die Freigabe des Nutzers im Connector-Fenster.",
              file=sys.stderr)
    if kind == "error":
        print(f"Fehler: {answer.get('error')}", file=sys.stderr)
        return 1
    artifacts = answer.get("artifacts") or []
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        for art in artifacts:
            blob = art.get("base64")
            if not blob:
                continue
            target = out_dir / Path(str(art.get("name"))).name
            target.write_bytes(base64.b64decode(blob))
            print(f"gespeichert: {target}", file=sys.stderr)
    status = answer.get("status")
    if status and status != "succeeded":
        print(f"Status: {status}", file=sys.stderr)
        print(json.dumps(answer, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(answer.get("result", answer), ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="BlenderBridge Relay-Client (Agentenseite)")
    p.add_argument("--repo", default=os.environ.get("BB_RELAY_REPO", ""),
                   help="owner/name")
    p.add_argument("--issue", type=int, default=int(os.environ.get("BB_RELAY_ISSUE", "0") or 0))
    p.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", "") or
                   os.environ.get("GH_TOKEN", ""), help="GitHub-Token (Issues: read/write)")
    p.add_argument("--secret", default=os.environ.get("BB_RELAY_SECRET", ""),
                   help="gemeinsames Geheimnis aus der BlenderBridge-Oberfläche")
    p.add_argument("--base", default=os.environ.get("GITHUB_API", API))
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("call", help="Werkzeug aufrufen und auf das Ergebnis warten")
    c.add_argument("tool")
    c.add_argument("--params", default="{}")
    c.add_argument("--note", default="")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--out", default="", help="Ordner für Bilder/Dateien")
    c.add_argument("--wait", type=float, default=180.0)
    c.add_argument("--timeout", type=float, default=120.0)
    args = p.parse_args(argv)

    missing = [name for name, value in
               (("--repo", args.repo), ("--issue", args.issue), ("--token", args.token),
                ("--secret", args.secret)) if not value]
    if missing:
        p.error("fehlt: " + ", ".join(missing))
    if args.command == "call":
        return call(args)
    p.error("unbekannter Befehl")
    return 2


if __name__ == "__main__":
    sys.exit(main())

"""Authentication, authorisation, redaction and secret storage.

Design rules
------------
* A hard-to-guess link is **not** authentication.  Every API call carries a
  token, including calls that arrive through a tunnel.
* Agent keys are stored hashed; the plaintext is shown to the user once.
* Secrets at rest use Windows DPAPI (per-user, per-machine).  Elsewhere we fall
  back to 0600 file permissions and say so loudly.
* Log lines are redacted before they are written, not after.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

AGENT_KEY_PREFIX = "bbk_"
KEY_HASH_ITERATIONS = 120_000

# ---------------------------------------------------------------------------
# Tokens and keys
# ---------------------------------------------------------------------------


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def constant_time_equals(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def hash_agent_key(key: str, salt: str) -> str:
    """PBKDF2-HMAC-SHA256; the salt is stored next to the hash."""
    dk = hashlib.pbkdf2_hmac("sha256", key.encode("utf-8"), salt.encode("utf-8"),
                             KEY_HASH_ITERATIONS)
    return base64.b64encode(dk).decode("ascii")


def new_agent_key() -> Tuple[str, str, str]:
    """Return ``(plaintext_key, salt, hash)`` - show ``key`` to the user once."""
    key = AGENT_KEY_PREFIX + secrets.token_urlsafe(32)
    salt = secrets.token_urlsafe(16)
    return key, salt, hash_agent_key(key, salt)


@dataclass
class AgentKey:
    key_id: str
    label: str
    salt: str
    key_hash: str
    created_at: float
    last_used_at: float = 0.0
    uses: int = 0
    revoked: bool = False

    def to_dict(self) -> Dict[str, object]:
        return {
            "key_id": self.key_id,
            "label": self.label,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "uses": self.uses,
            "revoked": self.revoked,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, object]) -> "AgentKey":
        return cls(
            key_id=str(d["key_id"]),
            label=str(d.get("label", "")),
            salt=str(d["salt"]),
            key_hash=str(d["key_hash"]),
            created_at=float(d.get("created_at", 0.0)),
            last_used_at=float(d.get("last_used_at", 0.0)),
            uses=int(d.get("uses", 0)),
            revoked=bool(d.get("revoked", False)),
        )


class KeyStore:
    """In-memory view of agent keys; persistence is handled by ConfigStore."""

    def __init__(self, keys: Optional[Iterable[Dict[str, object]]] = None):
        self._keys: Dict[str, AgentKey] = {}
        for d in keys or []:
            k = AgentKey.from_dict(d)
            self._keys[k.key_id] = k
        self._lock = threading.Lock()

    def create(self, label: str = "agent") -> Tuple[str, AgentKey]:
        key, salt, digest = new_agent_key()
        rec = AgentKey(key_id="key_" + secrets.token_hex(4), label=label,
                       salt=salt, key_hash=digest, created_at=time.time())
        with self._lock:
            self._keys[rec.key_id] = rec
        return key, rec

    def verify(self, presented: str) -> Optional[AgentKey]:
        if not presented or not presented.startswith(AGENT_KEY_PREFIX):
            return None
        with self._lock:
            candidates = [k for k in self._keys.values() if not k.revoked]
        for rec in candidates:
            if hmac.compare_digest(hash_agent_key(presented, rec.salt), rec.key_hash):
                with self._lock:
                    rec.last_used_at = time.time()
                    rec.uses += 1
                return rec
        return None

    def revoke(self, key_id: str) -> bool:
        with self._lock:
            rec = self._keys.get(key_id)
            if rec:
                rec.revoked = True
                return True
        return False

    def list(self) -> List[Dict[str, object]]:
        with self._lock:
            return [k.to_dict() for k in self._keys.values()]

    def export(self) -> List[Dict[str, object]]:
        with self._lock:
            return [
                {
                    "key_id": k.key_id,
                    "label": k.label,
                    "salt": k.salt,
                    "key_hash": k.key_hash,
                    "created_at": k.created_at,
                    "last_used_at": k.last_used_at,
                    "uses": k.uses,
                    "revoked": k.revoked,
                }
                for k in self._keys.values()
            ]


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class RateLimiter:
    """Simple token bucket, per key id."""

    def __init__(self, per_minute: int = 120, burst: Optional[int] = None):
        self.per_minute = max(1, per_minute)
        self.capacity = burst if burst is not None else max(1, self.per_minute // 4 + 1)
        self._state: Dict[str, Tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._state.get(key, (float(self.capacity), now))
            refill = (now - last) * (self.per_minute / 60.0)
            tokens = min(self.capacity, tokens + refill)
            if tokens >= cost:
                self._state[key] = (tokens - cost, now)
                return True
            self._state[key] = (tokens, now)
            return False


# ---------------------------------------------------------------------------
# Risk policy
# ---------------------------------------------------------------------------

RISK_ORDER = {"read": 0, "low": 1, "moderate": 2, "high": 3, "critical": 4, "never": 99}

APPROVAL_TEXT = {
    "read": "Liest nur Daten, verändert nichts.",
    "low": "Fügt etwas Neues hinzu, überschreibt nichts Bestehendes.",
    "moderate": "Verändert bestehende Objekte oder Geometrie.",
    "high": "Kann Daten löschen, Topologie umbauen oder Dateien schreiben.",
    "critical": "Führt beliebigen Python-Code in Blender aus - voller Zugriff auf deinen PC.",
}


def risk_rank(level: str) -> int:
    return RISK_ORDER.get(str(level).lower(), RISK_ORDER["high"])


class ApprovalPolicy:
    """Decides whether a tool call needs an explicit yes from the user."""

    def __init__(self, threshold: str = "moderate", trust_window_s: int = 0,
                 allow_trust_window: bool = True):
        self.threshold = str(threshold).lower()
        self.trust_window_s = int(trust_window_s)
        self.allow_trust_window = bool(allow_trust_window)
        self._trust_until = 0.0
        self._lock = threading.Lock()

    def requires_approval(self, risk: str) -> bool:
        if self.threshold not in RISK_ORDER:
            self.threshold = "moderate"
        if self.allow_trust_window:
            with self._lock:
                if time.time() < self._trust_until:
                    return False
        return risk_rank(risk) >= risk_rank(self.threshold)

    def grant_trust(self, seconds: Optional[int] = None) -> float:
        if not self.allow_trust_window:
            return 0.0
        secs = self.trust_window_s if seconds is None else max(0, min(int(seconds), 3600))
        with self._lock:
            self._trust_until = time.time() + secs
        return self._trust_until

    def revoke_trust(self) -> None:
        with self._lock:
            self._trust_until = 0.0

    def trust_remaining(self) -> int:
        with self._lock:
            return max(0, int(self._trust_until - time.time()))

    def describe(self, risk: str) -> str:
        base = APPROVAL_TEXT.get(str(risk).lower(), "")
        return f"Risiko '{risk}': {base}"


# ---------------------------------------------------------------------------
# Secret storage: Windows DPAPI with a portable fallback
# ---------------------------------------------------------------------------

_BLOB_MAGIC_DPAPI = b"BBSEC1D"
_BLOB_MAGIC_PLAIN = b"BBSEC1P"


def _dpapi_available() -> bool:
    return sys.platform == "win32"


def _dpapi(data: bytes, protect: bool) -> bytes:
    """CryptProtectData / CryptUnprotectData via ctypes (no extra dependency)."""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    def to_blob(b: bytes) -> DATA_BLOB:
        buf = ctypes.create_string_buffer(b, len(b))
        return DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    in_blob = to_blob(data)
    out_blob = DATA_BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if protect:
        ok = fn(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob))
    else:
        ok = fn(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob))
    if not ok:
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def seal_secret_blob(data: bytes) -> Tuple[bytes, bool]:
    """Protect ``data`` for the current user. Returns ``(blob, encrypted)``."""
    if _dpapi_available():
        try:
            return _BLOB_MAGIC_DPAPI + _dpapi(data, protect=True), True
        except OSError:
            pass
    return _BLOB_MAGIC_PLAIN + base64.b64encode(data), False


def open_secret_blob(blob: bytes) -> Tuple[bytes, bool]:
    if blob.startswith(_BLOB_MAGIC_DPAPI):
        if not _dpapi_available():
            raise ValueError("secrets were encrypted with DPAPI on Windows and cannot be read here")
        return _dpapi(blob[len(_BLOB_MAGIC_DPAPI):], protect=False), True
    if blob.startswith(_BLOB_MAGIC_PLAIN):
        return base64.b64decode(blob[len(_BLOB_MAGIC_PLAIN):]), False
    raise ValueError("unrecognised secrets file")


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_SENSITIVE_KEYS = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|authorization|cookie|bearer|"
    r"private[_-]?key|session|credential)",
    re.IGNORECASE,
)
_TOKEN_PATTERNS = [
    re.compile(r"bbk_[A-Za-z0-9_\-]{10,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{10,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"),  # JWT
    re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com", re.IGNORECASE),
]
_CODE_KEYS = {"code", "script", "source", "python"}


def redact(value, key: str = "", depth: int = 0):
    """Recursively redact secrets from a JSON-like structure."""
    if depth > 12:
        return "<deeply nested>"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if _SENSITIVE_KEYS.search(str(k)) and not isinstance(v, (dict, list)):
                out[k] = "<redacted>"
            elif str(k).lower() in _CODE_KEYS and isinstance(v, str):
                out[k] = f"<code {len(v)} chars, sha256={hashlib.sha256(v.encode()).hexdigest()[:12]}>"
            else:
                out[k] = redact(v, str(k), depth + 1)
        return out
    if isinstance(value, list):
        return [redact(v, key, depth + 1) for v in value[:200]]
    if isinstance(value, str):
        text = value
        for pat in _TOKEN_PATTERNS:
            text = pat.sub("<redacted>", text)
        return text if len(text) <= 2000 else text[:1997] + "..."
    return value


def redact_text(text: str, limit: int = 4000) -> str:
    out = text
    for pat in _TOKEN_PATTERNS:
        out = pat.sub("<redacted>", out)
    return out if len(out) <= limit else out[: limit - 3] + "..."


def summary_of(params: Dict[str, object]) -> str:
    """Short, human readable one-liner about what a call will touch."""
    keys = ("object", "objects", "name", "target", "path", "tool")
    parts = []
    for k in keys:
        if k in params:
            v = params[k]
            if isinstance(v, (list, tuple)):
                v = ", ".join(str(x) for x in list(v)[:4])
            parts.append(f"{k}={v}")
    return "; ".join(parts)[:300]

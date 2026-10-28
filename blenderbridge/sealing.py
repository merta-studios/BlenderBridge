"""Sealing for the relay transport.

The relay writes job requests and results into a GitHub issue.  GitHub is
already reached over TLS, but issue comments live in a database and may be
visible to the repository owner or to anyone if the repository is public.  So
payloads are sealed with a shared secret before they ever leave the machine:

    sealed = base64( nonce || ciphertext || tag )
    keystream_i = HMAC-SHA256(key, nonce || counter_i)
    tag = HMAC-SHA256(mac_key, nonce || ciphertext)

This is encrypt-then-MAC with a standard-library keystream construction.  It is
*not* a replacement for an audited AEAD: it is defence in depth on top of TLS,
and the connector says so in the UI.  Anyone with the secret can decrypt.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any, Dict, List, Tuple

NONCE_BYTES = 16
TAG_BYTES = 32
MAX_CHUNK_CHARS = 48_000     # keep one GitHub comment well below the 65 536 limit
SEAL_VERSION = 1


class SealError(ValueError):
    pass


def _keys(secret: str) -> Tuple[bytes, bytes]:
    raw = secret.encode("utf-8")
    if len(raw) < 16:
        raise SealError("shared secret must be at least 16 characters")
    enc = hashlib.sha256(b"bb-relay-enc|" + raw).digest()
    mac = hashlib.sha256(b"bb-relay-mac|" + raw).digest()
    return enc, mac


def _keystream_xor(data: bytes, key: bytes, nonce: bytes) -> bytes:
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
    cipher = _keystream_xor(data, enc_key, nonce)
    tag = hmac.new(mac_key, nonce + cipher, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + cipher + tag).decode("ascii")


def open_sealed(blob: str, secret: str) -> bytes:
    enc_key, mac_key = _keys(secret)
    try:
        raw = base64.urlsafe_b64decode(blob.encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise SealError(f"payload is not valid base64: {exc}")
    if len(raw) < NONCE_BYTES + TAG_BYTES:
        raise SealError("payload is too short")
    nonce, rest = raw[:NONCE_BYTES], raw[NONCE_BYTES:]
    cipher, tag = rest[:-TAG_BYTES], rest[-TAG_BYTES:]
    expected = hmac.new(mac_key, nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise SealError("authentication failed - wrong secret or tampered payload")
    return _keystream_xor(cipher, enc_key, nonce)


def seal_json(obj: Any, secret: str) -> str:
    return seal(json.dumps(obj, separators=(",", ":")).encode("utf-8"), secret)


def open_sealed_json(blob: str, secret: str) -> Any:
    return json.loads(open_sealed(blob, secret).decode("utf-8"))


# ---------------------------------------------------------------------------
# Chunking: one logical message may need several issue comments
# ---------------------------------------------------------------------------


def split_message(payload: Dict[str, Any], secret: str, max_chars: int = MAX_CHUNK_CHARS) -> List[str]:
    """Seal a payload and split it into comment-sized pieces.

    Each piece is a self-contained JSON document with a ``part``/``parts`` pair,
    so a lost or reordered comment is detectable instead of silently corrupting
    the result.
    """
    message_id = payload.get("id") or os.urandom(8).hex()
    body = {"bb": SEAL_VERSION, "id": message_id, "type": payload.get("type", "request"),
            "sealed": seal_json(payload, secret)}
    whole = json.dumps(body, separators=(",", ":"))
    if len(whole) <= max_chars:
        return [whole]
    blob = body["sealed"]
    step = max_chars - 400
    pieces = [blob[i:i + step] for i in range(0, len(blob), step)]
    return [
        json.dumps({"bb": SEAL_VERSION, "id": message_id, "type": body["type"],
                    "part": i, "parts": len(pieces), "sealed": piece},
                   separators=(",", ":"))
        for i, piece in enumerate(pieces)
    ]


def join_parts(pieces: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Reassemble chunks produced by :func:`split_message`."""
    if not pieces:
        raise SealError("no pieces")
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
    return open_sealed_json(str(msg["sealed"]), secret)

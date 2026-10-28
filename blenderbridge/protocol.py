"""Wire protocol shared by the Windows connector, the Blender add-on and agents.

This module is *pure Python* (standard library only) so that it can be copied
verbatim into the Blender add-on (see ``tools/sync_shared.py``) and into the
PyInstaller bundle without pulling in any third party dependency.

Two protocols live here:

1. ``Connector API`` - HTTP/JSON between an AI agent (or the user) and the
   connector.  A request is a *job*; a job is accepted, may need approval, is
   forwarded to Blender and finally carries a result.
2. ``Add-on RPC`` - newline delimited JSON over a loopback TCP socket between
   the connector and the Blender add-on.  Every line is one envelope.

Nothing in this module touches the network, Blender or the filesystem.
"""

from __future__ import annotations

import re
import secrets
import string
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional

PROTOCOL_VERSION = "1.0"

# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


class ErrorCode(str):
    """Namespace of stable, machine readable error codes.

    The values are plain strings on purpose: they travel through JSON without
    any conversion and stay readable in logs and in the UI.
    """

    BAD_REQUEST = "bad_request"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    UNSUPPORTED = "unsupported"

    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_DENIED = "approval_denied"
    APPROVAL_TIMEOUT = "approval_timeout"

    BLENDER_UNAVAILABLE = "blender_unavailable"
    BLENDER_BUSY = "blender_busy"
    BLENDER_ERROR = "blender_error"

    TOOL_UNKNOWN = "tool_unknown"
    PARAM_INVALID = "param_invalid"
    PRECONDITION_FAILED = "precondition_failed"

    OBJECT_NOT_FOUND = "object_not_found"
    MESH_NOT_FOUND = "mesh_not_found"
    REGION_NOT_FOUND = "region_not_found"
    REGION_STALE = "region_stale"
    NO_GEOMETRY = "no_geometry"

    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INTERNAL = "internal"

    TUNNEL_ERROR = "tunnel_error"
    RELAY_ERROR = "relay_error"
    ARTIFACT_MISSING = "artifact_missing"


#: Codes that mean "nothing was changed in Blender".
NON_MUTATING_ERRORS = frozenset(
    {
        ErrorCode.BAD_REQUEST,
        ErrorCode.UNAUTHORIZED,
        ErrorCode.FORBIDDEN,
        ErrorCode.NOT_FOUND,
        ErrorCode.CONFLICT,
        ErrorCode.RATE_LIMITED,
        ErrorCode.APPROVAL_REQUIRED,
        ErrorCode.APPROVAL_DENIED,
        ErrorCode.APPROVAL_TIMEOUT,
        ErrorCode.BLENDER_UNAVAILABLE,
        ErrorCode.TOOL_UNKNOWN,
        ErrorCode.PARAM_INVALID,
        ErrorCode.PRECONDITION_FAILED,
        ErrorCode.OBJECT_NOT_FOUND,
        ErrorCode.MESH_NOT_FOUND,
        ErrorCode.REGION_NOT_FOUND,
        ErrorCode.REGION_STALE,
        ErrorCode.NO_GEOMETRY,
        ErrorCode.TUNNEL_ERROR,
        ErrorCode.RELAY_ERROR,
    }
)


class ProtocolError(Exception):
    """An error that is safe to hand back to the caller.

    Extra keyword arguments become ``details`` so call sites can simply write
    ``ProtocolError(ErrorCode.NOT_FOUND, "…", object=name)``.
    """

    def __init__(self, code: str, message: str,
                 details: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        self.code = str(code)
        self.message = str(message)
        merged: Dict[str, Any] = dict(details or {})
        merged.update(kwargs)
        self.details = merged
        super().__init__(self.message)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            out["details"] = self.details
        return out


def error(code: str, message: str, **details: Any) -> Dict[str, Any]:
    """Build a serialisable error object."""
    out: Dict[str, Any] = {"code": code, "message": message}
    if details:
        out["details"] = details
    return out


# ---------------------------------------------------------------------------
# Job states
# ---------------------------------------------------------------------------


class JobState(str):
    PENDING_APPROVAL = "pending_approval"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"

    #: States from which a job will never move again.
    TERMINAL = None


TERMINAL_STATES = frozenset(
    {
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.DENIED,
        JobState.TIMED_OUT,
        JobState.CANCELLED,
    }
)


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


# ---------------------------------------------------------------------------
# Identifiers and time
# ---------------------------------------------------------------------------

_ID_ALPHABET = string.ascii_lowercase + string.digits
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def new_id(prefix: str = "job", length: int = 16) -> str:
    """Return a random, URL safe identifier such as ``job-3f9c1a...``."""
    body = "".join(secrets.choice(_ID_ALPHABET) for _ in range(length))
    return f"{prefix}-{body}"


def is_valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def now() -> float:
    return time.time()


def now_iso(ts: Optional[float] = None) -> str:
    """UTC timestamp in ISO-8601 with a ``Z`` suffix (sortable, unambiguous)."""
    t = time.gmtime(now() if ts is None else ts)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", t)


# ---------------------------------------------------------------------------
# Job request validation
# ---------------------------------------------------------------------------

TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,3}$")

MAX_PARAM_BYTES = 2 * 1024 * 1024
DEFAULT_JOB_TIMEOUT = 120.0
MAX_JOB_TIMEOUT = 1800.0


def validate_tool_name(tool: Any) -> str:
    if not isinstance(tool, str) or not TOOL_NAME_RE.match(tool):
        raise ProtocolError(
            ErrorCode.PARAM_INVALID,
            "tool must look like 'namespace.action', e.g. 'scene.info'",
            {"tool": _short_repr(tool)},
        )
    return tool


def validate_job_request(payload: Any) -> Dict[str, Any]:
    """Normalise an incoming job request.

    Returns a dict with the keys ``tool``, ``params``, ``timeout_s``,
    ``dry_run``, ``note`` and ``request_id``.  Raises :class:`ProtocolError`
    with ``param_invalid`` when the payload is not usable.
    """
    if not isinstance(payload, Mapping):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "request body must be a JSON object")

    unknown = set(payload) - {
        "tool",
        "params",
        "timeout_s",
        "dry_run",
        "note",
        "request_id",
        "protocol",
    }
    if unknown:
        raise ProtocolError(
            ErrorCode.BAD_REQUEST,
            "unknown top level field(s)",
            {"fields": sorted(unknown)},
        )

    if "protocol" in payload and str(payload["protocol"]) != PROTOCOL_VERSION:
        raise ProtocolError(
            ErrorCode.UNSUPPORTED,
            "unsupported protocol version",
            {"expected": PROTOCOL_VERSION, "got": str(payload["protocol"])},
        )

    tool = validate_tool_name(payload.get("tool"))

    params = payload.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        raise ProtocolError(ErrorCode.PARAM_INVALID, "params must be a JSON object")
    params = dict(params)
    if _json_len(params) > MAX_PARAM_BYTES:
        raise ProtocolError(ErrorCode.PAYLOAD_TOO_LARGE, "params are too large")

    timeout = payload.get("timeout_s", DEFAULT_JOB_TIMEOUT)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        raise ProtocolError(ErrorCode.PARAM_INVALID, "timeout_s must be a number")
    if not (1.0 <= timeout <= MAX_JOB_TIMEOUT):
        raise ProtocolError(
            ErrorCode.PARAM_INVALID,
            f"timeout_s must be between 1 and {int(MAX_JOB_TIMEOUT)}",
        )

    dry_run = payload.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise ProtocolError(ErrorCode.PARAM_INVALID, "dry_run must be a boolean")

    note = payload.get("note", "")
    if not isinstance(note, str) or len(note) > 2000:
        raise ProtocolError(ErrorCode.PARAM_INVALID, "note must be a string <= 2000 chars")

    request_id = payload.get("request_id")
    if request_id is not None and not is_valid_id(request_id):
        raise ProtocolError(ErrorCode.PARAM_INVALID, "request_id is not a valid id")

    return {
        "tool": tool,
        "params": params,
        "timeout_s": timeout,
        "dry_run": dry_run,
        "note": note,
        "request_id": request_id,
    }


def _json_len(obj: Any) -> int:
    import json

    try:
        return len(json.dumps(obj, default=str))
    except (TypeError, ValueError):
        return MAX_PARAM_BYTES + 1


def _short_repr(value: Any, limit: int = 80) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# Job object
# ---------------------------------------------------------------------------


def new_job_record(
    tool: str,
    params: Dict[str, Any],
    *,
    risk: str,
    requires_approval: bool,
    approval_reason: str = "",
    note: str = "",
    request_id: Optional[str] = None,
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create the JSON object that the API reports for a job."""
    ts = now()
    state = JobState.PENDING_APPROVAL if requires_approval else JobState.QUEUED
    return {
        "job_id": job_id or new_id("job"),
        "protocol": PROTOCOL_VERSION,
        "tool": tool,
        "params": params,
        "risk": risk,
        "status": state,
        "requires_approval": requires_approval,
        "approval_reason": approval_reason,
        "note": note,
        "request_id": request_id,
        "created_at": now_iso(ts),
        "updated_at": now_iso(ts),
        "started_at": None,
        "finished_at": None,
        "result": None,
        "error": None,
        "warnings": [],
        "assumptions": [],
        "artifacts": [],
    }


# ---------------------------------------------------------------------------
# Add-on RPC envelope
# ---------------------------------------------------------------------------


def addon_request(
    tool: str,
    params: Mapping[str, Any],
    *,
    token: str,
    request_id: Optional[str] = None,
    timeout_s: float = DEFAULT_JOB_TIMEOUT,
    dry_run: bool = False,
    meta: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build one NDJSON line for the add-on."""
    return {
        "v": PROTOCOL_VERSION,
        "id": request_id or new_id("rpc", 12),
        "tool": tool,
        "params": dict(params),
        "timeout_s": float(timeout_s),
        "dry_run": bool(dry_run),
        "auth": token,
        "meta": dict(meta or {}),
    }


def addon_ok(request_id: str, result: Any, *, warnings: Optional[Iterable[str]] = None,
             assumptions: Optional[Iterable[str]] = None, duration_ms: Optional[int] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"id": request_id, "ok": True, "result": result}
    if warnings:
        out["warnings"] = list(warnings)
    if assumptions:
        out["assumptions"] = list(assumptions)
    if duration_ms is not None:
        out["duration_ms"] = int(duration_ms)
    return out


def addon_fail(request_id: str, code: str, message: str, **details: Any) -> Dict[str, Any]:
    return {"id": request_id, "ok": False, "error": error(code, message, **details)}


def validate_addon_request(obj: Any) -> Dict[str, Any]:
    if not isinstance(obj, Mapping):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "RPC line must be a JSON object")
    for key in ("id", "tool"):
        if not isinstance(obj.get(key), str):
            raise ProtocolError(ErrorCode.BAD_REQUEST, f"RPC field '{key}' is required")
    if str(obj.get("v")) != PROTOCOL_VERSION:
        raise ProtocolError(
            ErrorCode.UNSUPPORTED,
            "protocol version mismatch between connector and add-on",
            {"expected": PROTOCOL_VERSION, "got": obj.get("v")},
        )
    params = obj.get("params") or {}
    if not isinstance(params, Mapping):
        raise ProtocolError(ErrorCode.PARAM_INVALID, "RPC params must be an object")
    return {
        "id": obj["id"],
        "tool": obj["tool"],
        "params": dict(params),
        "timeout_s": float(obj.get("timeout_s") or DEFAULT_JOB_TIMEOUT),
        "dry_run": bool(obj.get("dry_run")),
        "auth": obj.get("auth") or "",
        "meta": obj.get("meta") or {},
    }


# ---------------------------------------------------------------------------
# Small helpers used by both sides
# ---------------------------------------------------------------------------


def ok(payload: Optional[Dict[str, Any]] = None, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": True}
    if payload:
        out.update(payload)
    out.update(extra)
    return out


def tools_by_namespace(names: Iterable[str]) -> Dict[str, List[str]]:
    grouped: Dict[str, List[str]] = {}
    for name in sorted(names):
        ns, _, _ = name.partition(".")
        grouped.setdefault(ns, []).append(name)
    return grouped

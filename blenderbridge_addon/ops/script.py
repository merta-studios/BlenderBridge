"""script.run - the deliberately sharp tool.

Three independent locks, all of which must be open:

1. The connector's ``allow_script_mode`` setting.
2. An explicit approval (the connector enforces ``script_requires_approval``).
3. The add-on's own ``allow_script_mode`` preference, off by default.

And it is never reachable without the shared add-on token.
"""

from __future__ import annotations

import contextlib
import io
import traceback
from typing import Any, Dict

import bpy

from .. import helpers as H
from ..shared.protocol import ErrorCode

MAX_CODE_BYTES = 200_000


def script_run(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    from .. import server  # local import to avoid a cycle

    prefs = server.prefs()
    if not prefs.allow_script_mode:
        raise H.fail(
            ErrorCode.FORBIDDEN,
            "Der Skript-Modus ist im Add-on nicht erlaubt. Aktiviere ihn in Blender unter "
            "Einstellungen → Add-ons → BlenderBridge, wenn du ihn wirklich brauchst.",
            setting="allow_script_mode",
        )
    code = params.get("code")
    if not isinstance(code, str) or not code.strip():
        raise H.fail(ErrorCode.PARAM_INVALID, "code fehlt")
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise H.fail(ErrorCode.PAYLOAD_TOO_LARGE, "Code ist zu groß")
    description = str(params.get("description") or "")[:500]

    namespace: Dict[str, Any] = {
        "__name__": "__blenderbridge_script__",
        "bpy": bpy,
        "H": H,
        "result": None,
        "print": print,
    }
    stdout = io.StringIO()
    H.push_undo("Skript")
    error = None
    try:
        with contextlib.redirect_stdout(stdout):
            exec(compile(code, "<blenderbridge-script>", "exec"), namespace)  # noqa: S102
    except Exception as exc:  # noqa: BLE001 - report, do not crash Blender
        error = {
            "type": type(exc).__name__,
            "message": H.short_error(exc, 500),
            "traceback": traceback.format_exc()[-2000:],
        }
    output = stdout.getvalue()[-8000:]
    H.add_history(bpy.context.object, {"action": "script.run", "description": description,
                                       "error": bool(error)}) if bpy.context.object else None
    result_value = namespace.get("result")
    try:
        serialised = result_value if isinstance(result_value, (dict, list, str, int, float, bool, type(None))) else repr(result_value)
    except Exception:  # noqa: BLE001
        serialised = "<nicht serialisierbar>"
    if error:
        return {
            "ok": False,
            "stdout": output,
            "error": error,
            "note": "Der Code lief bis zum Fehler. Änderungen davor können bereits "
                    "angewendet sein - die Sicherung vor dem Aufruf hilft.",
        }
    return {
        "ok": True,
        "stdout": output,
        "result": serialised,
        "description": description,
        "warning": ("Skript-Modus: dieser Code hatte vollen Zugriff auf Blender und "
                    "damit auf deine Dateien. Prüfe das Protokoll im Connector."),
    }


TOOLS = {"script.run": script_run}

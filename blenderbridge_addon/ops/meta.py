"""meta.* and model.*: durable project memory on top of Blender data.

Everything is stored in a custom property ``bb_meta`` on the object (plus a
stable ``bb_id``), so it survives saving, reopening, duplicating and - within
reason - renaming.  The user can inspect it in Blender: Object Properties →
Custom Properties.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

import bpy

from .. import helpers as H
from ..shared.protocol import ErrorCode

RESERVED = {"history"}


def meta_set(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_active_or_named(params) if hasattr(H, "get_active_or_named") else H.active_or_named(params)
    key = str(params.get("key") or "").strip()
    if not key:
        raise H.fail(ErrorCode.PARAM_INVALID, "key fehlt (z. B. 'style', 'parts.trunk', 'params')")
    if key in RESERVED:
        raise H.fail(ErrorCode.FORBIDDEN, f"'{key}' wird automatisch geführt")
    if "value" not in params:
        raise H.fail(ErrorCode.PARAM_INVALID, "value fehlt")
    value = params["value"]
    H.push_undo("Metadaten setzen")
    meta = H.meta_doc(obj)
    target = meta
    parts = key.split(".")
    for part in parts[:-1]:
        nxt = target.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            target[part] = nxt
        target = nxt
    if value is None:
        target.pop(parts[-1], None)
    else:
        target[parts[-1]] = value
    H.save_meta(obj, meta)
    H.add_history(obj, {"action": "meta.set", "key": key})
    return {"object": obj.name, "key": key, "stored": value is not None}


def meta_get(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.active_or_named(params)
    key = params.get("key")
    meta = H.meta_doc(obj)
    if not key:
        return {"object": obj.name, "meta": meta}
    target: Any = meta
    for part in str(key).split("."):
        if not isinstance(target, dict) or part not in target:
            raise H.fail(ErrorCode.NOT_FOUND, f"Schlüssel '{key}' nicht gesetzt",
                         available=list(meta))
        target = target[part]
    return {"object": obj.name, "key": key, "value": target}


def model_tree(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    """Structure of a model: stored parts + real hierarchy + collections."""
    root_name = params.get("object")
    roots: List["bpy.types.Object"]
    if root_name:
        roots = [H.get_object(root_name)]
    else:
        roots = [o for o in bpy.data.objects if o.parent is None][:50]

    def node(obj: "bpy.types.Object", depth: int = 0) -> Dict[str, Any]:
        meta = H.meta_doc(obj)
        entry: Dict[str, Any] = {
            "kind": "object",
            "name": obj.name,
            "id": H.stable_id(obj),
            "type": obj.type,
            "parts": sorted((meta.get("parts") or {}).keys()),
            "params": sorted((meta.get("params") or {}).keys()),
            "regions": sorted((H.all_regions(obj) or {}).keys()),
            "collections": [c.name for c in obj.users_collection],
        }
        if obj.type == "MESH":
            entry["vertices"] = len(obj.data.vertices)
            entry["polygons"] = len(obj.data.polygons)
        if depth < 6 and obj.children:
            entry["children"] = [node(c, depth + 1) for c in obj.children]
        return entry

    trees = [node(o) for o in roots]
    return {
        "roots": trees,
        "count": len(roots),
        "note": "parts/params/regions kommen aus gespeicherten Metadaten, "
                "children aus der echten Blender-Hierarchie.",
    }


def model_params(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    """Set parameters of a prepared component and apply the ones with a recipe.

    A parameter is either plain data (stored, not applied) or a recipe:
        {"type": "object_scale", "axis": "z", "value": 4.0}
        {"type": "region_deform", "region": "trunk", "op": "scale", "factor": 1.2}
        {"type": "modifier", "modifier": "Bevel", "options": {"width": 0.05}}
    """
    obj = H.active_or_named(params)
    new_params = params.get("params") or {}
    if not isinstance(new_params, dict):
        raise H.fail(ErrorCode.PARAM_INVALID, "params muss ein Objekt sein")
    meta = H.meta_doc(obj)
    stored = dict(meta.get("params") or {})
    applied: Dict[str, Any] = {}
    skipped: Dict[str, str] = {}
    values = {k: (v.get("value") if isinstance(v, dict) else v) for k, v in new_params.items()}
    stored.update(values)
    meta["params"] = stored

    from . import mesh as mesh_ops
    from . import objects as object_ops

    for name, spec in new_params.items():
        if not isinstance(spec, dict) or "type" not in spec:
            skipped[name] = "nur gespeichert (keine Anwendungsregel)"
            continue
        kind = str(spec["type"])
        try:
            if kind == "object_scale":
                axis = str(spec.get("axis", "z"))
                factor = float(spec.get("value", spec.get("factor", 1.0)))
                scale = {"x": [1, 1, 1], "y": [1, 1, 1], "z": [1, 1, 1]}[axis if axis in "xyz" else "z"]
                index = {"x": 0, "y": 1, "z": 2}[axis if axis in "xyz" else "z"]
                scale[index] = factor
                object_ops.object_transform({"object": obj.name, "scale": scale,
                                             "relative": spec.get("relative", False)}, ctx)
                applied[name] = f"Skalierung {axis} x{factor}"
            elif kind == "region_deform":
                result = mesh_ops.mesh_edit({
                    "object": obj.name,
                    "region": spec.get("region"),
                    "op": spec.get("op", "scale"),
                    "amount": spec.get("amount"),
                    "factor": spec.get("factor"),
                    "axis": spec.get("axis"),
                    "angle_deg": spec.get("angle_deg"),
                }, ctx)
                applied[name] = result
            elif kind == "modifier":
                mod = obj.modifiers.get(str(spec.get("modifier", "")))
                if mod is None:
                    skipped[name] = "Modifier nicht gefunden"
                else:
                    for key, value in (spec.get("options") or {}).items():
                        if hasattr(mod, key):
                            setattr(mod, key, value)
                    applied[name] = f"Modifier {mod.name} aktualisiert"
            else:
                skipped[name] = f"unbekannter Typ '{kind}'"
        except Exception as exc:  # noqa: BLE001 - one param must not kill the rest
            skipped[name] = f"Fehler: {exc}"
    H.save_meta(obj, meta)
    H.add_history(obj, {"action": "model.params", "applied": list(applied)})
    return {"object": obj.name, "params": stored, "applied": applied, "skipped": skipped,
            "assumptions": ["Parameter ohne 'type'-Regel werden gespeichert, aber nicht "
                            "angewendet - so bleibt die Kontrolle bei dir."]}


TOOLS = {
    "meta.set": meta_set,
    "meta.get": meta_get,
    "model.tree": model_tree,
    "model.params": model_params,
}

"""relation.*: place things by describing the relationship instead of raw numbers."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import bpy
from mathutils import Euler, Matrix, Vector

from .. import facts
from .. import helpers as H
from ..shared import geometry as G
from ..shared import relations as REL
from ..shared.protocol import ErrorCode


def _target_facts(params: Dict[str, Any]) -> Dict[str, Any]:
    refs: List[Dict[str, Any]] = []
    if params.get("targets"):
        refs = list(params["targets"])
    elif params.get("target"):
        refs = [params["target"]]
    if not refs:
        raise H.fail(ErrorCode.PARAM_INVALID, "target oder targets fehlt")
    out: Dict[str, Any] = {}
    for ref in refs:
        name = ref.get("object")
        if not name:
            raise H.fail(ErrorCode.PARAM_INVALID, "target.object fehlt")
        obj = H.get_object(name)
        out[obj.name] = facts.object_facts(obj)
    return out


def _subject(params: Dict[str, Any], obj: Optional["bpy.types.Object"] = None) -> Dict[str, Any]:
    if params.get("subject"):
        return dict(params["subject"])
    if obj is not None:
        return {"dimensions": list(H.dimensions(obj)), "origin": "center"}
    dims = params.get("dimensions")
    if dims:
        return {"dimensions": list(dims), "origin": params.get("origin", "center")}
    return {"dimensions": [1.0, 1.0, 1.0], "origin": "center"}


def relation_resolve(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    targets = _target_facts(params)
    obj = H.get_object(params["object"]) if params.get("object") else None
    subject = _subject(params, obj)
    spec = {
        "placement": params.get("placement", "on"),
        "target": params.get("target"),
        "targets": params.get("targets"),
        "anchor": params.get("anchor"),
        "frame": params.get("frame"),
        "side": params.get("side"),
        "gap": params.get("gap", 0.0),
        "sink": params.get("sink", 0.0),
        "offset": params.get("offset"),
        "rotation": params.get("rotation"),
        "size": params.get("size"),
        "persist": params.get("persist"),
        "up": params.get("up"),
        "accept_ambiguities": bool(params.get("accept_ambiguities", False)),
    }
    # anchors are given either inside target or as a top level shortcut
    if params.get("anchor") and spec["target"] and "anchor" not in spec["target"]:
        spec["target"] = {**spec["target"], "anchor": params["anchor"]}
    try:
        resolved = REL.resolve_placement(spec, targets, subject)
    except REL.RelationError as exc:
        raise H.fail(ErrorCode.PARAM_INVALID, str(exc), **exc.details)
    resolved["targets_used"] = {name: {"bbox": f["bbox"], "dimensions": f["dimensions"]}
                                for name, f in targets.items()}
    return resolved


def relation_apply(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    objs = H.as_object_list(params.get("objects") or params.get("object"))
    if not objs:
        raise H.fail(ErrorCode.BAD_REQUEST, "kein Zielobjekt angegeben")
    resolved = relation_resolve({**params, "object": objs[0].name}, ctx)
    if ctx.dry_run:
        return {"dry_run": True, "resolved": resolved}
    H.push_undo("Beziehung anwenden")
    out = {}
    for obj in objs:
        out[obj.name] = _apply_one(obj, resolved)
    bpy.context.view_layer.update()
    return {
        "applied": out,
        "resolved": resolved,
        "assumptions": resolved.get("assumptions", []),
        "ambiguities": resolved.get("ambiguities", []),
        "needs_confirmation": resolved.get("needs_confirmation", False),
    }


def _apply_one(obj: "bpy.types.Object", resolved: Dict[str, Any]) -> Dict[str, Any]:
    current_dims = H.dimensions(obj)
    wanted = resolved.get("size_m") or current_dims
    factors = [wanted[i] / current_dims[i] if current_dims[i] > 1e-9 else 1.0 for i in range(3)]
    obj.scale = Vector((obj.scale.x * factors[0], obj.scale.y * factors[1],
                        obj.scale.z * factors[2]))
    rot = resolved.get("rotation_deg") or [0, 0, 0]
    obj.rotation_euler = Euler([math.radians(a) for a in rot], "XYZ")
    bpy.context.view_layer.update()
    # move so the object's bbox centre lands on the computed centre
    lo, hi = H.world_bbox(obj)
    centre = Vector(G.aabb_center((lo, hi)))
    target = Vector(resolved["center"])
    delta = target - centre
    mw = obj.matrix_world.copy()
    mw.translation = mw.translation + delta
    obj.matrix_world = mw
    bpy.context.view_layer.update()
    H.add_history(obj, {"action": "relation.apply", "placement": resolved.get("target_point")})
    lo2, hi2 = H.world_bbox(obj)
    return {
        "location": [round(c, 6) for c in obj.matrix_world.translation],
        "rotation_deg": [round(c, 4) for c in H.rotation_deg(obj)],
        "dimensions": [round(c, 6) for c in G.aabb_dims((lo2, hi2))],
        "centre": [round(c, 6) for c in G.aabb_center((lo2, hi2))],
    }


def relation_persist(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    objs = H.as_object_list(params.get("objects") or params.get("object"))
    mode = str(params.get("mode", "parent")).lower()
    target_name = params.get("target")
    if not target_name:
        raise H.fail(ErrorCode.PARAM_INVALID, "target fehlt")
    target = H.get_object(target_name)
    H.push_undo("Beziehung dauerhaft machen")
    result: Dict[str, Any] = {"mode": mode, "target": target.name, "objects": []}
    for obj in objs:
        if mode == "parent":
            world = obj.matrix_world.copy()
            obj.parent = target
            obj.matrix_parent_inverse = target.matrix_world.inverted()
            obj.matrix_world = world
            result["objects"].append({"object": obj.name, "parent": target.name})
        elif mode == "vertex_parent":
            vertex = params.get("vertex_index")
            if vertex is None:
                raise H.fail(ErrorCode.PARAM_INVALID, "vertex_parent braucht vertex_index")
            world = obj.matrix_world.copy()
            H.select_only([obj, target], target)
            bpy.ops.object.mode_set(mode="EDIT")
            bm_ok = False
            try:
                bpy.ops.mesh.select_all(action="DESELECT")
                bpy.ops.object.mode_set(mode="OBJECT")
                target.data.vertices[int(vertex)].select = True
                bm_ok = True
            finally:
                pass
            if not bm_ok:
                raise H.fail(ErrorCode.BLENDER_ERROR, "Vertex konnte nicht ausgewählt werden")
            H.select_only([obj, target], target)
            bpy.ops.object.mode_set(mode="EDIT")
            try:
                bpy.ops.object.vertex_parent_set()
            finally:
                bpy.ops.object.mode_set(mode="OBJECT")
            obj.matrix_world = world
            result["objects"].append({"object": obj.name, "vertex": int(vertex)})
        elif mode == "constraint":
            added = []
            for spec in params.get("constraints") or [
                {"type": "COPY_LOCATION"}, {"type": "COPY_ROTATION"}]:
                con = obj.constraints.new(str(spec["type"]))
                con.target = target
                for key, value in spec.items():
                    if key != "type" and hasattr(con, key):
                        try:
                            setattr(con, key, value)
                        except (TypeError, ValueError):
                            pass
                added.append(con.name)
            result["objects"].append({"object": obj.name, "constraints": added})
        elif mode == "surface":
            mod = obj.modifiers.new(name="BB_Shrinkwrap", type="SHRINKWRAP")
            mod.target = target
            mod.wrap_method = str(params.get("wrap_method", "NEAREST_SURFACEPOINT"))
            mod.offset = float(params.get("offset", 0.0))
            result["objects"].append({"object": obj.name, "modifier": mod.name})
        else:
            raise H.fail(ErrorCode.PARAM_INVALID, f"unbekannter Modus: {mode}",
                         allowed=["parent", "vertex_parent", "constraint", "surface"])
    bpy.context.view_layer.update()
    result["note"] = {
        "parent": "Das Objekt folgt dem Ziel bei Bewegung/Skalierung.",
        "vertex_parent": "Hängt an einem einzelnen Punkt des Ziels (auch bei Verformung).",
        "constraint": "Position/Rotation bleiben synchron, Hierarchie bleibt frei.",
        "surface": "Shrinkwrap hält das Objekt auf der Zieloberfläche, auch nach Umformung.",
    }.get(mode, "")
    return result


def relation_distribute(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    spec = {
        "count": int(params.get("count", 3)),
        "target": params.get("target"),
        "from": params.get("from", "base"),
        "to": params.get("to", "top"),
        "between": params.get("between"),
        "include_ends": params.get("include_ends", True),
        "rotation": params.get("rotation"),
    }
    if params.get("target", {}).get("path") if isinstance(params.get("target"), dict) else False:
        spec["target"] = params["target"]
    targets = _target_facts(params)
    try:
        distributed = REL.distribute(spec, targets)
    except REL.RelationError as exc:
        raise H.fail(ErrorCode.PARAM_INVALID, str(exc), **exc.details)

    obj = H.get_object(params["object"]) if params.get("object") else None
    if ctx.dry_run or obj is None:
        return {"dry_run": ctx.dry_run, "points": distributed["points"],
                "assumptions": distributed.get("assumptions", [])}

    count = len(distributed["points"])
    H.push_undo("Verteilen")
    created: List[str] = []
    base_world = obj.matrix_world.copy()
    for i, point in enumerate(distributed["points"]):
        new = obj.copy()
        if obj.data is not None:
            new.data = obj.data
        new.name = f"{obj.name}_dist_{i + 1}"
        for coll in obj.users_collection:
            coll.objects.link(new)
        mw = base_world.copy()
        mw.translation = Vector(point)
        new.matrix_world = mw
        if distributed.get("rotation_deg"):
            new.rotation_euler = Euler([math.radians(a) for a in distributed["rotation_deg"]],
                                       "XYZ")
        created.append(new.name)
    return {"created": created, "points": distributed["points"],
            "assumptions": distributed.get("assumptions", []),
            "note": "Kopien teilen sich die Mesh-Daten (instanziert); für eigene Änderungen "
                    "pro Kopie vorher duplizieren."}


TOOLS = {
    "relation.resolve": relation_resolve,
    "relation.apply": relation_apply,
    "relation.persist": relation_persist,
    "relation.distribute": relation_distribute,
}

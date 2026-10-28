"""region.*, anchor.* and path.*: naming parts of a mesh and remembering them."""

from __future__ import annotations

import time
from typing import Any, Dict, List

import bpy
from mathutils import Vector

from .. import helpers as H
from ..shared import geometry as G
from ..shared import regions as RG
from ..shared.protocol import ErrorCode


def region_list(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_mesh_object(params.get("object"))
    state = H.refresh_region_state(obj)
    out = {}
    for name, raw in H.all_regions(obj).items():
        out[name] = {
            "vertices": len(raw.get("vertices", [])),
            "full": sum(1 for w in raw.get("weights", []) if w >= 0.999),
            "falloff": sum(1 for w in raw.get("weights", []) if 0 < w < 0.999),
            "kind": raw.get("kind", "region"),
            "protected": raw.get("protected", False),
            "source": raw.get("source", ""),
            "note": raw.get("note", ""),
            "created_at": raw.get("created_at", ""),
            "bounds": raw.get("bounds"),
            "state": state.get(name, "unknown"),
        }
    return {"object": obj.name, "count": len(out), "regions": out,
            "vertices": len(obj.data.vertices)}


def region_create(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_mesh_object(params.get("object"))
    name = str(params.get("name") or "").strip()
    if not name:
        raise H.fail(ErrorCode.PARAM_INVALID, "name fehlt")
    spec = dict(params)
    spec.setdefault("source", "box")
    spec["name"] = name
    region = H.build_region(obj, spec)
    if ctx.dry_run:
        return {"dry_run": True, "name": name, "vertices": len(region.vertices),
                "summary": RG.summarise_weights(region.weights)}
    H.push_undo(f"Bereich {name}")
    stored = H.store_region(obj, region)
    H.add_history(obj, {"action": "region.create", "name": name,
                        "vertices": len(region.vertices), "source": region.source})
    return {
        "object": obj.name,
        "region": {k: stored[k] for k in ("name", "source", "kind", "protected",
                                          "vertices", "bounds")},
        "summary": RG.summarise_weights(region.weights),
        "vertex_group": RG.REGION_PREFIX + name,
        "hint": "Der Bereich ist als Vertex-Gruppe gespeichert und kann in mesh.edit "
                "über 'region' verwendet werden.",
    }


def region_from_selection(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.active_or_named(params)
    if obj.type != "MESH":
        raise H.fail(ErrorCode.MESH_NOT_FOUND, "aktives Objekt ist kein Mesh")
    indices = H.selected_vertex_indices(obj)
    if not indices:
        raise H.fail(ErrorCode.NO_GEOMETRY,
                     "In Blender ist nichts ausgewählt. Erst Punkte/Flächen auswählen "
                     "(Tab -> Auswahl) und dann erneut aufrufen.")
    spec = dict(params)
    spec["source"] = "selection"
    spec.setdefault("name", f"selection_{int(time.time()) % 100000}")
    return region_create(spec, ctx)


def region_get(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_mesh_object(params.get("object"))
    name = str(params.get("name") or "")
    raw = H.all_regions(obj).get(name)
    if raw is None:
        raise H.fail(ErrorCode.REGION_NOT_FOUND, f"Bereich '{name}' nicht gefunden",
                     available=sorted(H.all_regions(obj)))
    region = RG.region_from_dict(raw)
    weights = RG.weights_from_region(region, len(obj.data.vertices))
    return {
        "object": obj.name,
        "region": {k: raw.get(k) for k in ("name", "kind", "protected", "source",
                                           "note", "created_at", "vertex_count",
                                           "poly_count", "bounds")},
        "summary": RG.summarise_weights(weights),
        "state": RG.region_state(region, len(obj.data.vertices), len(obj.data.polygons),
                                 [tuple(e.vertices) for e in obj.data.edges]),
        "vertices": region.vertices[:200],
        "truncated": len(region.vertices) > 200,
    }


def region_update(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_mesh_object(params.get("object"))
    name = str(params.get("name") or "")
    doc = H.region_doc(obj)
    raw = (doc.get("regions") or {}).get(name)
    if raw is None:
        raise H.fail(ErrorCode.REGION_NOT_FOUND, f"Bereich '{name}' nicht gefunden")
    H.push_undo("Bereich ändern")
    for field in ("note", "kind", "protected"):
        if field in params:
            raw[field] = params[field]
    if params.get("rename"):
        new_name = str(params["rename"])
        if new_name in doc["regions"]:
            raise H.fail(ErrorCode.CONFLICT, f"Bereich '{new_name}' existiert bereits")
        doc["regions"][new_name] = raw
        del doc["regions"][name]
        H.delete_region(obj, name)  # removes the old vertex group
        name = new_name
        doc = H.region_doc(obj)
        doc["regions"][name] = raw
    H.save_region_doc(obj, doc)
    if params.get("protected") is not False:
        H.sync_vertex_group(obj, RG.region_from_dict(raw))
    return {"object": obj.name, "region": name,
            "updated": [k for k in ("note", "kind", "protected", "rename") if k in params]}


def region_delete(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_mesh_object(params.get("object"))
    name = str(params.get("name") or "")
    H.push_undo("Bereich löschen")
    ok = H.delete_region(obj, name)
    if not ok:
        raise H.fail(ErrorCode.REGION_NOT_FOUND, f"Bereich '{name}' nicht gefunden")
    return {"object": obj.name, "deleted": name}


def region_check(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_mesh_object(params.get("object"))
    state = H.refresh_region_state(obj)
    stale = [k for k, v in state.items() if v != "ok"]
    return {
        "object": obj.name,
        "regions": state,
        "stale": stale,
        "ok": not stale,
        "advice": ("Bereiche sind aktuell." if not stale else
                   "Veraltete Bereiche mit region.remap reparieren oder neu anlegen."),
    }


def region_remap(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    """Repair a stored region after the topology changed.

    Three honest strategies, in increasing optimism:

    ``keep_indices``  refresh the fingerprint, keep the indices (correct when
                      only positions changed but the fingerprint was outdated)
    ``clamp``         drop indices that no longer exist
    ``bounds``        rebuild the region from the stored bounding box
    """
    obj = H.get_mesh_object(params.get("object"))
    name = str(params.get("name") or "")
    strategy = str(params.get("strategy", "clamp")).lower()
    raw = H.all_regions(obj).get(name)
    if raw is None:
        raise H.fail(ErrorCode.REGION_NOT_FOUND, f"Bereich '{name}' nicht gefunden")
    n = len(obj.data.vertices)
    H.push_undo("Bereich reparieren")
    if strategy == "keep_indices":
        region = RG.region_from_dict(raw)
        region.vertices = [i for i in region.vertices if 0 <= i < n]
        region.weights = region.weights[: len(region.vertices)]
    elif strategy == "clamp":
        region = RG.region_from_dict(raw)
        pairs = [(i, w) for i, w in zip(region.vertices, region.weights) if 0 <= i < n]
        region.vertices = [p[0] for p in pairs]
        region.weights = [p[1] for p in pairs]
    elif strategy == "bounds":
        if not raw.get("bounds"):
            raise H.fail(ErrorCode.PRECONDITION_FAILED, "kein gespeicherter Bounding-Box-Wert")
        lo = H.world_to_local(obj, raw["bounds"][0])
        hi = H.world_to_local(obj, raw["bounds"][1])
        idx, weights = RG.region_from_box(H.local_positions(obj), lo, hi,
                                          float(params.get("soft", 0.0)))
        region = RG.region_from_dict(raw)
        region.vertices, region.weights = idx, weights
    else:
        raise H.fail(ErrorCode.PARAM_INVALID, f"unbekannte Strategie: {strategy}",
                     allowed=["keep_indices", "clamp", "bounds"])
    if not region.vertices:
        raise H.fail(ErrorCode.NO_GEOMETRY,
                     "Reparatur ergab einen leeren Bereich - bitte neu anlegen.")
    stored = H.store_region(obj, region)
    return {"object": obj.name, "region": name, "strategy": strategy,
            "vertices": len(region.vertices),
            "warning": ("Reparatur ist eine Schätzung. Prüfe das Ergebnis im Viewport, "
                        "bevor du darauf weitere Änderungen aufbaust."),
            "region_info": {k: stored[k] for k in ("name", "vertices", "bounds")}}


# ---------------------------------------------------------------------------
# Anchors
# ---------------------------------------------------------------------------


def anchor_set(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_object(params.get("object"))
    name = str(params.get("name") or "").strip()
    if not name:
        raise H.fail(ErrorCode.PARAM_INVALID, "name fehlt")
    position = params.get("position")
    if not position:
        anchor_name = params.get("from_anchor", "center")
        box = H.world_bbox(obj)
        position = list(G.aabb_anchor(box, anchor_name))
    direction = params.get("direction")
    H.push_undo("Anker setzen")
    doc = H.anchor_doc(obj)
    doc.setdefault("anchors", {})[name] = {
        "position": [float(c) for c in position],
        "direction": [float(c) for c in direction] if direction else None,
        "source": "manual",
        "object": obj.name,
    }
    H._write_json(obj, RG.ANCHOR_PROP, doc)
    H.add_history(obj, {"action": "anchor.set", "name": name})
    return {"object": obj.name, "anchor": name, "position": list(position)}


def anchor_list(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_object(params.get("object"))
    return {"object": obj.name, "anchors": H.anchor_doc(obj).get("anchors", {})}


# ---------------------------------------------------------------------------
# Paths (curved axes: trunk, tail, spine ...)
# ---------------------------------------------------------------------------


def path_set(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_object(params.get("object"))
    name = str(params.get("name") or "").strip()
    if not name:
        raise H.fail(ErrorCode.PARAM_INVALID, "name fehlt")
    points = params.get("points")
    if not points and params.get("from_region"):
        points = _path_from_region(obj, str(params["from_region"]),
                                   int(params.get("slices", 8)))
    if not points or len(points) < 2:
        raise H.fail(ErrorCode.PARAM_INVALID,
                     "points (mind. 2) oder from_region angeben")
    H.push_undo("Verlauf speichern")
    doc = H.path_doc(obj)
    doc.setdefault("paths", {})[name] = {
        "points": [[float(c) for c in p] for p in points],
        "object": obj.name,
        "source": params.get("source", "manual"),
    }
    H._write_json(obj, RG.PATH_PROP, doc)
    H.add_history(obj, {"action": "path.set", "name": name, "points": len(points)})
    return {"object": obj.name, "path": name, "points": len(points)}


def _path_from_region(obj: "bpy.types.Object", region_name: str, slices: int) -> List[List[float]]:
    """Trace the centreline of a roughly tubular region (trunk axis)."""
    region = H.load_region(obj, region_name)
    positions = H.local_positions(obj)
    mw = obj.matrix_world
    world = [(i, tuple(mw @ Vector(positions[i]))) for i in region.vertices]
    if len(world) < 4:
        raise H.fail(ErrorCode.NO_GEOMETRY, "Region zu klein für eine Achse")
    axis, _ = G.principal_axis([p for _, p in world])
    if axis[2] < 0:
        axis = G.vneg(axis)
    projections = [(G.vdot(p, axis), p) for _, p in world]
    lo = min(p[0] for p in projections)
    hi = max(p[0] for p in projections)
    span = (hi - lo) or 1.0
    buckets: List[List[G.Vec3]] = [[] for _ in range(max(2, slices))]
    for value, point in projections:
        i = min(len(buckets) - 1, int((value - lo) / span * len(buckets)))
        buckets[i].append(point)
    points = [list(G.centroid(b)) for b in buckets if b]
    return points


def path_list(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_object(params.get("object"))
    paths = H.path_doc(obj).get("paths", {})
    return {"object": obj.name,
            "paths": {name: {"points": len(v.get("points", []))} for name, v in paths.items()}}


TOOLS = {
    "region.list": region_list,
    "region.create": region_create,
    "region.from_selection": region_from_selection,
    "region.get": region_get,
    "region.update": region_update,
    "region.delete": region_delete,
    "region.check": region_check,
    "region.remap": region_remap,
    "anchor.set": anchor_set,
    "anchor.list": anchor_list,
    "path.set": path_set,
    "path.list": path_list,
}

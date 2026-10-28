"""Measuring the open scene so an agent can reason about it.

The output of :func:`object_facts` is exactly the ``facts`` dictionary the
relation solver expects, so "place X on the palm" is answered from real geometry
instead of guesses.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import bpy
from mathutils import Vector

from . import helpers as H
from .shared import geometry as G
from .shared import regions as RG

MAX_SAMPLE = 6000


def _sampled_world_vertices(obj: "bpy.types.Object", limit: int = MAX_SAMPLE) -> List[G.Vec3]:
    mesh = obj.data
    count = len(mesh.vertices)
    if count == 0:
        return []
    step = max(1, count // limit)
    mw = obj.matrix_world
    return [tuple(mw @ mesh.vertices[i].co) for i in range(0, count, step)]


def dominant_axis(obj: "bpy.types.Object") -> Dict[str, Any]:
    points = _sampled_world_vertices(obj)
    if len(points) < 3:
        return {"axis": [0.0, 0.0, 1.0], "spread": 0.0}
    axis, spread = G.principal_axis(points)
    # keep the axis pointing "up" when it is roughly vertical, for readability
    if axis[2] < 0:
        axis = G.vneg(axis)
    return {"axis": [round(c, 6) for c in axis], "spread": round(spread, 4)}


def surface_normal(obj: "bpy.types.Object", point: Optional[Sequence[float]] = None) -> Optional[List[float]]:
    """Normal of the face closest to ``point`` (default: the highest point)."""
    mesh = obj.data
    if not mesh.polygons:
        return None
    mw = obj.matrix_world
    if point is None:
        pts = _sampled_world_vertices(obj)
        if not pts:
            return None
        _, hi = G.aabb(pts)
        target = Vector(hi)
    else:
        target = Vector(tuple(point))
    best = None
    best_d = float("inf")
    for poly in mesh.polygons[:4000]:
        center = mw @ poly.center
        d = (center - target).length
        if d < best_d:
            best_d = d
            best = poly
    if best is None:
        return None
    normal = (mw.to_3x3() @ best.normal).normalized()
    return [round(normal.x, 6), round(normal.y, 6), round(normal.z, 6)]


def bbox_anchors(box: Sequence[Sequence[float]]) -> Dict[str, Dict[str, Any]]:
    lo, hi = box
    out: Dict[str, Dict[str, Any]] = {}
    for name in ("center", "top", "bottom", "base", "left", "right", "front", "back",
                 "top_left", "top_right", "bottom_left", "bottom_right"):
        try:
            out[name] = {"position": list(G.aabb_anchor((lo, hi), name)), "source": "bbox"}
        except ValueError:
            continue
    return out


def stored_anchors(obj: "bpy.types.Object") -> Dict[str, Dict[str, Any]]:
    doc = H.anchor_doc(obj)
    out: Dict[str, Dict[str, Any]] = {}
    for name, value in (doc.get("anchors") or {}).items():
        if isinstance(value, dict) and value.get("position"):
            out[name] = {"position": list(value["position"]),
                         "direction": value.get("direction"),
                         "source": value.get("source", "stored")}
        elif isinstance(value, (list, tuple)) and len(value) == 3:
            out[name] = {"position": list(value), "source": "stored"}
    return out


def stored_paths(obj: "bpy.types.Object") -> Dict[str, List[List[float]]]:
    doc = H.path_doc(obj)
    out: Dict[str, List[List[float]]] = {}
    for name, value in (doc.get("paths") or {}).items():
        points = value.get("points") if isinstance(value, dict) else value
        if isinstance(points, list) and len(points) >= 2:
            out[name] = [[float(c) for c in p] for p in points]
    return out


def mesh_summary(obj: "bpy.types.Object") -> Dict[str, Any]:
    mesh = obj.data
    return {
        "vertices": len(mesh.vertices),
        "edges": len(mesh.edges),
        "polygons": len(mesh.polygons),
        "triangles": H.tri_count(obj),
        "materials": [slot.material.name if slot.material else None for slot in obj.material_slots],
        "uv_layers": [layer.name for layer in mesh.uv_layers],
        "vertex_colors": [c.name for c in getattr(mesh, "color_attributes", [])],
        "vertex_groups": [g.name for g in obj.vertex_groups],
        "shape_keys": len(mesh.shape_keys.key_blocks) - 1 if mesh.shape_keys else 0,
        "is_manifold_checked": False,
    }


def part_facts(obj: "bpy.types.Object", meta: Dict[str, Any]) -> Dict[str, Any]:
    """Facts for the named parts stored in the object metadata."""
    parts: Dict[str, Any] = {}
    stored = meta.get("parts") or {}
    for name, info in stored.items():
        if not isinstance(info, dict):
            continue
        entry: Dict[str, Any] = {"name": name, "source": "metadata"}
        if info.get("bbox"):
            entry["bbox"] = info["bbox"]
            entry["dimensions"] = list(G.aabb_dims((info["bbox"][0], info["bbox"][1])))
            entry["anchors"] = bbox_anchors(info["bbox"])
        if info.get("axis"):
            entry["axis"] = info["axis"]
        if info.get("vertex_indices"):
            entry["region"] = name
            entry["vertices"] = len(info["vertex_indices"])
        parts[name] = entry
    # every stored region is also usable as a part
    for name, raw in H.all_regions(obj).items():
        if name in parts:
            continue
        region = RG.region_from_dict(raw)
        if region.bounds:
            parts[name] = {
                "name": name,
                "source": "region",
                "bbox": region.bounds,
                "dimensions": list(G.aabb_dims((region.bounds[0], region.bounds[1]))),
                "anchors": bbox_anchors(region.bounds),
                "vertices": region.size,
                "kind": region.kind,
            }
    return parts


def object_facts(obj: "bpy.types.Object", *, include_parts: bool = True,
                 include_paths: bool = True) -> Dict[str, Any]:
    box = H.world_bbox(obj)
    dims = G.aabb_dims(box)
    center = G.aabb_center(box)
    facts: Dict[str, Any] = {
        "name": obj.name,
        "type": obj.type,
        "id": H.stable_id(obj),
        "bbox": [list(box[0]), list(box[1])],
        "dimensions": [round(c, 6) for c in dims],
        "center": [round(c, 6) for c in center],
        "origin": [round(c, 6) for c in obj.matrix_world.translation],
        "rotation_deg": [round(c, 4) for c in H.rotation_deg(obj)],
        "axes_world": H.axes_world(obj),
        "scale": [round(c, 6) for c in obj.scale],
        "parent": obj.parent.name if obj.parent else None,
        "collections": [c.name for c in obj.users_collection],
        "visible": not obj.hide_get(),
        "anchors": {**bbox_anchors(box), **stored_anchors(obj)},
        "normal": surface_normal(obj),
        **dominant_axis(obj),
    }
    if obj.type == "MESH":
        facts["mesh"] = mesh_summary(obj)
    if include_paths:
        paths = stored_paths(obj)
        if paths:
            facts["paths"] = paths
    if include_parts:
        meta = H.meta_doc(obj)
        parts = part_facts(obj, meta)
        if parts:
            facts["parts"] = parts
        if meta.get("style"):
            facts["style"] = meta["style"]
    return facts


def scene_info(detail: str = "summary") -> Dict[str, Any]:
    scene = bpy.context.scene
    objects = list(bpy.data.objects)
    meshes = [o for o in objects if o.type == "MESH"]
    totals = {
        "objects": len(objects),
        "meshes": len(meshes),
        "vertices": sum(len(o.data.vertices) for o in meshes),
        "polygons": sum(len(o.data.polygons) for o in meshes),
        "materials": len(bpy.data.materials),
        "collections": len(bpy.data.collections),
        "images": len(bpy.data.images),
        "cameras": len(bpy.data.cameras),
        "lights": len(bpy.data.lights),
    }
    info: Dict[str, Any] = {
        "blender": {
            "version": bpy.app.version_string,
            "version_tuple": list(bpy.app.version),
            "api": bpy.app.version_string,
            "binary": bpy.app.binary_path,
            "background": bpy.app.background,
        },
        "file": {
            "path": bpy.data.filepath or "",
            "name": bpy.path.basename(bpy.data.filepath) if bpy.data.filepath else "",
            "saved": bool(bpy.data.filepath),
            "dirty": bpy.data.is_dirty,
        },
        "scene": {
            "name": scene.name,
            "frame": scene.frame_current,
            "frame_start": scene.frame_start,
            "frame_end": scene.frame_end,
            "fps": scene.render.fps,
            "unit_system": scene.unit_settings.system,
            "unit_scale": scene.unit_settings.scale_length,
            "render_engine": scene.render.engine,
        },
        "totals": totals,
        "active_object": (bpy.context.view_layer.objects.active.name
                          if bpy.context.view_layer.objects.active else None),
        "selected": [o.name for o in bpy.context.selected_objects],
        "mode": bpy.context.mode,
    }
    if detail == "full":
        info["objects"] = [
            {
                "name": o.name,
                "type": o.type,
                "parent": o.parent.name if o.parent else None,
                "collections": [c.name for c in o.users_collection],
                "vertices": len(o.data.vertices) if o.type == "MESH" else 0,
                "polygons": len(o.data.polygons) if o.type == "MESH" else 0,
                "visible": not o.hide_get(),
            }
            for o in objects
        ]
        info["collections"] = [
            {"name": c.name, "objects": [o.name for o in c.objects],
             "children": [ch.name for ch in c.children]}
            for c in bpy.data.collections
        ]
    return info


def scene_tree(root: Optional[str] = None, depth: int = 8) -> Dict[str, Any]:
    """Hierarchy of collections and objects as a nested structure."""

    def collection_node(coll: "bpy.types.Collection", level: int) -> Dict[str, Any]:
        node: Dict[str, Any] = {
            "kind": "collection",
            "name": coll.name,
            "objects": [
                {"kind": "object", "name": o.name, "type": o.type,
                 "parent": o.parent.name if o.parent else None,
                 "children": [c.name for c in o.children]}
                for o in coll.objects
            ],
        }
        if level < depth:
            node["collections"] = [collection_node(ch, level + 1) for ch in coll.children]
        return node

    if root:
        coll = bpy.data.collections.get(root)
        if coll is None:
            from .shared.protocol import ErrorCode, ProtocolError
            raise ProtocolError(ErrorCode.NOT_FOUND, f"Collection '{root}' nicht gefunden",
                                {"available": [c.name for c in bpy.data.collections]})
        return {"root": collection_node(coll, 0)}
    roots = [c for c in bpy.data.collections if not c.users_scene or
             any(c.name in [sc.name for sc in c.users_scene])]
    scene_colls = [c for c in bpy.context.scene.collection.children]
    return {
        "scene": bpy.context.scene.name,
        "collections": [collection_node(c, 0) for c in (scene_colls or roots)],
        "loose_objects": [
            {"kind": "object", "name": o.name, "type": o.type}
            for o in bpy.context.scene.collection.objects
        ],
    }


def scene_stats() -> Dict[str, Any]:
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    boxes = []
    for obj in meshes:
        try:
            boxes.append(H.world_bbox(obj))
        except Exception:  # noqa: BLE001
            continue
    extent = None
    if boxes:
        union = G.aabb_union(boxes)
        extent = {
            "min": [round(c, 4) for c in union[0]],
            "max": [round(c, 4) for c in union[1]],
            "dimensions": [round(c, 4) for c in G.aabb_dims(union)],
        }
    return {
        "objects": len(bpy.data.objects),
        "meshes": len(meshes),
        "vertices": sum(len(o.data.vertices) for o in meshes),
        "polygons": sum(len(o.data.polygons) for o in meshes),
        "triangles": sum(H.tri_count(o) for o in meshes),
        "materials": len(bpy.data.materials),
        "scene_extent": extent,
        "units": bpy.context.scene.unit_settings.system,
    }

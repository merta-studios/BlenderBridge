"""object.* tools: create, transform, duplicate, hierarchy, modifiers, probes."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import bpy
from mathutils import Euler, Matrix, Vector

from .. import facts
from .. import helpers as H
from ..shared import geometry as G
from ..shared import regions as RG
from ..shared.protocol import ErrorCode

PRIMITIVES = ("cube", "sphere", "uv_sphere", "ico_sphere", "cylinder", "cone",
              "torus", "plane", "circle", "monkey", "empty")

MODIFIER_TYPES = {
    "bevel", "subsurf", "solidify", "mirror", "array", "shrinkwrap", "triangulate",
    "weighted_normal", "smooth", "displace", "simple_deform", "lattice", "remesh",
    "boolean", "decimate", "wireframe", "edge_split", "screw", "skin", "ocean",
}

CONSTRAINT_TYPES = {
    "CHILD_OF", "COPY_LOCATION", "COPY_ROTATION", "COPY_SCALE", "COPY_TRANSFORMS",
    "DAMPED_TRACK", "TRACK_TO", "LIMIT_LOCATION", "LIMIT_ROTATION", "STRETCH_TO",
    "FLOOR", "SHRINKWRAP", "CLAMP_TO", "FOLLOW_PATH",
}


def _set_transform(obj, params: Dict[str, Any], relative: bool) -> Dict[str, Any]:
    loc = params.get("location")
    rot = params.get("rotation_deg")
    scale = params.get("scale")
    if loc is not None:
        target = Vector(tuple(loc))
        obj.location = obj.location + target if relative else target
    if rot is not None:
        radians = [math.radians(a) for a in rot]
        if relative:
            current = obj.rotation_euler
            obj.rotation_euler = Euler((current.x + radians[0], current.y + radians[1],
                                        current.z + radians[2]), "XYZ")
        else:
            obj.rotation_euler = Euler(radians, "XYZ")
    if scale is not None:
        if relative:
            obj.scale = Vector((obj.scale.x * scale[0], obj.scale.y * scale[1],
                                obj.scale.z * scale[2]))
        else:
            obj.scale = Vector(tuple(scale))
    return {
        "location": [round(c, 6) for c in obj.location],
        "rotation_deg": [round(math.degrees(a), 4) for a in obj.rotation_euler],
        "scale": [round(c, 6) for c in obj.scale],
    }


def object_list(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    kind = params.get("type")
    collection = params.get("collection")
    limit = int(params.get("limit", 500))
    out = []
    for obj in bpy.data.objects:
        if kind and obj.type.lower() != str(kind).lower():
            continue
        if collection and collection not in [c.name for c in obj.users_collection]:
            continue
        entry = {
            "name": obj.name,
            "type": obj.type,
            "parent": obj.parent.name if obj.parent else None,
            "collections": [c.name for c in obj.users_collection],
            "visible": not obj.hide_get(),
            "selected": obj.select_get(),
        }
        if obj.type == "MESH":
            entry.update({"vertices": len(obj.data.vertices),
                          "polygons": len(obj.data.polygons)})
            try:
                entry["dimensions"] = [round(c, 4) for c in H.dimensions(obj)]
            except Exception:  # noqa: BLE001
                pass
        out.append(entry)
        if len(out) >= limit:
            break
    return {"count": len(out), "objects": out,
            "truncated": len(bpy.data.objects) > limit}


def object_get(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.active_or_named(params)
    data: Dict[str, Any] = {
        "name": obj.name,
        "type": obj.type,
        "id": H.stable_id(obj),
        "transform": {
            "location": [round(c, 6) for c in obj.location],
            "rotation_deg": [round(math.degrees(a), 4) for a in obj.rotation_euler],
            "scale": [round(c, 6) for c in obj.scale],
            "matrix_world": [[round(v, 6) for v in row] for row in obj.matrix_world],
        },
        "dimensions": [round(c, 6) for c in H.dimensions(obj)],
        "bbox": [list(map(lambda x: round(x, 6), box)) for box in H.world_bbox(obj)],
        "parent": obj.parent.name if obj.parent else None,
        "children": [c.name for c in obj.children],
        "collections": [c.name for c in obj.users_collection],
        "modifiers": [{"name": m.name, "type": m.type, "show_viewport": m.show_viewport}
                      for m in obj.modifiers],
        "constraints": [{"name": c.name, "type": c.type, "target": getattr(c.target, "name", None)}
                        for c in obj.constraints],
        "custom_properties": {k: obj[k] for k in obj.keys()
                              if not k.startswith("bb_") or k in ("bb_id",)},
        "metadata": H.meta_doc(obj),
        "regions": {name: {"vertices": len(r.get("vertices", [])),
                           "protected": r.get("protected", False),
                           "kind": r.get("kind", "region"),
                           "source": r.get("source", "")}
                    for name, r in H.all_regions(obj).items()},
        "anchors": list(H.anchor_doc(obj).get("anchors", {}).keys()),
        "paths": list(H.path_doc(obj).get("paths", {}).keys()),
    }
    if obj.type == "MESH":
        data["mesh"] = facts.mesh_summary(obj)
    return data


def object_create_primitive(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    kind = str(params.get("kind", "cube")).lower()
    if kind not in PRIMITIVES:
        raise H.fail(ErrorCode.PARAM_INVALID, f"unbekanntes Primitiv: {kind}",
                     allowed=list(PRIMITIVES))
    name = params.get("name")
    location = params.get("location") or (0, 0, 0)
    rotation = params.get("rotation_deg") or (0, 0, 0)
    dims = params.get("dimensions")
    scale = params.get("scale") or (1, 1, 1)
    H.ensure_object_mode()
    H.push_undo(f"Primitiv {kind}")

    if kind in ("cube",):
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=tuple(location),
                                        rotation=[math.radians(a) for a in rotation])
    elif kind in ("sphere", "uv_sphere"):
        bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, segments=int(params.get("segments", 24)),
                                             ring_count=int(params.get("rings", 12)),
                                             location=tuple(location),
                                             rotation=[math.radians(a) for a in rotation])
    elif kind == "ico_sphere":
        bpy.ops.mesh.primitive_ico_sphere_add(radius=0.5,
                                              subdivisions=int(params.get("subdivisions", 2)),
                                              location=tuple(location),
                                              rotation=[math.radians(a) for a in rotation])
    elif kind == "cylinder":
        bpy.ops.mesh.primitive_cylinder_add(radius=0.5, depth=1.0,
                                            vertices=int(params.get("vertices", 24)),
                                            location=tuple(location),
                                            rotation=[math.radians(a) for a in rotation])
    elif kind == "cone":
        bpy.ops.mesh.primitive_cone_add(radius1=0.5, radius2=float(params.get("radius2", 0.0)),
                                        depth=1.0, vertices=int(params.get("vertices", 24)),
                                        location=tuple(location),
                                        rotation=[math.radians(a) for a in rotation])
    elif kind == "torus":
        bpy.ops.mesh.primitive_torus_add(major_radius=0.5, minor_radius=0.15,
                                         location=tuple(location),
                                         rotation=[math.radians(a) for a in rotation])
    elif kind == "plane":
        bpy.ops.mesh.primitive_plane_add(size=1.0, location=tuple(location),
                                         rotation=[math.radians(a) for a in rotation])
    elif kind == "circle":
        bpy.ops.mesh.primitive_circle_add(radius=0.5, fill_type="NGON",
                                          location=tuple(location),
                                          rotation=[math.radians(a) for a in rotation])
    elif kind == "monkey":
        bpy.ops.mesh.primitive_monkey_add(location=tuple(location),
                                          rotation=[math.radians(a) for a in rotation])
    else:  # empty
        bpy.ops.object.empty_add(location=tuple(location),
                                 rotation=[math.radians(a) for a in rotation])

    obj = bpy.context.object
    if obj is None:
        raise H.fail(ErrorCode.BLENDER_ERROR, "Objekt konnte nicht erzeugt werden")
    if name:
        obj.name = str(name)
        if obj.data is not None:
            obj.data.name = str(name)
    if dims:
        current = H.dimensions(obj)
        obj.scale = Vector(tuple(
            (dims[i] / current[i]) if current[i] > 1e-9 else scale[i] for i in range(3)
        ))
    elif scale and tuple(scale) != (1, 1, 1):
        obj.scale = Vector(tuple(scale))

    collection = params.get("collection")
    if collection:
        target = bpy.data.collections.get(str(collection))
        if target is None:
            target = bpy.data.collections.new(str(collection))
            bpy.context.scene.collection.children.link(target)
        for coll in list(obj.users_collection):
            coll.objects.unlink(obj)
        target.objects.link(obj)

    H.stable_id(obj)
    H.add_history(obj, {"action": "create_primitive", "kind": kind})
    return {"name": obj.name, "kind": kind,
            "location": [round(c, 6) for c in obj.location],
            "dimensions": [round(c, 6) for c in H.dimensions(obj)]}


def object_transform(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    objs = H.as_object_list(params.get("objects") or params.get("object"))
    if not objs:
        raise H.fail(ErrorCode.BAD_REQUEST, "kein Objekt angegeben")
    relative = bool(params.get("relative", False))
    H.push_undo("Transformieren")
    out = {}
    for obj in objs:
        out[obj.name] = _set_transform(obj, params, relative)
        H.add_history(obj, {"action": "transform", **out[obj.name]})
    bpy.context.view_layer.update()
    return {"updated": out}


def object_duplicate(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    objs = H.as_object_list(params.get("objects") or params.get("object"))
    if not objs:
        raise H.fail(ErrorCode.BAD_REQUEST, "kein Objekt angegeben")
    count = int(params.get("count", 1))
    offset = G.vec3(params.get("offset") or (0, 0, 0))
    linked = bool(params.get("linked", False))
    suffix = str(params.get("suffix", "_copy"))
    H.push_undo("Duplizieren")
    created: List[str] = []
    for obj in objs:
        for i in range(count):
            new = obj.copy()
            if not linked and obj.data is not None:
                new.data = obj.data.copy()
            new.name = f"{obj.name}{suffix}{i + 1}"
            new.location = obj.location + Vector(offset) * (i + 1)
            for coll in obj.users_collection:
                coll.objects.link(new)
            if obj.data is not None and not linked:
                new.data.name = new.name
            created.append(new.name)
            H.add_history(new, {"action": "duplicate", "from": obj.name})
    bpy.context.view_layer.update()
    return {"created": created}


def object_delete(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    objs = H.as_object_list(params.get("objects") or params.get("object"))
    if not objs:
        raise H.fail(ErrorCode.BAD_REQUEST, "kein Objekt angegeben")
    expected = params.get("confirm_count")
    if expected is not None and int(expected) != len(objs):
        raise H.fail(ErrorCode.PRECONDITION_FAILED,
                     f"confirm_count={expected} passt nicht zu {len(objs)} Objekten")
    H.push_undo("Löschen")
    names = [o.name for o in objs]
    for obj in objs:
        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if data is not None and getattr(data, "users", 1) == 0:
            try:
                if obj.type == "MESH":
                    bpy.data.meshes.remove(data)
            except Exception:  # noqa: BLE001
                pass
    return {"deleted": names}


def object_rename(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_object(params.get("object"))
    new_name = str(params.get("name") or "").strip()
    if not new_name:
        raise H.fail(ErrorCode.PARAM_INVALID, "name fehlt")
    old = obj.name
    H.push_undo("Umbenennen")
    obj.name = new_name
    if obj.data is not None:
        obj.data.name = new_name
    return {"old": old, "new": obj.name}


def object_move_to_collection(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    objs = H.as_object_list(params.get("objects") or params.get("object"))
    name = str(params.get("collection") or "")
    if not name:
        raise H.fail(ErrorCode.PARAM_INVALID, "collection fehlt")
    coll = bpy.data.collections.get(name)
    if coll is None:
        if not params.get("create", True):
            raise H.fail(ErrorCode.NOT_FOUND, f"Collection '{name}' nicht gefunden")
        coll = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(coll)
    H.push_undo("In Collection verschieben")
    for obj in objs:
        for existing in list(obj.users_collection):
            existing.objects.unlink(obj)
        coll.objects.link(obj)
    return {"collection": coll.name, "objects": [o.name for o in objs]}


def object_parent(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    objs = H.as_object_list(params.get("objects") or params.get("object"))
    parent_name = params.get("parent")
    keep = bool(params.get("keep_transform", True))
    H.push_undo("Parenting")
    if not parent_name:
        for obj in objs:
            if obj.parent:
                world = obj.matrix_world.copy()
                obj.parent = None
                if keep:
                    obj.matrix_world = world
        return {"parent": None, "objects": [o.name for o in objs]}
    parent = H.get_object(parent_name)
    for obj in objs:
        if obj == parent:
            continue
        world = obj.matrix_world.copy()
        obj.parent = parent
        obj.matrix_parent_inverse = parent.matrix_world.inverted()
        if keep:
            obj.matrix_world = world
        H.add_history(obj, {"action": "parent", "parent": parent.name})
    bpy.context.view_layer.update()
    return {"parent": parent.name, "objects": [o.name for o in objs]}


def object_apply_transform(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    objs = H.as_object_list(params.get("objects") or params.get("object"))
    if not objs:
        raise H.fail(ErrorCode.BAD_REQUEST, "kein Objekt angegeben")
    H.select_only(objs)
    H.push_undo("Transform anwenden")
    bpy.ops.object.transform_apply(
        location=bool(params.get("location", False)),
        rotation=bool(params.get("rotation", True)),
        scale=bool(params.get("scale", True)),
        properties=False,
    )
    return {"applied": [o.name for o in objs],
            "note": "Skalierung/Rotation sind jetzt in der Geometrie - gut für Roblox-Export."}


def object_set_origin(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    objs = H.as_object_list(params.get("objects") or params.get("object"))
    mode = str(params.get("mode", "geometry")).lower()
    mapping = {
        "geometry": "ORIGIN_GEOMETRY",
        "cursor": "ORIGIN_CURSOR",
        "bbox_bottom": "ORIGIN_GEOMETRY",
        "bbox_center": "ORIGIN_GEOMETRY",
        "center_of_mass": "ORIGIN_CENTER_OF_MASS",
    }
    if mode not in mapping:
        raise H.fail(ErrorCode.PARAM_INVALID, f"unbekannter Modus: {mode}",
                     allowed=list(mapping))
    out = {}
    for obj in objs:
        H.select_only([obj], obj)
        H.push_undo("Ursprung setzen")
        if mode in ("bbox_bottom", "bbox_center"):
            # move the 3D cursor to the desired point of the bbox, then use it
            lo, hi = H.world_bbox(obj)
            if mode == "bbox_bottom":
                target = G.aabb_anchor((lo, hi), "bottom")
            else:
                target = G.aabb_center((lo, hi))
            bpy.context.scene.cursor.location = Vector(target)
            bpy.ops.object.origin_set(type="ORIGIN_CURSOR", center="MEDIAN")
        else:
            center = "MEDIAN" if mode != "center_of_mass" else "MEDIAN"
            bpy.ops.object.origin_set(type=mapping[mode], center=center)
        out[obj.name] = [round(c, 6) for c in obj.matrix_world.translation]
        H.add_history(obj, {"action": "set_origin", "mode": mode})
    return {"origins": out}


def object_add_modifier(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.active_or_named(params)
    kind = str(params.get("type", "")).lower()
    if kind not in MODIFIER_TYPES:
        raise H.fail(ErrorCode.PARAM_INVALID, f"unbekannter Modifier: {kind}",
                     allowed=sorted(MODIFIER_TYPES))
    H.push_undo("Modifier hinzufügen")
    mod = obj.modifiers.new(name=str(params.get("name") or kind.title()), type=kind.upper())
    options = params.get("options") or {}
    for key, value in options.items():
        if hasattr(mod, key):
            try:
                setattr(mod, key, value)
            except (TypeError, ValueError):
                pass
    if "target" in options:
        target = bpy.data.objects.get(str(options["target"]))
        if target is not None and hasattr(mod, "target"):
            mod.target = target
    H.add_history(obj, {"action": "add_modifier", "type": kind})
    return {"modifier": mod.name, "type": mod.type, "object": obj.name}


def object_modifier_action(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.active_or_named(params)
    name = params.get("modifier")
    action = str(params.get("action", "apply")).lower()
    mod = obj.modifiers.get(str(name)) if name else (obj.modifiers[-1] if obj.modifiers else None)
    if mod is None:
        raise H.fail(ErrorCode.NOT_FOUND, f"Modifier '{name}' nicht gefunden",
                     available=[m.name for m in obj.modifiers])
    H.push_undo(f"Modifier {action}")
    if action == "remove":
        obj.modifiers.remove(mod)
        return {"removed": name, "object": obj.name}
    if action == "apply":
        H.select_only([obj], obj)
        try:
            bpy.ops.object.modifier_apply(modifier=mod.name)
        except RuntimeError as exc:
            raise H.fail(ErrorCode.BLENDER_ERROR, f"Modifier konnte nicht angewendet werden: {exc}")
        return {"applied": name, "object": obj.name}
    if action in ("options", "set"):
        options = params.get("options") or {}
        for key, value in options.items():
            if hasattr(mod, key):
                try:
                    setattr(mod, key, value)
                except (TypeError, ValueError):
                    pass
        return {"updated": name, "options": options}
    raise H.fail(ErrorCode.PARAM_INVALID, "action muss apply|remove|set sein")


def object_add_constraint(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.active_or_named(params)
    kind = str(params.get("type", "")).upper()
    if kind not in CONSTRAINT_TYPES:
        raise H.fail(ErrorCode.PARAM_INVALID, f"unbekannter Constraint: {kind}",
                     allowed=sorted(CONSTRAINT_TYPES))
    H.push_undo("Constraint hinzufügen")
    con = obj.constraints.new(kind)
    target_name = params.get("target")
    if target_name:
        con.target = H.get_object(target_name)
    options = params.get("options") or {}
    for key, value in options.items():
        if hasattr(con, key):
            try:
                setattr(con, key, value)
            except (TypeError, ValueError):
                pass
    H.add_history(obj, {"action": "add_constraint", "type": kind,
                        "target": target_name})
    return {"constraint": con.name, "type": con.type,
            "target": getattr(con.target, "name", None), "object": obj.name}


def object_vertex_group(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_mesh_object(params.get("object"))
    group_name = str(params.get("group") or "bb_weights")
    mode = str(params.get("mode", "replace")).lower()
    vertices = params.get("vertices") or []
    weight = float(params.get("weight", 1.0))
    vg = obj.vertex_groups.get(group_name) or obj.vertex_groups.new(name=group_name)
    H.push_undo("Vertex-Gruppe")
    if mode == "remove":
        vg.remove(list(range(len(obj.data.vertices))))
        return {"group": group_name, "cleared": True}
    if isinstance(vertices, dict):
        for index, w in vertices.items():
            vg.add([int(index)], float(w), "REPLACE")
        return {"group": group_name, "assignments": len(vertices)}
    indices = [int(i) for i in vertices] if vertices else list(range(len(obj.data.vertices)))
    vg.add(indices, weight, "REPLACE" if mode == "replace" else "ADD")
    return {"group": group_name, "assigned": len(indices), "weight": weight}


def object_probe(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_object(params.get("object"))
    data = facts.object_facts(obj, include_parts=True, include_paths=True)
    part = params.get("part")
    if part:
        parts = data.get("parts") or {}
        if part not in parts:
            raise H.fail(ErrorCode.NOT_FOUND, f"Teil '{part}' nicht gefunden",
                         available=sorted(parts))
        data = {**data, **parts[part], "parent_object": obj.name}
    return data


TOOLS = {
    "object.list": object_list,
    "object.get": object_get,
    "object.create_primitive": object_create_primitive,
    "object.transform": object_transform,
    "object.duplicate": object_duplicate,
    "object.delete": object_delete,
    "object.rename": object_rename,
    "object.move_to_collection": object_move_to_collection,
    "object.parent": object_parent,
    "object.apply_transform": object_apply_transform,
    "object.set_origin": object_set_origin,
    "object.add_modifier": object_add_modifier,
    "object.modifier_action": object_modifier_action,
    "object.add_constraint": object_add_constraint,
    "object.vertex_group": object_vertex_group,
    "object.probe": object_probe,
}

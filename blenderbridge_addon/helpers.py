"""Blender-side helpers: object lookup, mesh access, regions, undo, safety.

Everything in this module must be called from Blender's main thread (the bridge
server arranges that).  The functions are deliberately small and defensive: a
tool must fail with a clear message instead of half-modifying the scene.
"""

from __future__ import annotations

import json
import math
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import bmesh
import bpy
from mathutils import Matrix, Vector

from .shared import geometry as G
from .shared import regions as RG
from .shared.protocol import ErrorCode, ProtocolError

ADDON_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def fail(code: str, message: str, **details: Any) -> ProtocolError:
    return ProtocolError(code, message, details)


def short_error(exc: BaseException, limit: int = 240) -> str:
    """One-line error text without the Python traceback noise."""
    text = f"{type(exc).__name__}: {exc}" if not isinstance(exc, str) else str(exc)
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def require(condition: Any, code: str, message: str, **details: Any) -> None:
    if not condition:
        raise fail(code, message, **details)


# ---------------------------------------------------------------------------
# Objects
# ---------------------------------------------------------------------------


def get_object(name: Any) -> "bpy.types.Object":
    if not isinstance(name, str) or not name:
        raise fail(ErrorCode.BAD_REQUEST, "Parameter 'object' fehlt oder ist kein Name")
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise fail(ErrorCode.OBJECT_NOT_FOUND, f"Objekt '{name}' nicht gefunden",
                   available=[o.name for o in bpy.data.objects][:60])
    return obj


def as_object_list(value: Any) -> List["bpy.types.Object"]:
    """Accept a name, a list of names, or objects."""
    if value is None:
        return []
    if isinstance(value, str):
        names = [value]
    elif isinstance(value, (list, tuple)):
        names = []
        for item in value:
            if isinstance(item, str):
                names.append(item)
            elif hasattr(item, "name"):
                names.append(item.name)
    else:
        raise fail(ErrorCode.BAD_REQUEST, "'object(s)' muss Name oder Liste sein")
    return [get_object(n) for n in names]


def get_mesh_object(name: Any) -> "bpy.types.Object":
    obj = get_object(name)
    if obj.type != "MESH":
        raise fail(ErrorCode.MESH_NOT_FOUND,
                   f"Objekt '{obj.name}' ist kein Mesh (Typ {obj.type})")
    return obj


def active_or_named(params: Dict[str, Any], key: str = "object") -> "bpy.types.Object":
    """Use the named object, or the active object when none is given."""
    name = params.get(key)
    if name:
        return get_object(name)
    obj = bpy.context.view_layer.objects.active or bpy.context.object
    if obj is None:
        raise fail(ErrorCode.BAD_REQUEST,
                   "kein Objekt angegeben und nichts in Blender aktiv")
    return obj


# ---------------------------------------------------------------------------
# Transforms / bounding boxes
# ---------------------------------------------------------------------------


def evaluated_mesh(obj: "bpy.types.Object"):
    """Mesh with modifiers applied (read only).  Falls back to the raw mesh."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    try:
        eval_obj = obj.evaluated_get(depsgraph)
        return eval_obj.to_mesh(), eval_obj
    except Exception:  # noqa: BLE001 - e.g. object without geometry
        return obj.data, obj


def free_evaluated(obj_eval, mesh) -> None:
    try:
        if mesh is not obj_eval.data:
            obj_eval.to_mesh_clear()
    except Exception:  # noqa: BLE001
        pass


def world_bbox(obj: "bpy.types.Object") -> Tuple[G.Vec3, G.Vec3]:
    """Axis aligned bounding box of the evaluated object in world space."""
    if obj.type == "MESH":
        mesh, eval_obj = evaluated_mesh(obj)
        try:
            mw = eval_obj.matrix_world
            pts = [mw @ v.co for v in mesh.vertices]
        finally:
            free_evaluated(eval_obj, mesh)
        if pts:
            lo, hi = G.aabb(pts)
            return tuple(lo), tuple(hi)  # type: ignore[return-value]
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    lo, hi = G.aabb([tuple(c) for c in corners])
    return lo, hi


def world_matrix(obj: "bpy.types.Object") -> Matrix:
    return obj.matrix_world.copy()


def axes_world(obj: "bpy.types.Object") -> Dict[str, List[float]]:
    m = obj.matrix_world.to_3x3()
    return {
        "x": list(G.vnorm(tuple(m.col[0]))),
        "y": list(G.vnorm(tuple(m.col[1]))),
        "z": list(G.vnorm(tuple(m.col[2]))),
    }


def dimensions(obj: "bpy.types.Object") -> G.Vec3:
    lo, hi = world_bbox(obj)
    return G.aabb_dims((lo, hi))


def rotation_deg(obj: "bpy.types.Object") -> G.Vec3:
    return tuple(math.degrees(a) for a in obj.matrix_world.to_euler("XYZ"))  # type: ignore[return-value]


def mesh_vertices_world(obj: "bpy.types.Object") -> List[G.Vec3]:
    mw = obj.matrix_world
    return [tuple(mw @ v.co) for v in obj.data.vertices]


def local_positions(obj: "bpy.types.Object") -> List[G.Vec3]:
    return [tuple(v.co) for v in obj.data.vertices]


# ---------------------------------------------------------------------------
# Undo + selection
# ---------------------------------------------------------------------------


def push_undo(label: str) -> None:
    """Make the next user Ctrl+Z undo the whole tool call.

    Never rely on this for correctness, but it is the cheapest way to give the
    user an escape hatch for every mutation.
    """
    try:
        bpy.ops.ed.undo_push(message=f"BlenderBridge: {label}")
    except Exception:  # noqa: BLE001 - undo is best effort
        pass


def deselect_all() -> None:
    for obj in bpy.context.view_layer.objects:
        try:
            obj.select_set(False)
        except RuntimeError:
            continue


def select_only(objs: Iterable["bpy.types.Object"], active: Optional["bpy.types.Object"] = None) -> None:
    if bpy.context.mode != "OBJECT":
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except RuntimeError:
            pass
    deselect_all()
    first = None
    for obj in objs:
        try:
            obj.select_set(True)
            first = first or obj
        except RuntimeError:
            continue
    target = active or first
    if target is not None:
        bpy.context.view_layer.objects.active = target


def ensure_object_mode() -> None:
    if bpy.context.mode != "OBJECT":
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# Object metadata: regions, anchors, paths, tags
# ---------------------------------------------------------------------------


def _read_json(obj: "bpy.types.Object", key: str) -> Dict[str, Any]:
    raw = obj.get(key)
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        data = json.loads(str(raw))
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _write_json(obj: "bpy.types.Object", key: str, value: Dict[str, Any]) -> None:
    obj[key] = json.dumps(value, separators=(",", ":"))


def region_doc(obj: "bpy.types.Object") -> Dict[str, Any]:
    doc = _read_json(obj, RG.REGION_PROP)
    doc.setdefault("format", RG.REGION_FORMAT)
    doc.setdefault("regions", {})
    return doc


def save_region_doc(obj: "bpy.types.Object", doc: Dict[str, Any]) -> None:
    _write_json(obj, RG.REGION_PROP, doc)


def load_region(obj: "bpy.types.Object", name: str) -> RG.Region:
    doc = region_doc(obj)
    raw = (doc.get("regions") or {}).get(name)
    if raw is None:
        raise fail(ErrorCode.REGION_NOT_FOUND, f"Bereich '{name}' existiert auf '{obj.name}' nicht",
                   available=sorted((doc.get("regions") or {}).keys()))
    return RG.region_from_dict(raw)


def topology_fingerprint(obj: "bpy.types.Object") -> str:
    return RG.fingerprint(len(obj.data.vertices), len(obj.data.polygons),
                          [tuple(e.vertices) for e in obj.data.edges])


def refresh_region_state(obj: "bpy.types.Object") -> Dict[str, str]:
    """Check all stored regions against the current mesh, updating bounds."""
    doc = region_doc(obj)
    state: Dict[str, str] = {}
    changed = False
    for name, raw in list((doc.get("regions") or {}).items()):
        region = RG.region_from_dict(raw)
        status = RG.region_state(region, len(obj.data.vertices), len(obj.data.polygons),
                                 [tuple(e.vertices) for e in obj.data.edges])
        state[name] = status
        if status == "ok":
            new_bounds = RG.region_bounds(local_positions(obj), region.vertices)
            if new_bounds and new_bounds != region.bounds:
                region.bounds = new_bounds
                doc["regions"][name]["bounds"] = new_bounds
                changed = True
    if changed:
        save_region_doc(obj, doc)
    return state


def all_regions(obj: "bpy.types.Object") -> Dict[str, Dict[str, Any]]:
    return dict(region_doc(obj).get("regions") or {})


def store_region(obj: "bpy.types.Object", region: RG.Region) -> Dict[str, Any]:
    """Persist a region and mirror its weights into a vertex group."""
    doc = region_doc(obj)
    region.fingerprint = RG.fingerprint(len(obj.data.vertices), len(obj.data.polygons),
                                        [tuple(e.vertices) for e in obj.data.edges])
    region.vertex_count = len(obj.data.vertices)
    region.poly_count = len(obj.data.polygons)
    region.bounds = RG.region_bounds(local_positions(obj), region.vertices)
    if not region.created_at:
        region.created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    doc["regions"][region.name] = RG.region_to_dict(region)
    save_region_doc(obj, doc)
    sync_vertex_group(obj, region)
    return RG.region_to_dict(region)


def sync_vertex_group(obj: "bpy.types.Object", region: RG.Region) -> None:
    group_name = RG.REGION_PREFIX + region.name
    vg = obj.vertex_groups.get(group_name)
    if vg is None:
        vg = obj.vertex_groups.new(name=group_name)
    else:
        vg.remove(list(range(len(obj.data.vertices))))
    # group equal weights to keep the number of calls low
    buckets: Dict[float, List[int]] = {}
    for index, weight in zip(region.vertices, region.weights):
        buckets.setdefault(round(weight, 3), []).append(index)
    for weight, indices in buckets.items():
        if weight <= 0.0:
            continue
        vg.add(indices, float(weight), "REPLACE")


def delete_region(obj: "bpy.types.Object", name: str) -> bool:
    doc = region_doc(obj)
    if name not in (doc.get("regions") or {}):
        return False
    del doc["regions"][name]
    save_region_doc(obj, doc)
    vg = obj.vertex_groups.get(RG.REGION_PREFIX + name)
    if vg is not None:
        obj.vertex_groups.remove(vg)
    return True


def anchor_doc(obj: "bpy.types.Object") -> Dict[str, Any]:
    return _read_json(obj, RG.ANCHOR_PROP)


def path_doc(obj: "bpy.types.Object") -> Dict[str, Any]:
    return _read_json(obj, RG.PATH_PROP)


def meta_doc(obj: "bpy.types.Object") -> Dict[str, Any]:
    """Free-form project memory: parts, params, style, notes, history."""
    meta = _read_json(obj, "bb_meta")
    meta.setdefault("parts", {})
    meta.setdefault("params", {})
    meta.setdefault("style", {})
    meta.setdefault("notes", [])
    meta.setdefault("history", [])
    return meta


def save_meta(obj: "bpy.types.Object", meta: Dict[str, Any]) -> None:
    # keep the history short but useful
    hist = list(meta.get("history") or [])
    meta["history"] = hist[-50:]
    _write_json(obj, "bb_meta", meta)


def add_history(obj: "bpy.types.Object", entry: Dict[str, Any]) -> None:
    meta = meta_doc(obj)
    hist = list(meta.get("history") or [])
    entry = dict(entry)
    entry.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    hist.append(entry)
    meta["history"] = hist[-50:]
    save_meta(obj, meta)


def stable_id(obj: "bpy.types.Object") -> str:
    """A stable identifier that survives renames (stored once in custom props)."""
    existing = obj.get("bb_id")
    if isinstance(existing, str) and existing:
        return existing
    value = f"bb-{int(time.time() * 1000):x}-{abs(hash(obj.name)) & 0xFFFF:04x}"
    obj["bb_id"] = value
    return value


# ---------------------------------------------------------------------------
# Regions: resolve helper for tool parameters
# ---------------------------------------------------------------------------


def resolve_weights(obj: "bpy.types.Object", region_specs: Any, *,
                    vertex_count: Optional[int] = None) -> Tuple[List[float], List[str], List[str]]:
    """Build a dense weight array from one or more region references.

    ``region_specs`` may be a region name, a list of names, or an inline spec
    such as ``{"name": "cheek", "soft": 0.05, "source": "selection"}``.
    Returns ``(weights, used_region_names, warnings)``.
    """
    n = vertex_count if vertex_count is not None else len(obj.data.vertices)
    weights = [0.0] * n
    used: List[str] = []
    warnings: List[str] = []
    if not region_specs:
        return weights, used, warnings

    specs = region_specs if isinstance(region_specs, list) else [region_specs]
    for spec in specs:
        if isinstance(spec, str):
            region = load_region(obj, spec)
        elif isinstance(spec, dict):
            if spec.get("vertices"):
                region = RG.Region(name=spec.get("name", "inline"), object_name=obj.name,
                                   vertices=[int(i) for i in spec["vertices"]],
                                   weights=[float(w) for w in spec.get("weights", [])] or
                                           [1.0] * len(spec["vertices"]))
            else:
                region = build_region(obj, spec)
        else:
            raise fail(ErrorCode.PARAM_INVALID, "region muss Name oder Objekt sein")
        used.append(region.name)
        for index, weight in zip(region.vertices, region.weights):
            if 0 <= index < n:
                weights[index] = max(weights[index], weight)
    strong = sum(1 for w in weights if w > 0)
    if strong == 0:
        warnings.append("Der Bereich enthält keine Vertices - es wurde nichts verändert.")
    return weights, used, warnings


def build_region(obj: "bpy.types.Object", spec: Dict[str, Any]) -> RG.Region:
    """Create a region in memory from a probe description (not persisted)."""
    source = str(spec.get("source", "box")).lower()
    soft = float(spec.get("soft", 0.0) or 0.0)
    profile = str(spec.get("profile", "smooth"))
    local = local_positions(obj)
    name = str(spec.get("name", source))
    if source in ("selection", "selected"):
        indices = selected_vertex_indices(obj)
        idx, w = RG.region_from_indices(local, indices, soft, profile)
    elif source == "box":
        spec_box = spec.get("box") or [spec.get("min"), spec.get("max")]
        require(len(spec_box) == 2 and spec_box[0] and spec_box[1],
                ErrorCode.PARAM_INVALID, "box braucht min und max")
        lo = world_to_local(obj, spec_box[0])
        hi = world_to_local(obj, spec_box[1])
        idx, w = RG.region_from_box(local, [min(lo[i], hi[i]) for i in range(3)],
                                    [max(lo[i], hi[i]) for i in range(3)], soft, profile)
    elif source == "sphere":
        center = world_to_local(obj, spec.get("center") or [0, 0, 0])
        radius = float(spec.get("radius", 0.5))
        idx, w = RG.region_from_sphere(local, center, radius, soft, profile)
    elif source in ("axis", "band"):
        start = world_to_local(obj, spec.get("start") or [0, 0, 0])
        end = world_to_local(obj, spec.get("end") or [0, 0, 1])
        radius = float(spec.get("radius", 0.3))
        t_range = spec.get("t_range") or (0.0, 1.0)
        idx, w = RG.region_from_axis_band(local, start, end, radius, tuple(t_range), soft, profile)
    elif source in ("height", "slice"):
        axis = spec.get("axis") or [0, 0, 1]
        t_range = spec.get("range") or spec.get("t_range") or (0.0, 1.0)
        idx, w = RG.region_by_height(local, axis, tuple(t_range), soft, profile)
    elif source == "material":
        slot = spec.get("material")
        indices = material_vertex_indices(obj, slot)
        idx, w = RG.region_from_indices(local, indices, soft, profile)
    elif source == "islands":
        islands = mesh_islands(obj)
        want = spec.get("island")
        if want is None:
            raise fail(ErrorCode.PARAM_INVALID, "islands braucht 'island' oder 'island_index'")
        indices = islands[int(want)] if int(want) < len(islands) else []
        idx, w = RG.region_from_indices(local, indices, soft, profile)
    elif source == "loose_part":
        islands = mesh_islands(obj)
        point = world_to_local(obj, spec.get("point") or spec.get("center") or [0, 0, 0])
        best, best_d = -1, float("inf")
        for i, verts in enumerate(islands):
            for vi in verts[:200]:
                d = G.vlen(G.vsub(local[vi], point))
                if d < best_d:
                    best, best_d = i, d
        indices = islands[best] if best >= 0 else []
        idx, w = RG.region_from_indices(local, indices, soft, profile)
    else:
        raise fail(ErrorCode.PARAM_INVALID, f"unbekannte Region-Quelle: {source}",
                   allowed=["selection", "box", "sphere", "axis", "height",
                            "material", "islands", "loose_part"])
    region = RG.Region(name=name, object_name=obj.name, vertices=idx, weights=w,
                       source=source, kind=str(spec.get("kind", "region")),
                       protected=bool(spec.get("protected", False)),
                       note=str(spec.get("note", "")))
    require(region.vertices, ErrorCode.NO_GEOMETRY,
            f"Region '{name}' ist leer - Auswahl/Box prüfen", source=source)
    return region


def world_to_local(obj: "bpy.types.Object", point: Sequence[float]) -> G.Vec3:
    p = obj.matrix_world.inverted() @ Vector(tuple(point))
    return (p.x, p.y, p.z)


def local_to_world(obj: "bpy.types.Object", point: Sequence[float]) -> G.Vec3:
    p = obj.matrix_world @ Vector(tuple(point))
    return (p.x, p.y, p.z)


def direction_to_local(obj: "bpy.types.Object", direction: Sequence[float]) -> G.Vec3:
    d = obj.matrix_world.to_3x3().inverted() @ Vector(tuple(direction))
    return (d.x, d.y, d.z)


# ---------------------------------------------------------------------------
# Mesh analysis
# ---------------------------------------------------------------------------


def mesh_edges(obj: "bpy.types.Object") -> List[List[int]]:
    return [[e.vertices[0], e.vertices[1]] for e in obj.data.edges]


def vertex_normals(obj: "bpy.types.Object") -> List[G.Vec3]:
    mesh = obj.data
    mesh.calc_normals_split() if hasattr(mesh, "calc_normals_split") else None
    out: List[G.Vec3] = []
    for v in mesh.vertices:
        out.append(tuple(v.normal))
    return out


def selected_vertex_indices(obj: "bpy.types.Object") -> List[int]:
    mesh = obj.data
    mode = bpy.context.mode
    if mode in ("EDIT_MESH", "EDIT") and obj.mode == "EDIT":
        bm = bmesh.from_edit_mesh(mesh)
        bm.verts.ensure_lookup_table()
        return [v.index for v in bm.verts if v.select]
    return [v.index for v in mesh.vertices if v.select]


def selected_objects() -> List["bpy.types.Object"]:
    return [o for o in bpy.context.selected_objects]


def mesh_islands(obj: "bpy.types.Object") -> List[List[int]]:
    """Vertex indices grouped by connected component."""
    n = len(obj.data.vertices)
    adjacency: List[List[int]] = [[] for _ in range(n)]
    for e in obj.data.edges:
        a, b = e.vertices
        adjacency[a].append(b)
        adjacency[b].append(a)
    seen = [False] * n
    islands: List[List[int]] = []
    for start in range(n):
        if seen[start]:
            continue
        stack = [start]
        seen[start] = True
        component = []
        while stack:
            v = stack.pop()
            component.append(v)
            for nb in adjacency[v]:
                if not seen[nb]:
                    seen[nb] = True
                    stack.append(nb)
        islands.append(sorted(component))
    return islands


def material_vertex_indices(obj: "bpy.types.Object", material: Any) -> List[int]:
    mesh = obj.data
    wanted_slots = set()
    if material is None:
        return []
    if isinstance(material, int):
        wanted_slots.add(material)
    else:
        for i, slot in enumerate(obj.material_slots):
            if slot.material and (slot.material.name == material or str(i) == str(material)):
                wanted_slots.add(i)
    verts = set()
    for poly in mesh.polygons:
        if poly.material_index in wanted_slots:
            verts.update(poly.vertices)
    return sorted(verts)


def poly_count(obj: "bpy.types.Object") -> int:
    return len(obj.data.polygons)


def tri_count(obj: "bpy.types.Object") -> int:
    total = 0
    for poly in obj.data.polygons:
        total += max(1, len(poly.vertices) - 2)
    return total


def has_uv(obj: "bpy.types.Object") -> bool:
    return bool(obj.data.uv_layers)


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


def artifact_dir(meta: Dict[str, Any]) -> str:
    """The connector tells us where files may be written; enforce it."""
    directory = str(meta.get("artifact_dir") or "")
    if not directory:
        directory = os.path.join(os.path.expanduser("~"), ".blenderbridge_artifacts")
    os.makedirs(directory, exist_ok=True)
    return directory


def safe_join(directory: str, name: str) -> str:
    base = os.path.realpath(directory)
    target = os.path.realpath(os.path.join(base, os.path.basename(name)))
    if os.path.dirname(target) != base:
        raise fail(ErrorCode.FORBIDDEN, "ungültiger Dateiname")
    return target


def scene_snapshot_dir() -> str:
    """Where snapshots live: a folder next to the .blend if it is saved."""
    blend = bpy.data.filepath
    if blend:
        base = os.path.join(os.path.dirname(blend), "blenderbridge_backups")
    else:
        base = os.path.join(os.path.expanduser("~"), "blenderbridge_backups")
    os.makedirs(base, exist_ok=True)
    return base

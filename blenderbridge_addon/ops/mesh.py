"""mesh.* tools: inspect, deform regions, extrude, subdivide, join, UVs.

The heavy geometry maths lives in ``shared/regions.py`` (pure Python, unit
tested).  This module only shuttles data between bmesh/mesh and those functions,
which keeps the risky part small.
"""

from __future__ import annotations

import math
import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

import bmesh
import bpy
from mathutils import Matrix, Vector

from .. import facts
from .. import helpers as H
from ..shared import geometry as G
from ..shared import regions as RG
from ..shared.protocol import ErrorCode

DEFORM_OPS = ("move", "inflate", "scale", "taper", "bend", "smooth", "flatten",
              "noise", "stretch", "pinch")


def _write_positions(obj: "bpy.types.Object", positions: Sequence[Sequence[float]]) -> None:
    mesh = obj.data
    if len(positions) != len(mesh.vertices):
        raise H.fail(ErrorCode.INTERNAL, "Positionsanzahl passt nicht zum Mesh")
    for i, co in enumerate(positions):
        mesh.vertices[i].co = Vector(co)
    mesh.update()


def _protected_mask(obj: "bpy.types.Object", params: Dict[str, Any]) -> List[int]:
    """Vertex indices that must never move."""
    blocked: List[int] = []
    for name, raw in H.all_regions(obj).items():
        if raw.get("protected"):
            blocked.extend(int(i) for i in raw.get("vertices", []))
    specs = params.get("protect")
    if specs:
        weights, _, _ = H.resolve_weights(obj, specs)
        blocked.extend(i for i, w in enumerate(weights) if w > 0.01)
    return sorted(set(blocked))


def mesh_inspect(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_mesh_object(params.get("object"))
    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    try:
        non_manifold = [e.index for e in bm.edges if not e.is_manifold]
        boundary = [e.index for e in bm.edges if e.is_boundary]
        loose_verts = [v.index for v in bm.verts if not v.link_faces]
        ngons = [f.index for f in bm.faces if len(f.verts) > 4]
        degenerate = [f.index for f in bm.faces if f.calc_area() < 1e-9]
        normals_flipped = 0
        for f in bm.faces:
            if f.normal.length > 0 and f.calc_center_median().length > 0:
                if f.normal.dot(f.calc_center_median().normalized()) < -0.999:
                    normals_flipped += 1
        islands = H.mesh_islands(obj)
        result = {
            "object": obj.name,
            "vertices": len(mesh.vertices),
            "edges": len(mesh.edges),
            "polygons": len(mesh.polygons),
            "triangles": H.tri_count(obj),
            "islands": len(islands),
            "island_sizes": [len(i) for i in sorted(islands, key=len, reverse=True)[:12]],
            "non_manifold_edges": len(non_manifold),
            "boundary_edges": len(boundary),
            "loose_vertices": len(loose_verts),
            "ngons": len(ngons),
            "degenerate_faces": len(degenerate),
            "suspicious_normals": normals_flipped,
            "uv_layers": [l.name for l in mesh.uv_layers],
            "materials": [slot.material.name if slot.material else None
                          for slot in obj.material_slots],
            "has_custom_normals": bool(getattr(mesh, "has_custom_normals", False)),
            "shape_keys": len(mesh.shape_keys.key_blocks) - 1 if mesh.shape_keys else 0,
            "dimensions": [round(c, 6) for c in H.dimensions(obj)],
            "regions": {name: len(r.get("vertices", []))
                        for name, r in H.all_regions(obj).items()},
        }
    finally:
        bm.free()
    return result


def mesh_points(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_mesh_object(params.get("object"))
    limit = max(1, min(int(params.get("limit", 2000)), 20000))
    offset = max(0, int(params.get("offset", 0)))
    positions = H.local_positions(obj)
    indices = list(range(len(positions)))
    region = params.get("region")
    if region:
        region_obj = H.load_region(obj, str(region))
        indices = region_obj.vertices
    elif params.get("bbox"):
        box = params["bbox"]
        lo = H.world_to_local(obj, box[0])
        hi = H.world_to_local(obj, box[1])
        lo = [min(lo[i], hi[i]) for i in range(3)]
        hi = [max(lo[i], hi[i]) for i in range(3)]
        indices = [i for i in indices
                   if all(lo[k] <= positions[i][k] <= hi[k] for k in range(3))]
    total = len(indices)
    chunk = indices[offset: offset + limit]
    mw = obj.matrix_world
    return {
        "object": obj.name,
        "total": total,
        "offset": offset,
        "returned": len(chunk),
        "points": [
            {"index": i,
             "local": [round(c, 6) for c in positions[i]],
             "world": [round(c, 6) for c in (mw @ Vector(positions[i]))]}
            for i in chunk
        ],
        "note": "local = Objektkoordinaten, world = Szenenkoordinaten (Meter)",
    }


def mesh_edit(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_mesh_object(params.get("object"))
    op = str(params.get("op", "")).lower()
    if op not in DEFORM_OPS:
        raise H.fail(ErrorCode.PARAM_INVALID, f"unbekannte Operation: {op}",
                     allowed=list(DEFORM_OPS))

    weights, used, warnings = H.resolve_weights(obj, params.get("region") or params.get("regions"))
    if not any(w > 0 for w in weights):
        raise H.fail(ErrorCode.REGION_NOT_FOUND,
                     "Kein Bereich mit Einfluss gefunden. Erst region.create aufrufen "
                     "oder 'region' mitgeben.", used=used)

    blocked = _protected_mask(obj, params)
    protected_hit = sum(1 for i in blocked if weights[i] > 0)
    for i in blocked:
        weights[i] = 0.0
    if protected_hit:
        warnings.append(f"{protected_hit} geschützte Punkte wurden ausgenommen.")

    profile = str(params.get("profile", "smooth"))
    positions = H.local_positions(obj)
    normals = [tuple(v.normal) for v in obj.data.vertices]
    edges = [[e.vertices[0], e.vertices[1]] for e in obj.data.edges]
    amount = float(params.get("amount", 0.0) or 0.0)

    if op == "move":
        direction = params.get("direction") or [0, 0, 1]
        local_dir = H.direction_to_local(obj, direction)
        result = RG.displace(positions, weights, local_dir, amount)
    elif op == "inflate":
        result = RG.inflate(positions, weights, normals, amount)
    elif op == "scale":
        center = params.get("center")
        center_local = H.world_to_local(obj, center) if center else _weighted_center(positions, weights)
        factor = params.get("factor") or params.get("scale") or 1.05
        if isinstance(factor, (int, float)):
            factor = (float(factor),) * 3
        result = RG.scale_about(positions, weights, center_local, factor)
    elif op == "taper":
        axis = H.direction_to_local(obj, params.get("axis") or [0, 0, 1])
        origin = H.world_to_local(obj, params.get("origin") or list(H.world_bbox(obj)[0]))
        start = float(params.get("start_factor", 1.0))
        end = float(params.get("end_factor", 0.3))
        result = RG.taper(positions, weights, axis, origin, start, end,
                          str(params.get("curve", "linear")))
    elif op == "bend":
        axis = H.direction_to_local(obj, params.get("axis") or [0, 0, 1])
        origin = H.world_to_local(obj, params.get("origin") or list(H.world_bbox(obj)[0]))
        direction = H.direction_to_local(obj, params.get("direction") or [1, 0, 0])
        t_range = params.get("t_range") or (0.0, 1.0)
        result = RG.bend(positions, weights, axis, origin, float(params.get("angle_deg", 15.0)),
                         direction, tuple(t_range), str(params.get("curve", "smooth")))
    elif op == "smooth":
        factor = float(params.get("factor", 0.5))
        iterations = max(1, min(int(params.get("iterations", 2)), 20))
        result = RG.smooth(positions, weights, edges, factor, iterations)
    elif op == "flatten":
        normal = G.vnorm(H.direction_to_local(obj, params.get("normal") or [0, 0, 1]))
        point = H.world_to_local(obj, params.get("point") or list(G.aabb_center(H.world_bbox(obj))))
        strength = float(params.get("strength", 1.0))
        result = RG._apply(
            positions, weights,
            lambda p, w, i: G.vlerp(p, G.project_to_plane(p, point, normal), w * strength),
        )
    elif op == "noise":
        seed = int(params.get("seed", 0))
        rng = random.Random(seed)
        scale = float(params.get("scale", 0.01))
        result = RG._apply(
            positions, weights,
            lambda p, w, i: G.vadd(p, (rng.uniform(-1, 1) * scale * w,
                                       rng.uniform(-1, 1) * scale * w,
                                       rng.uniform(-1, 1) * scale * w)),
        )
    elif op == "stretch":
        axis = G.vnorm(H.direction_to_local(obj, params.get("axis") or [0, 0, 1]))
        center = _weighted_center(positions, weights)
        factor = float(params.get("factor", 1.2))

        def stretch(p, w, i):
            rel = G.vsub(p, center)
            along = G.vdot(rel, axis)
            return G.vlerp(p, G.vadd(p, G.vmul(axis, along * (factor - 1.0))), w)

        result = RG._apply(positions, weights, stretch)
    else:  # pinch
        center = _weighted_center(positions, weights)
        factor = float(params.get("factor", 0.8))
        result = RG.scale_about(positions, weights, center, (factor, factor, factor))

    if ctx.dry_run:
        return {
            "dry_run": True,
            "object": obj.name,
            "op": op,
            "would_move": result.moved,
            "regions": used,
            "estimated_max_displacement": round(result.max_displacement, 6),
            "warnings": warnings + result.warnings,
        }

    H.push_undo(f"mesh.edit {op}")
    _write_positions(obj, result.positions)
    obj.data.update()
    H.refresh_region_state(obj)
    H.add_history(obj, {"action": "mesh.edit", "op": op, "moved": result.moved,
                        "regions": used})
    bpy.context.view_layer.update()
    return {
        "object": obj.name,
        "op": op,
        "moved_vertices": result.moved,
        "max_displacement": round(result.max_displacement, 6),
        "regions": used,
        "protected_vertices": len(blocked),
        "warnings": warnings + result.warnings,
        "assumptions": _op_assumptions(op, params),
    }


def _op_assumptions(op: str, params: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    if op == "bend" and "t_range" not in params:
        out.append("Biegung startet am Fuß (t_range 0–1) und ist oben am stärksten.")
    if op == "taper" and "curve" not in params:
        out.append("Verjüngung verläuft linear entlang der Achse.")
    if op == "smooth" and "iterations" not in params:
        out.append("2 Glättungsdurchläufe, Stärke 0.5.")
    if op in ("inflate", "move") and "profile" not in params:
        out.append("Weicher Übergang (smoothstep) im Bereichsrand.")
    return out


def _weighted_center(positions: Sequence[Sequence[float]], weights: Sequence[float]) -> G.Vec3:
    sx = sy = sz = 0.0
    total = 0.0
    for p, w in zip(positions, weights):
        if w <= 0:
            continue
        sx += p[0] * w
        sy += p[1] * w
        sz += p[2] * w
        total += w
    if total <= 0:
        return G.centroid(positions)
    return (sx / total, sy / total, sz / total)


def mesh_extrude(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_mesh_object(params.get("object"))
    weights, used, warnings = H.resolve_weights(obj, params.get("region") or params.get("regions"))
    if not any(w > 0 for w in weights):
        raise H.fail(ErrorCode.REGION_NOT_FOUND, "Bereich für Extrusion fehlt", used=used)

    distance = float(params.get("distance", 0.2))
    taper = float(params.get("taper", 1.0))
    segments = max(1, min(int(params.get("segments", 1)), 8))

    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    try:
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        vertex_set = {i for i, w in enumerate(weights) if w >= 0.5}
        faces = [f for f in bm.faces if all(v.index in vertex_set for v in f.verts)]
        if not faces:
            raise H.fail(ErrorCode.NO_GEOMETRY,
                         "Im Bereich liegen keine vollständigen Flächen - "
                         "Bereich größer wählen oder mit 'soft' erweitern.",
                         region=used)
        if params.get("direction"):
            direction = Vector(G.vnorm(H.direction_to_local(obj, params["direction"])))
        else:
            normal_sum = Vector((0, 0, 0))
            for f in faces:
                normal_sum += f.normal * f.calc_area()
            direction = normal_sum.normalized() if normal_sum.length > 1e-9 else Vector((0, 0, 1))
            warnings.append("Extrusionsrichtung aus der mittleren Flächennormale abgeleitet.")

        created: List[int] = []
        for step in range(segments):
            ret = bmesh.ops.extrude_face_region(bm, geom=faces)
            geom = ret.get("geom", [])
            new_verts = [g for g in geom if isinstance(g, bmesh.types.BMVert)]
            new_faces = [g for g in geom if isinstance(g, bmesh.types.BMFace)]
            step_distance = distance / segments
            bmesh.ops.translate(bm, verts=new_verts, vec=direction * step_distance)
            if taper != 1.0 and new_verts:
                center = Vector((0, 0, 0))
                for v in new_verts:
                    center += v.co
                center /= len(new_verts)
                factor = taper ** (step + 1)
                bmesh.ops.scale(bm, vec=(factor, factor, factor), verts=new_verts,
                                space=Matrix.Translation(-center))
            created.extend(v.index for v in new_verts if v.index >= 0)
            faces = [f for f in new_faces if f.is_valid]
            if not faces:
                break
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        if not ctx.dry_run:
            bm.to_mesh(mesh)
    finally:
        bm.free()

    if ctx.dry_run:
        return {"dry_run": True, "would_extrude_faces": len(faces), "region": used}

    mesh.update()
    state = H.refresh_region_state(obj)
    H.push_undo("Extrusion")
    H.add_history(obj, {"action": "mesh.extrude", "region": used,
                        "distance": distance, "regions_after": state})
    bpy.context.view_layer.update()
    return {
        "object": obj.name,
        "region": used,
        "extruded": True,
        "distance": distance,
        "segments": segments,
        "warning": ("Durch die neue Topologie können gespeicherte Bereiche veralten. "
                    "region.check zeigt den Zustand." if any(v == "stale" for v in state.values())
                    else ""),
        "regions_after": state,
        "warnings": warnings,
    }


def mesh_subdivide(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_mesh_object(params.get("object"))
    cuts = max(1, min(int(params.get("cuts", 1)), 10))
    region = params.get("region")
    weights = None
    if region:
        weights, _, _ = H.resolve_weights(obj, region)
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    try:
        if weights is not None:
            verts = {i for i, w in enumerate(weights) if w > 0}
            edges = [e for e in bm.edges if e.verts[0].index in verts and e.verts[1].index in verts]
        else:
            edges = list(bm.edges)
        if not edges:
            raise H.fail(ErrorCode.NO_GEOMETRY, "keine Kanten zum Unterteilen gefunden")
        bmesh.ops.subdivide_edges(bm, edges=edges, cuts=cuts, use_grid_fill=True,
                                  smooth=float(params.get("smoothness", 0.0)))
        if not ctx.dry_run:
            bm.to_mesh(obj.data)
    finally:
        bm.free()
    if ctx.dry_run:
        return {"dry_run": True, "edges": len(edges)}
    obj.data.update()
    state = H.refresh_region_state(obj)
    H.push_undo("Unterteilen")
    H.add_history(obj, {"action": "mesh.subdivide", "cuts": cuts})
    stale = [k for k, v in state.items() if v != "ok"]
    return {"object": obj.name, "cuts": cuts,
            "vertices": len(obj.data.vertices), "polygons": len(obj.data.polygons),
            "stale_regions": stale,
            "warning": (f"{len(stale)} gespeicherte Bereiche passen nicht mehr zur neuen "
                        f"Topologie. Mit region.remap reparieren." if stale else "")}


def mesh_join(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    objs = H.as_object_list(params.get("objects"))
    if len(objs) < 2:
        raise H.fail(ErrorCode.PARAM_INVALID, "mindestens zwei Objekte angeben")
    if any(o.type != "MESH" for o in objs):
        raise H.fail(ErrorCode.MESH_NOT_FOUND, "nur Meshes können zusammengefügt werden")
    target = H.get_object(params["target"]) if params.get("target") else objs[0]
    H.select_only([target] + [o for o in objs if o != target], target)
    H.push_undo("Zusammenfügen")
    bpy.ops.object.join()
    if params.get("name"):
        target.name = str(params["name"])
    H.add_history(target, {"action": "mesh.join", "sources": [o.name for o in objs]})
    state = H.refresh_region_state(target)
    return {"object": target.name, "vertices": len(target.data.vertices),
            "regions_after": state,
            "warning": "Bereiche der zusammengefügten Objekte müssen neu geprüft werden."}


def mesh_separate(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_mesh_object(params.get("object"))
    mode = str(params.get("mode", "islands")).lower()
    H.ensure_object_mode()
    H.select_only([obj], obj)
    before = {o.name for o in bpy.data.objects}
    H.push_undo("Trennen")
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        bpy.ops.mesh.select_all(action="DESELECT")
        if mode == "islands":
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.mesh.separate(type="LOOSE")
        elif mode == "material":
            bpy.ops.mesh.separate(type="MATERIAL")
        elif mode in ("region", "selection"):
            region = params.get("region")
            if not region:
                raise H.fail(ErrorCode.PARAM_INVALID, "region fehlt")
            weights, _, _ = H.resolve_weights(obj, region)
            bm = bmesh.from_edit_mesh(obj.data)
            bm.verts.ensure_lookup_table()
            for v in bm.verts:
                v.select = weights[v.index] > 0.5
            for e in bm.edges:
                e.select = all(v.select for v in e.verts)
            for f in bm.faces:
                f.select = all(v.select for v in f.verts)
            bmesh.update_edit_mesh(obj.data)
            bpy.ops.mesh.separate(type="SELECTED")
        else:
            raise H.fail(ErrorCode.PARAM_INVALID, "mode muss islands|material|region sein")
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    created = sorted({o.name for o in bpy.data.objects} - before)
    return {"source": obj.name, "created": created}


def mesh_merge_by_distance(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_mesh_object(params.get("object"))
    distance = float(params.get("distance", 0.0001))
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    try:
        before = len(bm.verts)
        bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=distance)
        after = len(bm.verts)
        if not ctx.dry_run:
            bm.to_mesh(obj.data)
    finally:
        bm.free()
    obj.data.update()
    H.push_undo("Doppelte Punkte verschmelzen")
    state = H.refresh_region_state(obj)
    return {"object": obj.name, "before": before, "after": after,
            "removed": before - after, "regions_after": state,
            "warning": "Zusammengeführte Punkte können Bereiche verschieben - neu prüfen."}


def mesh_shade(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_mesh_object(params.get("object"))
    mode = str(params.get("mode", "smooth")).lower()
    if mode not in ("smooth", "flat", "auto"):
        raise H.fail(ErrorCode.PARAM_INVALID, "mode muss smooth|flat|auto sein")
    H.push_undo("Shading")
    mesh = obj.data
    smooth = mode != "flat"
    for poly in mesh.polygons:
        poly.use_smooth = smooth
    angle = float(params.get("angle", 40.0))
    note = ""
    if mode == "auto":
        if hasattr(mesh, "use_auto_smooth"):  # Blender <= 4.0
            mesh.use_auto_smooth = True
            mesh.auto_smooth_angle = math.radians(angle)
        else:  # Blender >= 4.1: operator adds the "Smooth by Angle" modifier
            H.select_only([obj], obj)
            try:
                bpy.ops.object.shade_smooth_by_angle(angle=math.radians(angle))
                note = "Smooth by Angle (Modifier) hinzugefügt"
            except (AttributeError, RuntimeError):
                note = "Auto-Smooth in dieser Blender-Version nicht verfügbar; smooth verwendet"
    mesh.update()
    return {"object": obj.name, "mode": mode, "note": note}


def mesh_uv(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_mesh_object(params.get("object"))
    mode = str(params.get("mode", "box")).lower()
    if mode not in ("box", "smart", "unwrap", "keep"):
        raise H.fail(ErrorCode.PARAM_INVALID, "mode muss box|smart|unwrap|keep sein")
    if mode == "keep":
        return {"object": obj.name, "uv_layers": [l.name for l in obj.data.uv_layers]}
    H.push_undo("UV erzeugen")
    note = ""
    if mode in ("smart", "unwrap"):
        H.select_only([obj], obj)
        try:
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")
            if mode == "smart":
                bpy.ops.uv.smart_project(angle_limit=math.radians(float(params.get("angle_limit", 66))),
                                         island_margin=float(params.get("margin", 0.02)))
            else:
                bpy.ops.uv.unwrap(method="ANGLE_BASED",
                                  margin=float(params.get("margin", 0.02)))
            bpy.ops.object.mode_set(mode="OBJECT")
            return {"object": obj.name, "mode": mode,
                    "uv_layers": [l.name for l in obj.data.uv_layers]}
        except (RuntimeError, AttributeError) as exc:
            if bpy.context.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            note = f"{mode} im Hintergrundmodus nicht möglich ({exc}); Box-Projektion verwendet."
    # deterministic box projection, works headless
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    try:
        uv_layer = bm.loops.layers.uv.verify()
        for face in bm.faces:
            normal = face.normal
            axis = max(range(3), key=lambda i: abs(normal[i]))
            for loop in face.loops:
                co = loop.vert.co
                if axis == 0:
                    u, v = co.y, co.z
                elif axis == 1:
                    u, v = co.x, co.z
                else:
                    u, v = co.x, co.y
                loop[uv_layer].uv = (u, v)
        bm.to_mesh(obj.data)
    finally:
        bm.free()
    obj.data.update()
    return {"object": obj.name, "mode": "box", "note": note,
            "uv_layers": [l.name for l in obj.data.uv_layers]}


def mesh_validate(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    obj = H.get_mesh_object(params.get("object"))
    info = mesh_inspect({"object": obj.name}, ctx)
    issues: List[str] = []
    if info["non_manifold_edges"]:
        issues.append(f"{info['non_manifold_edges']} nicht-mannigfaltige Kanten")
    if info["loose_vertices"]:
        issues.append(f"{info['loose_vertices']} lose Punkte ohne Fläche")
    if info["degenerate_faces"]:
        issues.append(f"{info['degenerate_faces']} Flächen ohne Fläche (degeneriert)")
    if not info["uv_layers"]:
        issues.append("keine UV-Map (für Roblox-Texturen meist nötig)")
    scale = [round(abs(c), 4) for c in obj.scale]
    if any(abs(s - 1.0) > 1e-3 for s in scale):
        issues.append(f"Skalierung nicht angewendet {scale} - vor dem Export anwenden")
    return {"object": obj.name, "ok": not issues, "issues": issues, "details": info}


TOOLS = {
    "mesh.inspect": mesh_inspect,
    "mesh.points": mesh_points,
    "mesh.edit": mesh_edit,
    "mesh.extrude": mesh_extrude,
    "mesh.subdivide": mesh_subdivide,
    "mesh.join": mesh_join,
    "mesh.separate": mesh_separate,
    "mesh.merge_by_distance": mesh_merge_by_distance,
    "mesh.shade": mesh_shade,
    "mesh.uv": mesh_uv,
    "mesh.validate": mesh_validate,
}

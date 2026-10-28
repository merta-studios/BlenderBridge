"""check.*: technical and Roblox-oriented review of the current model."""

from __future__ import annotations

from typing import Any, Dict, List

import bpy

from .. import facts
from .. import helpers as H
from ..shared import geometry as G
from ..shared.protocol import ErrorCode

#: Rough guidance, not a hard limit enforced by Roblox itself.
ROBLOX_BUDGETS = {
    "small_prop": 1500,
    "character_part": 4000,
    "building": 12000,
    "hero_asset": 30000,
    "scene_landmark": 60000,
}


def check_technical(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    from . import mesh as mesh_ops

    names = params.get("objects") or params.get("object")
    objs = H.as_object_list(names) or [o for o in bpy.context.scene.objects if o.type == "MESH"]
    budget = params.get("budget")
    report = []
    total_tris = 0
    for obj in objs:
        if obj.type != "MESH":
            report.append({"object": obj.name, "type": obj.type, "skipped": True})
            continue
        info = mesh_ops.mesh_inspect({"object": obj.name}, ctx)
        issues: List[str] = []
        if info["non_manifold_edges"]:
            issues.append(f"{info['non_manifold_edges']} nicht-mannigfaltige Kanten "
                          f"(können beim Export Löcher erzeugen)")
        if info["degenerate_faces"]:
            issues.append(f"{info['degenerate_faces']} degenerierte Flächen")
        if info["ngons"]:
            issues.append(f"{info['ngons']} N-Gons (Roblox trianguliert selbst - "
                          f"bei sichtbaren Fehlern vorher triangulieren)")
        if not info["uv_layers"]:
            issues.append("keine UV-Map")
        scale = [abs(c) for c in obj.scale]
        if any(abs(s - 1.0) > 1e-3 for s in scale):
            issues.append(f"Skalierung nicht angewendet {[round(s, 3) for s in scale]}")
        if info["loose_vertices"]:
            issues.append(f"{info['loose_vertices']} lose Punkte")
        if budget and info["triangles"] > int(budget):
            issues.append(f"über Budget: {info['triangles']} > {budget} Dreiecke")
        total_tris += info["triangles"]
        report.append({
            "object": obj.name,
            "triangles": info["triangles"],
            "polygons": info["polygons"],
            "vertices": info["vertices"],
            "islands": info["islands"],
            "uv_layers": info["uv_layers"],
            "materials": info["materials"],
            "ok": not issues,
            "issues": issues,
        })
    return {
        "objects": report,
        "total_triangles": total_tris,
        "budgets": ROBLOX_BUDGETS,
        "summary": ("Alles technisch unauffällig." if all(r.get("ok", True) for r in report)
                    else "Es gibt Punkte, die vor dem Export geprüft werden sollten."),
        "limits": ("Diese Prüfung erkennt Geometrie- und Exportrisiken. Sie ersetzt "
                   "keinen Test in Roblox Studio."),
    }


def check_roblox(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    names = params.get("objects") or params.get("object")
    objs = H.as_object_list(names) or [o for o in bpy.context.scene.objects if o.type == "MESH"]
    scene = bpy.context.scene
    entries = []
    for obj in objs:
        dims_m = H.dimensions(obj)
        dims_studs = G.dims_to_studs(dims_m)
        lo, hi = H.world_bbox(obj)
        origin = [round(c, 4) for c in obj.matrix_world.translation]
        origin_inside = all(lo[i] - 1e-6 <= origin[i] <= hi[i] + 1e-6 for i in range(3))
        notes: List[str] = []
        if not origin_inside:
            notes.append("Ursprung liegt außerhalb der Geometrie - Pivot in Roblox prüfen.")
        if any(abs(s - 1.0) > 1e-3 for s in obj.scale):
            notes.append("Skalierung nicht angewendet - beim Export kann sie verloren gehen.")
        if not obj.data.uv_layers:
            notes.append("Keine UV-Map: Texturen müssen in Roblox über Materialfarben "
                         "oder nachträgliches UV-Mapping kommen.")
        tris = H.tri_count(obj)
        entries.append({
            "object": obj.name,
            "dimensions_m": [round(c, 4) for c in dims_m],
            "dimensions_studs": [round(c, 3) for c in dims_studs],
            "triangles": tris,
            "pivot_world": origin,
            "pivot_inside_geometry": origin_inside,
            "height_studs": round(dims_studs[2], 3),
            "notes": notes,
        })
    return {
        "unit_note": (f"Blender arbeitet in Metern, Roblox in Studs. Umrechnung: "
                      f"1 Stud = {G.STUD_IN_METERS} m. Wenn dein Asset z. B. 6 m hoch ist, "
                      f"erscheint es in Roblox als {round(6 / G.STUD_IN_METERS, 2)} Studs."),
        "recommended_scale": ("Roblox-Charaktere sind ca. 5 Studs hoch (1.4 m). Prüfe, ob "
                              "die Größe zur Umgebung passt."),
        "objects": entries,
        "scene_frame": [scene.frame_start, scene.frame_end],
        "export_hint": ("file.export mit format='glb' erzeugt eine Datei für den "
                        "Roblox-Import (3D-Importer). FBX funktioniert auch, verliert "
                        "aber manchmal Materialien."),
        "manual_checks": [
            "Nach dem Import in Roblox Studio: Größe und Position prüfen.",
            "Kollisionen (CollisionFidelity) und CanCollide einstellen.",
            "Materialien: glTF bringt PBR, Roblox nutzt SurfaceAppearance/MaterialVariant.",
            "Polygonanzahl im Studio unter 'Info' kontrollieren.",
        ],
        "honesty": ("Was das Programm prüft: Maße, Pivot, Tri-Zahl, UVs, Skalierung. "
                    "Was du selbst prüfen musst: Aussehen im Studio, Kollisionen, "
                    "Materialwirkung, Streaming-Verhalten."),
    }


def check_relations(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    issues: List[Dict[str, Any]] = []
    report = []
    for obj in bpy.data.objects:
        for con in obj.constraints:
            if con.type in ("CHILD_OF", "COPY_TRANSFORMS", "COPY_LOCATION", "COPY_ROTATION",
                            "SHRINKWRAP", "DAMPED_TRACK", "TRACK_TO", "STRETCH_TO"):
                if con.target is None:
                    issues.append({"object": obj.name, "constraint": con.name,
                                   "problem": "Ziel fehlt"})
                else:
                    report.append({"object": obj.name, "constraint": con.name,
                                   "type": con.type, "target": con.target.name})
        for mod in obj.modifiers:
            if mod.type == "SHRINKWRAP" and getattr(mod, "target", None) is None:
                issues.append({"object": obj.name, "modifier": mod.name,
                               "problem": "Shrinkwrap ohne Ziel"})
        if obj.parent and obj.parent.name not in bpy.data.objects:
            issues.append({"object": obj.name, "problem": "Parent fehlt"})
    return {"relations": report, "issues": issues,
            "ok": not issues,
            "note": ("Dauerhafte Beziehungen (Parent/Constraint/Shrinkwrap) bleiben "
                     "erhalten, wenn sich das Ziel ändert - bis du sie entfernst.")}


TOOLS = {
    "check.technical": check_technical,
    "check.roblox": check_roblox,
    "check.relations": check_relations,
}

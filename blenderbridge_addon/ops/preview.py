"""preview.*: render views so an agent (and a human) can actually look at the model.

Rendering happens in Blender's main thread and blocks the UI while it runs, so
resolutions are small by default and samples are low.  Everything temporary is
restored afterwards, including the user's camera, engine and world.
"""

from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Optional, Tuple

import bpy
from mathutils import Euler, Vector, Matrix

from .. import helpers as H
from ..shared import geometry as G
from ..shared.protocol import ErrorCode

VIEW_DIRECTIONS = {
    "perspective": (1.0, -1.0, 0.65),
    "front": (0.0, -1.0, 0.0),
    "back": (0.0, 1.0, 0.0),
    "right": (1.0, 0.0, 0.0),
    "left": (-1.0, 0.0, 0.0),
    "top": (0.0, 0.0, 1.0),
    "bottom": (0.0, 0.0, -1.0),
    "three_quarter": (-1.0, -1.0, 0.6),
}

DEFAULT_VIEWS = ["perspective", "front", "right"]
ENGINE_ALIASES = {
    "cycles": "CYCLES",
    "eevee": "BLENDER_EEVEE_NEXT",
    "eevee_next": "BLENDER_EEVEE_NEXT",
    "workbench": "BLENDER_WORKBENCH",
}


def _visible_meshes() -> List["bpy.types.Object"]:
    out = []
    for obj in bpy.context.scene.objects:
        if obj.type == "MESH" and not obj.hide_render and not obj.hide_get():
            out.append(obj)
    return out


def _focus_box(params: Dict[str, Any]) -> Tuple[G.Vec3, G.Vec3]:
    if params.get("region") and params.get("object"):
        obj = H.get_mesh_object(params["object"])
        region = H.load_region(obj, str(params["region"]))
        positions = H.local_positions(obj)
        mw = obj.matrix_world
        pts = [tuple(mw @ Vector(positions[i])) for i in region.vertices]
        if pts:
            return G.aabb(pts)
    if params.get("object"):
        obj = H.get_object(params["object"])
        return H.world_bbox(obj)
    boxes = []
    for obj in _visible_meshes():
        try:
            boxes.append(H.world_bbox(obj))
        except Exception:  # noqa: BLE001
            continue
    if not boxes:
        raise H.fail(ErrorCode.NO_GEOMETRY,
                     "Keine sichtbare Geometrie zum Rendern gefunden.")
    return G.aabb_union(boxes)


#: Engines that need a window system / GPU context.  In a background Blender
#: (``blender --background``, headless CI, or a Python session without a display)
#: asking for one of these does not raise an exception - the process *aborts*.
#: So they are only tried when the caller asked for them explicitly or when a
#: real window system is present.
GPU_ENGINES = ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "BLENDER_WORKBENCH")
KNOWN_ENGINES = ("CYCLES",) + GPU_ENGINES


def _engine_chain(requested: str, background: Optional[bool] = None) -> List[str]:
    """Ordered list of engines to try, safest first.

    CPU rendering (Cycles) always works; the GPU engines are a bonus.  Trying an
    unavailable GPU engine first used to abort Blender in headless runs, which is
    why the order depends on whether there is a display.
    """
    if background is None:
        background = bool(getattr(bpy.app, "background", False))
    scene_engine = bpy.context.scene.render.engine
    explicit = requested and requested != "auto"
    wanted = ENGINE_ALIASES.get(requested.lower(), requested) if explicit else ""

    chain: List[str] = []
    if explicit:
        chain.append(wanted)
    if not background:
        if scene_engine in KNOWN_ENGINES:
            chain.append(scene_engine)
        chain += ["CYCLES", "BLENDER_EEVEE_NEXT", "BLENDER_WORKBENCH"]
    else:
        # Cycles only, unless the caller asked for something specific.
        if scene_engine == "CYCLES":
            chain.append("CYCLES")
        chain.append("CYCLES")
        if explicit and wanted in GPU_ENGINES and wanted != "CYCLES":
            chain.append(wanted)
    seen, out = set(), []
    for engine in chain:
        if engine and engine not in seen:
            seen.add(engine)
            out.append(engine)
    return out


def _setup_camera(center: G.Vec3, radius: float, direction: Sequence3, ortho: bool,
                  cam: "bpy.types.Object") -> None:
    d = Vector(direction).normalized()
    distance = max(radius * 3.2, 1.2)
    location = Vector(center) + d * distance
    cam.location = location
    look = Vector(center) - location
    cam.rotation_euler = look.to_track_quat("-Z", "Y").to_euler()
    cam.data.type = "ORTHO" if ortho else "PERSP"
    if ortho:
        cam.data.ortho_scale = max(radius * 2.3, 0.6)
    else:
        cam.data.angle = math.radians(39.6)


Sequence3 = Tuple[float, float, float]


def preview_render(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    scene = bpy.context.scene
    out_dir = H.artifact_dir(ctx.meta)
    resolution = int(params.get("resolution", 640))
    resolution = max(96, min(resolution, 2048))
    samples = max(1, min(int(params.get("samples", 16)), 256))
    views = params.get("views") or DEFAULT_VIEWS
    if isinstance(views, str):
        views = [views]
    views = [str(v).lower() for v in views]
    unknown = [v for v in views if v not in VIEW_DIRECTIONS]
    if unknown:
        raise H.fail(ErrorCode.PARAM_INVALID, f"unbekannte Ansicht(en): {unknown}",
                     allowed=sorted(VIEW_DIRECTIONS))
    isolate = bool(params.get("isolate", bool(params.get("object"))))
    transparent = bool(params.get("transparent", False))

    box = _focus_box(params)
    center = G.aabb_center(box)
    dims = G.aabb_dims(box)
    radius = max(G.vlen(dims) / 2.0, 0.1)

    # --- remember the scene state we are about to change -------------------
    original = {
        "camera": scene.camera.name if scene.camera else None,
        "engine": scene.render.engine,
        "resolution_x": scene.render.resolution_x,
        "resolution_y": scene.render.resolution_y,
        "resolution_percentage": scene.render.resolution_percentage,
        "filepath": scene.render.filepath,
        "film_transparent": scene.render.film_transparent,
        "world": scene.world.name if scene.world else None,
        "samples": getattr(scene.cycles, "samples", None) if hasattr(scene, "cycles") else None,
        "hidden": {o.name: o.hide_render for o in bpy.context.scene.objects},
    }
    temp_objects: List["bpy.types.Object"] = []
    artifacts: List[Dict[str, Any]] = []
    warnings: List[str] = []
    used_engine = ""
    try:
        # camera
        cam_data = bpy.data.cameras.new("BB_PreviewCam")
        cam = bpy.data.objects.new("BB_PreviewCam", cam_data)
        scene.collection.objects.link(cam)
        temp_objects.append(cam)
        scene.camera = cam

        # lights (only temporary, only if the scene has none)
        lights = [o for o in scene.objects if o.type == "LIGHT"]
        if not lights:
            sun_data = bpy.data.lights.new("BB_PreviewSun", type="SUN")
            sun_data.energy = 3.0
            sun = bpy.data.objects.new("BB_PreviewSun", sun_data)
            sun.rotation_euler = Euler((math.radians(55), 0, math.radians(35)), "XYZ")
            scene.collection.objects.link(sun)
            temp_objects.append(sun)
            warnings.append("Keine Lichtquelle in der Szene - temporäre Sonne verwendet.")

        if scene.world is None:
            world = bpy.data.worlds.new("BB_PreviewWorld")
            world.use_nodes = True
            bg = world.node_tree.nodes.get("Background")
            if bg:
                bg.inputs[0].default_value = (0.35, 0.38, 0.42, 1.0)
                bg.inputs[1].default_value = 1.0
            scene.world = world
            warnings.append("Keine Welt gesetzt - temporärer grauer Hintergrund.")

        if isolate:
            focus_name = params.get("object")
            for obj in scene.objects:
                if focus_name and obj.name == focus_name:
                    continue
                if obj in temp_objects:
                    continue
                obj.hide_render = True

        # render settings
        scene.render.resolution_x = resolution
        scene.render.resolution_y = int(resolution * 0.75)
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = "PNG"
        scene.render.film_transparent = transparent

        chain = _engine_chain(str(params.get("engine", "auto")))
        if getattr(bpy.app, "background", False) and "CYCLES" in chain and \
                chain[0] != "CYCLES":
            warnings.append("Blender läuft ohne Fenster - GPU-Engines können den "
                            "Prozess beenden, deshalb zuerst CYCLES.")
        last_error = ""
        for engine in chain:
            try:
                scene.render.engine = engine
                if engine == "CYCLES":
                    scene.cycles.samples = samples
                    scene.cycles.use_denoising = False
                artifacts = _render_views(scene, cam, views, box, center, radius,
                                          out_dir, params)
                used_engine = engine
                break
            except Exception as exc:  # noqa: BLE001 - try the next engine
                last_error = H.short_error(exc)
                for path in [a["path"] for a in artifacts]:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                artifacts = []
                continue
        if not artifacts:
            raise H.fail(ErrorCode.BLENDER_ERROR,
                         "Rendering mit allen Engines fehlgeschlagen",
                         last_error=last_error, tried=chain)
        if used_engine != chain[0]:
            warnings.append(f"Rendering mit {used_engine} statt {chain[0]} "
                            f"(Grund: {last_error or 'Engine nicht verfügbar'}).")
    finally:
        # --- restore -------------------------------------------------------
        try:
            for obj in bpy.context.scene.objects:
                if obj.name in original["hidden"]:
                    obj.hide_render = original["hidden"][obj.name]
            scene.render.engine = original["engine"]
            scene.render.resolution_x = original["resolution_x"]
            scene.render.resolution_y = original["resolution_y"]
            scene.render.resolution_percentage = original["resolution_percentage"]
            scene.render.filepath = original["filepath"]
            scene.render.film_transparent = original["film_transparent"]
            if original["camera"] and bpy.data.objects.get(original["camera"]):
                scene.camera = bpy.data.objects[original["camera"]]
            elif scene.camera in temp_objects:
                scene.camera = None
            if original["samples"] is not None and hasattr(scene, "cycles"):
                scene.cycles.samples = original["samples"]
            if original["world"] and bpy.data.worlds.get(original["world"]):
                scene.world = bpy.data.worlds[original["world"]]
        except Exception:  # noqa: BLE001
            pass
        for obj in temp_objects:
            data = obj.data
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception:  # noqa: BLE001
                pass
            if isinstance(data, bpy.types.Camera) and data.users == 0:
                bpy.data.cameras.remove(data)
            elif isinstance(data, bpy.types.Light) and data.users == 0:
                bpy.data.lights.remove(data)

    return {
        "engine": used_engine,
        "views": views,
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
        "focus": {"center": [round(c, 4) for c in center],
                  "dimensions": [round(c, 4) for c in dims]},
        "artifacts": artifacts,
        "warnings": warnings,
        "note": ("Diese Bilder sind echte Renderings aus Blender. Beurteile daran "
                 "Silhouette, Proportionen und grobe Fehler - nicht Materialdetails, "
                 "wenn du einen schnellen Engine-Fallback siehst."),
    }


def _render_views(scene, cam, views, box, center, radius, out_dir, params) -> List[Dict[str, Any]]:
    artifacts = []
    ortho_views = {"front", "back", "left", "right", "top", "bottom"}
    for view in views:
        direction = VIEW_DIRECTIONS[view]
        _setup_camera(center, radius, direction, view in ortho_views, cam)
        name = f"preview_{view}.png"
        path = H.safe_join(out_dir, name)
        scene.render.filepath = path
        bpy.ops.render.render(write_still=True)
        if not os.path.exists(path):
            raise RuntimeError(f"kein Bild geschrieben: {path}")
        artifacts.append({
            "path": path,
            "kind": "image",
            "view": view,
            "description": f"Ansicht '{view}' - {os.path.basename(path)}",
            "bytes": os.path.getsize(path),
        })
    return artifacts


def preview_snapshot_selection(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    objs = list(bpy.context.selected_objects)
    out = []
    for obj in objs:
        entry = {
            "name": obj.name,
            "type": obj.type,
            "bbox": [list(H.world_bbox(obj)[0]), list(H.world_bbox(obj)[1])],
            "dimensions": [round(c, 4) for c in H.dimensions(obj)],
            "parent": obj.parent.name if obj.parent else None,
        }
        if obj.type == "MESH":
            selected = H.selected_vertex_indices(obj)
            entry["vertices"] = len(obj.data.vertices)
            entry["selected_vertices"] = len(selected)
            entry["regions"] = {k: len(v.get("vertices", []))
                                for k, v in H.all_regions(obj).items()}
        out.append(entry)
    return {"mode": bpy.context.mode, "objects": out,
            "hint": ("Ist eine Vertex-Auswahl aktiv, kann sie mit "
                     "region.from_selection als Bereich gespeichert werden.")}


TOOLS = {
    "preview.render": preview_render,
    "preview.snapshot_selection": preview_snapshot_selection,
}

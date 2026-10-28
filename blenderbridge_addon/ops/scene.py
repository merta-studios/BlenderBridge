"""scene.* and bridge.* tools."""

from __future__ import annotations

from typing import Any, Dict

import bpy

from .. import facts
from .. import helpers as H
from ..shared.protocol import ErrorCode


def scene_info(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    detail = str(params.get("detail", "summary")).lower()
    if detail not in ("summary", "full"):
        raise H.fail(ErrorCode.PARAM_INVALID, "detail muss 'summary' oder 'full' sein")
    return facts.scene_info(detail)


def scene_tree(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    return facts.scene_tree(params.get("root"), int(params.get("depth", 8)))


def scene_stats(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    return facts.scene_stats()


def scene_frame(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    scene = bpy.context.scene
    return {"frame": scene.frame_current, "frame_start": scene.frame_start,
            "frame_end": scene.frame_end, "fps": scene.render.fps}


def scene_set_frame(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    frame = int(params.get("frame", 1))
    H.push_undo("Frame setzen")
    bpy.context.scene.frame_set(frame)
    return {"frame": bpy.context.scene.frame_current}


def bridge_info(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    from .. import server
    return {
        "addon_version": H.ADDON_VERSION,
        "blender_version": bpy.app.version_string,
        "protocol": "1.0",
        "platform": bpy.app.build_platform.decode() if isinstance(bpy.app.build_platform, bytes)
        else str(bpy.app.build_platform),
        "file": bpy.data.filepath,
        "background": bpy.app.background,
        "server": server.STATE.status(),
        "tools": sorted(server.STATE.registry.names()) if server.STATE.registry else [],
        "capabilities": {
            "mesh_regions": True,
            "preview_render": True,
            "script_mode": bool(server.prefs().allow_script_mode),
            "snapshots": True,
        },
    }


def bridge_selection(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    objs = list(bpy.context.selected_objects)
    active = bpy.context.view_layer.objects.active
    out: Dict[str, Any] = {
        "mode": bpy.context.mode,
        "active": active.name if active else None,
        "objects": [{"name": o.name, "type": o.type,
                     "vertices": len(o.data.vertices) if o.type == "MESH" else 0}
                    for o in objs],
    }
    if active and active.type == "MESH":
        selected = H.selected_vertex_indices(active)
        out["selected_vertices"] = len(selected)
        if selected:
            out["selection_preview"] = selected[:50]
        if bpy.context.mode in ("EDIT_MESH", "EDIT"):
            faces = [p.index for p in active.data.polygons if p.select]
            out["selected_faces"] = len(faces)
    return out


def bridge_ping(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    return {"pong": True, "frame": bpy.context.scene.frame_current}


TOOLS = {
    "scene.info": scene_info,
    "scene.tree": scene_tree,
    "scene.stats": scene_stats,
    "scene.frame": scene_frame,
    "scene.set_frame": scene_set_frame,
    "bridge.info": bridge_info,
    "bridge.selection": bridge_selection,
    "bridge.ping": bridge_ping,
}

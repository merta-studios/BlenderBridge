"""file.*: snapshots, restore, save and export.

Snapshots are real ``.blend`` copies written next to the project file (or in the
user's home folder when the project was never saved).  They are the only
reliable way to undo *destructive* work - Blender's own undo stack dies when the
file is closed or when a tool operates on data outside the undo system.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import time
from typing import Any, Dict, List

import bpy

from .. import helpers as H
from ..shared.protocol import ErrorCode

MAX_ARTIFACT_READ = 8 * 1024 * 1024


def file_snapshot(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    label = str(params.get("label") or "snapshot")
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    directory = H.scene_snapshot_dir()
    name = f"{stamp}_{safe_label}.blend"
    path = os.path.join(directory, name)
    # Never overwrite: two snapshots in the same second get a suffix.
    counter = 1
    while os.path.exists(path):
        counter += 1
        path = os.path.join(directory, f"{stamp}_{safe_label}_{counter}.blend")

    if not bpy.data.filepath:
        H.push_undo("Sicherung")
        bpy.ops.wm.save_as_mainfile(filepath=path, copy=True, check_existing=False)
    else:
        bpy.ops.wm.save_as_mainfile(filepath=path, copy=True, check_existing=False)
    size = os.path.getsize(path)
    index_path = os.path.join(directory, "index.json")
    index = _read_index(index_path)
    index.append({
        "file": name,
        "path": path,
        "label": label,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "bytes": size,
        "source_blend": bpy.data.filepath or "(unsaved)",
    })
    index = index[-200:]
    try:
        with open(index_path, "w", encoding="utf-8") as fh:
            json.dump(index, fh, indent=1)
    except OSError:
        pass
    _prune(directory, int(params.get("keep", 60)))
    return {
        "snapshot": name,
        "path": path,
        "bytes": size,
        "directory": directory,
        "created_at": index[-1]["created_at"],
        "note": ("Diese Sicherung ist eine echte .blend-Datei. Wiederherstellen mit "
                 "file.restore oder indem du sie in Blender öffnest."),
    }


def _read_index(path: str) -> List[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _prune(directory: str, keep: int) -> None:
    files = sorted((f for f in os.listdir(directory) if f.endswith(".blend")), reverse=True)
    for name in files[max(1, keep):]:
        try:
            os.unlink(os.path.join(directory, name))
        except OSError:
            pass


def file_list_snapshots(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    directory = H.scene_snapshot_dir()
    index = _read_index(os.path.join(directory, "index.json"))
    existing = []
    for entry in reversed(index):
        path = entry.get("path") or os.path.join(directory, entry.get("file", ""))
        entry = dict(entry)
        entry["exists"] = os.path.exists(path)
        if entry["exists"]:
            entry["bytes"] = os.path.getsize(path)
        existing.append(entry)
    return {"directory": directory, "count": len(existing),
            "snapshots": existing[:50],
            "hint": "Ältere Sicherungen werden automatisch aufgeräumt (Standard: 60)."}


def file_restore(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    name = params.get("snapshot")
    if not name:
        raise H.fail(ErrorCode.PARAM_INVALID, "snapshot fehlt")
    if params.get("confirm") not in (True, "yes", "JA", "ja"):
        raise H.fail(ErrorCode.PRECONDITION_FAILED,
                     "Wiederherstellen braucht confirm=true - dabei gehen alle Änderungen "
                     "seit der Sicherung verloren.")
    directory = H.scene_snapshot_dir()
    path = os.path.join(directory, os.path.basename(str(name)))
    if not os.path.exists(path):
        raise H.fail(ErrorCode.NOT_FOUND, f"Sicherung nicht gefunden: {name}",
                     directory=directory)
    # save the current state first, so a restore is itself reversible
    safety = file_snapshot({"label": "before_restore"}, ctx)
    bpy.ops.wm.open_mainfile(filepath=path)
    return {"restored": path, "safety_snapshot": safety["snapshot"],
            "note": "Die Datei wurde geöffnet; die vorherige Szene liegt als Sicherung vor."}


def file_save(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    path = params.get("path")
    copy = bool(params.get("copy", False))
    if path:
        path = os.path.abspath(str(path))
        if not path.lower().endswith(".blend"):
            path += ".blend"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=path, copy=copy, check_existing=False)
        return {"saved": path, "copy": copy}
    if not bpy.data.filepath:
        raise H.fail(ErrorCode.PRECONDITION_FAILED,
                     "Die Datei wurde noch nie gespeichert - bitte 'path' angeben.")
    bpy.ops.wm.save_mainfile()
    return {"saved": bpy.data.filepath}


def file_export(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    fmt = str(params.get("format", "glb")).lower()
    if fmt not in ("glb", "gltf", "fbx", "obj", "stl"):
        raise H.fail(ErrorCode.PARAM_INVALID, f"unbekanntes Format: {fmt}",
                     allowed=["glb", "gltf", "fbx", "obj", "stl"])
    out_dir = H.artifact_dir(ctx.meta)
    default_name = (bpy.path.basename(bpy.data.filepath).rsplit(".", 1)[0]
                    if bpy.data.filepath else "model")
    name = str(params.get("path") or f"{default_name}.{fmt}")
    path = H.safe_join(out_dir, name)
    objs = H.as_object_list(params.get("objects")) if params.get("objects") else []
    if objs:
        H.select_only(objs)
        use_selection = True
    else:
        use_selection = bool(params.get("selection_only", False))
    try:
        if fmt in ("glb", "gltf"):
            bpy.ops.export_scene.gltf(
                filepath=path,
                export_format="GLB" if fmt == "glb" else "GLTF_SEPARATE",
                use_selection=use_selection,
                export_apply=bool(params.get("apply_modifiers", True)),
                export_yup=True,
            )
        elif fmt == "fbx":
            bpy.ops.export_scene.fbx(
                filepath=path, use_selection=use_selection,
                apply_scale_options="FBX_SCALE_ALL",
                bake_space_transform=True,
            )
        elif fmt == "obj":
            bpy.ops.wm.obj_export(filepath=path, export_selected_objects=use_selection)
        else:
            bpy.ops.export_mesh.stl(filepath=path, use_selection=use_selection)
    except (AttributeError, RuntimeError) as exc:
        raise H.fail(ErrorCode.BLENDER_ERROR,
                     f"Export nach {fmt} fehlgeschlagen: {H.short_error(exc)}")
    if not os.path.exists(path):
        raise H.fail(ErrorCode.BLENDER_ERROR, f"Exportdatei wurde nicht erzeugt: {path}")
    return {
        "format": fmt,
        "path": path,
        "bytes": os.path.getsize(path),
        "artifacts": [{"path": path, "kind": "model", "description": f"{fmt}-Export"}],
        "note": ("glTF/GLB eignet sich für den Roblox-3D-Importer. Prüfe nach dem Import "
                 "Größe, Pivot und Materialien in Studio."),
    }


def file_artifact(params: Dict[str, Any], ctx) -> Dict[str, Any]:
    """Read a file that this add-on produced (e.g. an export) back as base64."""
    path = params.get("path")
    if not path:
        raise H.fail(ErrorCode.PARAM_INVALID, "path fehlt")
    allowed_root = os.path.realpath(H.artifact_dir(ctx.meta))
    resolved = os.path.realpath(str(path))
    if not resolved.startswith(allowed_root):
        raise H.fail(ErrorCode.FORBIDDEN,
                     "Nur Dateien im Auftragsordner dürfen gelesen werden.",
                     allowed_root=allowed_root)
    if not os.path.isfile(resolved):
        raise H.fail(ErrorCode.NOT_FOUND, "Datei nicht gefunden")
    max_bytes = min(int(params.get("max_bytes", 400_000)), MAX_ARTIFACT_READ)
    if os.path.getsize(resolved) > max_bytes:
        raise H.fail(ErrorCode.PAYLOAD_TOO_LARGE,
                     f"Datei ist größer als {max_bytes} Bytes",
                     bytes=os.path.getsize(resolved))
    with open(resolved, "rb") as fh:
        data = fh.read()
    return {"path": resolved, "bytes": len(data),
            "base64": base64.b64encode(data).decode("ascii")}


TOOLS = {
    "file.snapshot": file_snapshot,
    "file.list_snapshots": file_list_snapshots,
    "file.restore": file_restore,
    "file.save": file_save,
    "file.export": file_export,
    "file.artifact": file_artifact,
}

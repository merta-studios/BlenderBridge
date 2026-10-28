"""Sidebar panel in the 3D viewport: start/stop, status, live log, quick actions."""

from __future__ import annotations

import bpy
from bpy.types import Panel

from . import helpers as H


class BLENDERBRIDGE_PT_main(Panel):
    bl_label = "BlenderBridge"
    bl_idname = "BLENDERBRIDGE_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BlenderBridge"

    def draw(self, context):
        from . import server

        layout = self.layout
        state = server.STATE
        status = state.status()
        prefs = context.preferences.addons[__package__].preferences

        box = layout.box()
        row = box.row()
        if status["running"]:
            row.label(text=f"Läuft · Port {status['port']}", icon="LINKED")
        else:
            row.label(text="Gestoppt", icon="UNLINKED")

        if status["running"]:
            col = box.column(align=True)
            col.label(text=f"Anfragen: {status['requests']} · Fehler: {status['errors']}")
            col.label(text=f"Warteschlange: {status['queue']} · Werkzeuge: {status['tools']}")
            if status["last_request"]:
                col.label(text=f"Zuletzt: {status['last_request']}"[:60])
            if status["last_error"]:
                col.label(text=f"Fehler: {status['last_error'][:50]}", icon="ERROR")
            row = box.row(align=True)
            row.operator("blenderbridge.stop_server", icon="CANCEL")
            row.operator("blenderbridge.copy_token", icon="COPYDOWN")
        else:
            box.label(text="Connector startet die Verbindung.", icon="INFO")
            box.operator("blenderbridge.start_server", icon="PLAY")

        col = layout.column(align=True)
        col.label(text="Sicherheit", icon="LOCKED")
        col.prop(prefs, "allow_script_mode", text="Skript-Modus")
        if prefs.allow_script_mode:
            col.label(text="Skript-Modus AN - voller Zugriff", icon="ERROR")

        layout.separator()
        box = layout.box()
        box.label(text="Bereiche (Mesh-Gedächtnis)", icon="MESH_DATA")
        obj = context.object
        if obj and obj.type == "MESH":
            regions = H.all_regions(obj)
            if regions:
                for name, raw in list(regions.items())[:8]:
                    row = box.row()
                    row.label(text=f"{name} ({len(raw.get('vertices', []))})",
                              icon="LOCKED" if raw.get("protected") else "DOT")
                if len(regions) > 8:
                    box.label(text=f"… {len(regions) - 8} weitere")
            else:
                box.label(text="Noch keine Bereiche", icon="INFO")
            box.operator("blenderbridge.save_selection", icon="ADD",
                         text="Auswahl als Bereich")
        else:
            box.label(text="Mesh auswählen", icon="INFO")

        layout.separator()
        row = layout.row(align=True)
        row.operator("blenderbridge.snapshot", icon="FILE_TICK")
        row.operator("blenderbridge.check_connection", icon="QUESTION")

        log = state.log[-6:]
        if log:
            box = layout.box()
            box.label(text="Protokoll", icon="TEXT")
            for line in log:
                box.label(text=line[:64])


class BLENDERBRIDGE_PT_help(Panel):
    bl_label = "Erste Schritte"
    bl_idname = "BLENDERBRIDGE_PT_help"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BlenderBridge"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        for text in (
            "1. Server starten (Knopf oben)",
            "2. Windows-Connector starten",
            "3. Im Browser-Fenster steht 'Blender verbunden'",
            "4. Agent gibt den Schlüssel im Header mit",
            "",
            "Bereiche: Mesh auswählen, Punkte markieren,",
            "'Auswahl als Bereich' - dann kann der Agent",
            "gezielt z. B. die Wange verändern.",
        ):
            col.label(text=text)


CLASSES = (BLENDERBRIDGE_PT_main, BLENDERBRIDGE_PT_help)


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:  # noqa: BLE001
            pass

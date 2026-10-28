"""BlenderBridge - Blender add-on side of the AI modelling bridge.

Install this as a zip (Edit → Preferences → Add-ons → Install…), enable it, press
N in the 3D viewport and open the "BlenderBridge" tab, then "Server starten".
The add-on listens on 127.0.0.1 only and requires the shared token.
"""

bl_info = {
    "name": "BlenderBridge",
    "author": "Merta Studios",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "location": "3D Viewport > Sidebar (N) > BlenderBridge",
    "description": "Kontrollierte Brücke zwischen KI-Agent, Windows-Connector und Blender-Szene",
    "category": "3D View",
}

import importlib
import sys

import bpy
from bpy.props import BoolProperty, IntProperty, StringProperty
from bpy.types import AddonPreferences, Operator, Panel

from . import helpers as H
from . import server as bridge_server
from . import ui

MODULES = [
    "shared", "shared.protocol", "shared.geometry", "shared.regions", "shared.relations",
    "shared.tools_catalogue", "helpers", "facts", "server",
    "ops", "ops.scene", "ops.objects", "ops.mesh", "ops.region", "ops.meta",
    "ops.relation", "ops.preview", "ops.check", "ops.file", "ops.script", "ui",
]


class BlenderBridgePreferences(AddonPreferences):
    bl_idname = __package__

    host: StringProperty(
        name="Bind-Adresse",
        description="Nur localhost verwenden - externe Erreichbarkeit macht der Connector",
        default="127.0.0.1",
    )
    port: IntProperty(name="Port", default=9876, min=1024, max=65535)
    token: StringProperty(
        name="Gemeinsamer Token",
        description="Leer lassen = automatisch aus der gemeinsamen Datei lesen",
        default="",
        subtype="PASSWORD",
    )
    auto_start: BoolProperty(
        name="Server beim Start von Blender automatisch starten",
        default=False,
    )
    allow_script_mode: BoolProperty(
        name="Skript-Modus erlauben (script.run)",
        description="Führt beliebigen Python-Code in Blender aus. Nur einschalten, wenn du "
                    "dem Agenten voll vertraust. Zusätzlich verlangt der Connector eine "
                    "Freigabe.",
        default=False,
    )
    log_requests: BoolProperty(name="Anfragen im Panel protokollieren", default=True)
    verbose_errors: BoolProperty(name="Ausführliche Fehlermeldungen", default=False)

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.label(text="Verbindung", icon="LINKED")
        row = col.row(align=True)
        row.prop(self, "host")
        row.prop(self, "port")
        col.prop(self, "token")
        col.prop(self, "auto_start")
        col.separator()
        col.label(text="Sicherheit", icon="LOCKED")
        col.prop(self, "allow_script_mode")
        if self.allow_script_mode:
            box = col.box()
            box.label(text="Skript-Modus aktiv: voller Zugriff auf Blender und Dateien.",
                      icon="ERROR")
        col.separator()
        col.label(text="Diagnose", icon="INFO")
        col.prop(self, "log_requests")
        col.prop(self, "verbose_errors")
        col.label(text=f"Konfigurationsordner: {bridge_server.config_dir()}")


class BLENDERBRIDGE_OT_start_server(Operator):
    bl_idname = "blenderbridge.start_server"
    bl_label = "Server starten"
    bl_description = "Nimmt Verbindungen vom Windows-Connector an (nur localhost)"

    def execute(self, context):
        prefs = context.preferences.addons[__package__].preferences
        try:
            status = bridge_server.start_server(prefs.host, prefs.port, prefs.token)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"BlenderBridge läuft auf {status['host']}:{status['port']}")
        return {"FINISHED"}


class BLENDERBRIDGE_OT_stop_server(Operator):
    bl_idname = "blenderbridge.stop_server"
    bl_label = "Server stoppen"
    bl_description = "Beendet die Verbindung sofort"

    def execute(self, context):
        bridge_server.stop_server()
        self.report({"INFO"}, "BlenderBridge-Server gestoppt")
        return {"FINISHED"}


class BLENDERBRIDGE_OT_copy_token(Operator):
    bl_idname = "blenderbridge.copy_token"
    bl_label = "Token kopieren"
    bl_description = "Kopiert den gemeinsamen Token in die Zwischenablage"

    def execute(self, context):
        token = bridge_server.STATE.token
        if not token:
            self.report({"WARNING"}, "Server läuft noch nicht")
            return {"CANCELLED"}
        context.window_manager.clipboard = token
        self.report({"INFO"}, "Token kopiert")
        return {"FINISHED"}


class BLENDERBRIDGE_OT_snapshot(Operator):
    bl_idname = "blenderbridge.snapshot"
    bl_label = "Sicherung erstellen"
    bl_description = "Speichert eine Kopie der aktuellen .blend-Datei"

    def execute(self, context):
        from .ops import file as file_ops
        from .server import JobContext
        ctx = JobContext(request_id="ui", tool="file.snapshot", meta={})
        try:
            result = file_ops.file_snapshot({"label": "manuell"}, ctx)
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"Sicherung fehlgeschlagen: {H.short_error(exc)}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Sicherung: {result['snapshot']}")
        return {"FINISHED"}


class BLENDERBRIDGE_OT_save_selection(Operator):
    bl_idname = "blenderbridge.save_selection"
    bl_label = "Auswahl als Bereich speichern"
    bl_description = "Speichert die ausgewählten Punkte als benannten Bereich"

    region_name: StringProperty(name="Name", default="bereich")

    def execute(self, context):
        from .ops import region as region_ops
        from .server import JobContext
        obj = context.object
        if obj is None or obj.type != "MESH":
            self.report({"ERROR"}, "Kein Mesh aktiv")
            return {"CANCELLED"}
        ctx = JobContext(request_id="ui", tool="region.from_selection", meta={})
        try:
            result = region_ops.region_from_selection(
                {"object": obj.name, "name": self.region_name}, ctx)
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, H.short_error(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Bereich '{result['region']['name']}' gespeichert "
                              f"({result['summary']['vertices']} Punkte)")
        return {"FINISHED"}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)


class BLENDERBRIDGE_OT_check_connection(Operator):
    bl_idname = "blenderbridge.check_connection"
    bl_label = "Verbindung prüfen"
    bl_description = "Zeigt Status und letzte Anfragen"

    def execute(self, context):
        status = bridge_server.STATE.status()
        if status["running"]:
            self.report({"INFO"},
                        f"Läuft auf {status['host']}:{status['port']} · "
                        f"{status['requests']} Anfragen · {status['tools']} Werkzeuge")
        else:
            self.report({"WARNING"}, "Server ist gestoppt")
        return {"FINISHED"}


CLASSES = (
    BlenderBridgePreferences,
    BLENDERBRIDGE_OT_start_server,
    BLENDERBRIDGE_OT_stop_server,
    BLENDERBRIDGE_OT_copy_token,
    BLENDERBRIDGE_OT_snapshot,
    BLENDERBRIDGE_OT_save_selection,
    BLENDERBRIDGE_OT_check_connection,
)


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    ui.register()
    settings = bridge_server.prefs()
    if getattr(settings, "auto_start", False):
        try:
            bridge_server.start_server(settings.host, settings.port, settings.token)
        except RuntimeError as exc:
            print(f"[BlenderBridge] Auto-Start fehlgeschlagen: {exc}")


def unregister() -> None:
    try:
        bridge_server.stop_server()
    except Exception:  # noqa: BLE001
        pass
    ui.unregister()
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    register()

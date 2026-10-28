"""The tool catalogue: one source of truth for names, risk and documentation.

The connector exposes this through ``GET /v1/tools`` so an AI agent can discover
what it may do, how risky it is and whether an approval will be requested.  The
test-suite asserts that the add-on registers exactly these tools and nothing
else, so an agent can never reach an undocumented backdoor.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ToolSpec:
    name: str
    risk: str                  # read | low | moderate | high | critical
    summary: str
    params: str = ""           # human readable parameter list
    mutating: bool = False
    needs_blender: bool = True
    approval_hint: str = ""
    examples: tuple = ()
    internal: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["examples"] = list(self.examples)
        return d


_SPECS: List[ToolSpec] = [
    # ---------------------------------------------------------------- scene
    ToolSpec("scene.info", "read",
             "Blender-Version, geöffnete Datei, Einheiten, Collections, Objekt- und Polygonzahlen.",
             "detail: 'summary'|'full'"),
    ToolSpec("scene.tree", "read",
             "Hierarchie aus Collections und Objekten mit Typ, Parent und Kinderzahl.",
             "root: optionaler Collectionname, depth: int"),
    ToolSpec("scene.stats", "read",
             "Gesamtzahlen: Objekte, Vertices, Flächen, Materialien, Größenextent der Szene."),
    ToolSpec("scene.frame", "read", "Aktueller Frame, Frame-Range, FPS."),
    ToolSpec("scene.set_frame", "low", "Setzt den aktuellen Frame.", "frame: int", mutating=True),

    # --------------------------------------------------------------- bridge
    ToolSpec("bridge.info", "read",
             "Version und Fähigkeiten des Add-ons, Blender-Version, aktive Einstellungen."),
    ToolSpec("bridge.selection", "read",
             "Aktuelle Auswahl in Blender: Objekte, Bearbeitungsmodus, ausgewählte Vertices."),
    ToolSpec("bridge.ping", "read", "Lebenszeichen inkl. Latenz."),

    # --------------------------------------------------------------- object
    ToolSpec("object.list", "read",
             "Alle Objekte mit Typ, Collection, sichtbarem Namen und Kennzahlen.",
             "type: optional Filter, collection: optional, limit: int"),
    ToolSpec("object.get", "read",
             "Detail zu einem Objekt: Transform, Mesh-Statistik, Modifier, Constraints, "
             "Materialien, Vertex-Gruppen, gespeicherte Tags.",
             "object: str"),
    ToolSpec("object.create_primitive", "low",
             "Erzeugt Würfel/Kugel/Zylinder/Kegel/Torus/Plane/Ebene-Kurve mit Namen und Transform.",
             "kind, name, location, rotation_deg, scale, collection, dimensions",
             mutating=True),
    ToolSpec("object.transform", "moderate",
             "Setzt oder ändert Position/Rotation/Skalierung, absolut oder relativ.",
             "object(s), location, rotation_deg, scale, relative: bool",
             mutating=True,
             approval_hint="Bestehende Positionen können überschrieben werden."),
    ToolSpec("object.duplicate", "low",
             "Dupliziert Objekte (verlinkt oder als Kopie) an eine neue Position.",
             "object(s), count, offset, linked: bool", mutating=True),
    ToolSpec("object.delete", "high",
             "Löscht Objekte. Sicherung vorher empfohlen.",
             "object(s), confirm_count: int", mutating=True,
             approval_hint="Gelöschte Objekte sind nur über eine Sicherung wiederherstellbar."),
    ToolSpec("object.rename", "low", "Benennt Objekte um.", "object, name", mutating=True),
    ToolSpec("object.move_to_collection", "low",
             "Verschiebt Objekte in eine Collection (erzeugt sie bei Bedarf).",
             "object(s), collection, create: bool", mutating=True),
    ToolSpec("object.parent", "moderate",
             "Setzt/entfernt Parenting inkl. 'keep transform'.", "object(s), parent, keep_transform",
             mutating=True),
    ToolSpec("object.apply_transform", "moderate",
             "Wendet Rotation/Skalierung an (wichtig für Roblox-Export).",
             "object(s), location, rotation, scale", mutating=True),
    ToolSpec("object.set_origin", "moderate",
             "Setzt den Objektursprung (Pivot) auf Geometrie/3D-Cursor/Bounding-Box-Punkt.",
             "object(s), mode: 'geometry'|'cursor'|'bbox_bottom'|'bbox_center'", mutating=True),
    ToolSpec("object.add_modifier", "moderate",
             "Fügt einen Modifier hinzu (Bevel, Subsurf, Solidify, Mirror, Array, Shrinkwrap …).",
             "object, type, options, name", mutating=True),
    ToolSpec("object.modifier_action", "moderate",
             "Ändert/entfernt/wendet Modifier an.", "object, modifier, action, options",
             mutating=True),
    ToolSpec("object.add_constraint", "moderate",
             "Fügt einen Constraint hinzu (Child Of, Copy Transforms, Damped Track …).",
             "object, type, target, options", mutating=True),
    ToolSpec("object.vertex_group", "low",
             "Erzeugt/ändert Vertex-Gruppen (Gewichte) für lokale Einflüsse.",
             "object, group, mode, vertices", mutating=True),
    ToolSpec("object.probe", "read",
             "Vermessung: Bounding-Box, Maße, Achsen, Anker, Oberflächennormale, Teile. "
             "Grundlage für räumliche Beziehungen.",
             "object, part, include_paths"),

    # ----------------------------------------------------------------- mesh
    ToolSpec("mesh.inspect", "read",
             "Technische Mesh-Analyse: Punkte/Kanten/Flächen, Inseln, N-Gons, "
             "nicht-mannigfaltige Kanten, Normalen, UVs, Vertex-Farben, Materialien.",
             "object, max_islands"),
    ToolSpec("mesh.points", "read",
             "Liefert Vertex-Koordinaten (gefiltert nach Region/Bereich/Stichprobe).",
             "object, region, bbox, limit, offset"),
    ToolSpec("mesh.edit", "moderate",
             "Lokale Verformung mit weichem Einfluss: move, inflate, scale, taper, bend, "
             "smooth, flatten, noise. Unterstützt geschützte Bereiche.",
             "object, region(s), op, amount/direction/axis …, profile, protect",
             mutating=True,
             approval_hint="Verändert bestehende Geometrie. Vorher wird bei Bedarf gesichert."),
    ToolSpec("mesh.extrude", "moderate",
             "Zieht aus einem Bereich neue Geometrie heraus (Äste, Arme, Fortsätze).",
             "object, region, distance, direction, taper, segments", mutating=True),
    ToolSpec("mesh.subdivide", "moderate",
             "Unterteilt ausgewählte Flächen/Kanten für mehr Detail.",
             "object, region, cuts, smoothness", mutating=True),
    ToolSpec("mesh.join", "high", "Fügt Objekte zu einem Mesh zusammen.", "objects, name",
             mutating=True),
    ToolSpec("mesh.separate", "moderate",
             "Trennt Bereiche/Inseln/Materialien in eigene Objekte.", "object, mode, region",
             mutating=True),
    ToolSpec("mesh.merge_by_distance", "moderate",
             "Verschmilzt doppelte Punkte.", "object, distance", mutating=True),
    ToolSpec("mesh.shade", "low", "Setzt Shading (smooth/flat) und Auto-Smooth.",
             "object, mode, angle", mutating=True),
    ToolSpec("mesh.uv", "moderate", "Erzeugt/aktualisiert UVs (Smart Project, Unwrap, Box).",
             "object, mode, angle_limit", mutating=True),
    ToolSpec("mesh.validate", "read", "Prüft, ob ein Mesh für Export/Roblox brauchbar ist."),

    # --------------------------------------------------------------- region
    ToolSpec("region.list", "read", "Alle gespeicherten Bereiche/Teile eines Objekts."),
    ToolSpec("region.create", "low",
             "Legt einen benannten Bereich an: aus Auswahl, Box, Kugel, Achsenband, "
             "Höhenanteil, Material oder loser Insel. Optional mit weichem Übergang.",
             "object, name, source, spec, soft, profile, protected", mutating=True),
    ToolSpec("region.from_selection", "low",
             "Übernimmt die aktuell in Blender ausgewählten Vertices als Bereich.",
             "object, name, soft", mutating=True),
    ToolSpec("region.get", "read", "Details eines Bereichs inkl. Gewichtsverteilung."),
    ToolSpec("region.update", "low", "Ändert Metadaten/Schutz/Notiz eines Bereichs.",
             "object, name, note, protected, kind", mutating=True),
    ToolSpec("region.delete", "low", "Löscht einen Bereich (und seine Vertex-Gruppe).",
             "object, name", mutating=True),
    ToolSpec("region.check", "read",
             "Prüft, ob gespeicherte Bereiche noch zur aktuellen Topologie passen (Staleness)."),
    ToolSpec("region.remap", "moderate",
             "Repariert einen veralteten Bereich nach Topologieänderungen.",
             "object, name, strategy", mutating=True),
    ToolSpec("anchor.set", "low", "Speichert einen benannten Form-Anker (Punkt + Richtung).",
             "object, name, position, direction", mutating=True),
    ToolSpec("anchor.list", "read", "Alle Anker eines Objekts."),
    ToolSpec("path.set", "low",
             "Speichert einen Verlauf (Polyline), z. B. die gebogene Stammachse.",
             "object, name, points, from_region", mutating=True),
    ToolSpec("path.list", "read", "Alle gespeicherten Verläufe eines Objekts."),

    # ----------------------------------------------------------------- meta
    ToolSpec("meta.set", "low",
             "Speichert Bedeutung/Struktur: Teile, Parameter eines Bauteils, Stilvorgaben, Notizen.",
             "object, key, value", mutating=True),
    ToolSpec("meta.get", "read", "Liest gespeicherte Metadaten eines Objekts."),
    ToolSpec("model.tree", "read",
             "Strukturbaum eines Modells aus gespeicherten Metadaten und der echten Hierarchie."),
    ToolSpec("model.params", "low",
             "Setzt Parameter eines vorbereiteten Bauteils und wendet sie an (z. B. Turmhöhe).",
             "object, params", mutating=True),

    # ------------------------------------------------------------- relation
    ToolSpec("relation.resolve", "read",
             "Berechnet aus einer beschriebenen Beziehung (auf/neben/zwischen/entlang) "
             "Position, Rotation und Größe - ohne etwas zu verändern.",
             "placement, target(s), anchor, rotation, size, gap"),
    ToolSpec("relation.apply", "moderate",
             "Wendet eine berechnete Beziehung auf Objekte an (einmalige Platzierung).",
             "object(s), placement, target(s), rotation, size", mutating=True),
    ToolSpec("relation.persist", "moderate",
             "Macht eine Beziehung dauerhaft: Parenting, Constraints oder Shrinkwrap.",
             "object(s), target, mode, options", mutating=True),
    ToolSpec("relation.distribute", "moderate",
             "Verteilt Kopien gleichmäßig entlang einer Kante/eines Verlaufs/zwischen Punkten.",
             "object, count, target, path, between", mutating=True),

    # -------------------------------------------------------------- preview
    ToolSpec("preview.render", "low",
             "Rendert Ansichten (perspektive, vorne, seite, oben, ausgewählt, Region) "
             "als PNG in den Job-Ordner. Nutzt vorhandene Kamera oder eine temporäre.",
             "views, object, region, resolution, engine, samples, wireframe",
             mutating=False),
    ToolSpec("preview.snapshot_selection", "read",
             "Beschreibt die aktuelle Auswahl so, dass ein Agent sie nachvollziehen kann."),

    # --------------------------------------------------------------- check
    ToolSpec("check.technical", "read",
             "Technische Prüfung: nicht-mannigfaltig, N-Gons, lose Geometrie, "
             "Normalen, angewandte Skalierung, Materialzahl, UVs, Polygonbudget.",
             "object(s), budget"),
    ToolSpec("check.roblox", "read",
             "Roblox-Vorbereitung: Maße in Studs, Ursprung, Objektaufteilung, "
             "Polygonbudget, Material-/UV-Hinweise, Export-Empfehlung.", "object(s)"),
    ToolSpec("check.relations", "read",
             "Prüft, ob gespeicherte Beziehungen/Constraints noch sinnvoll sind.",
             "object(s)"),

    # ---------------------------------------------------------------- file
    ToolSpec("file.snapshot", "low",
             "Sichert die aktuelle .blend in den Job-Ordner (vor riskanten Änderungen).",
             "label, include_preview"),
    ToolSpec("file.restore", "high",
             "Stellt eine Sicherung wieder her. Alle Änderungen seit der Sicherung gehen verloren.",
             "snapshot, confirm", mutating=True),
    ToolSpec("file.list_snapshots", "read", "Liste der Sicherungen mit Zeit, Größe und Label."),
    ToolSpec("file.save", "moderate", "Speichert die geöffnete .blend (optional unter neuem Pfad).",
             "path, copy", mutating=True),
    ToolSpec("file.export", "low",
             "Exportiert als glTF/GLB oder FBX für Roblox Studio.",
             "object(s), format, path, options"),
    ToolSpec("file.artifact", "read", "Liest eine erzeugte Datei (z. B. PNG) als Base64 zurück.",
             "path, max_bytes"),

    # -------------------------------------------------------------- script
    ToolSpec("script.run", "critical",
             "Führt beliebigen Python-Code in Blender aus. Standardmäßig aus, "
             "verlangt ausdrückliche Freigabe und wird vollständig protokolliert.",
             "code, description, timeout_s",
             mutating=True,
             approval_hint="Voller Zugriff auf Blender und damit auf deinen Benutzeraccount."),
]


TOOLS: Dict[str, ToolSpec] = {s.name: s for s in _SPECS}

#: Tools that only exist between connector and add-on (never reachable over HTTP).
INTERNAL_TOOLS = {
    "_control.ping": ToolSpec("_control.ping", "read", "Keepalive", internal=True),
    "_control.cancel": ToolSpec("_control.cancel", "read", "Bricht einen laufenden Job ab",
                                internal=True),
    "_control.status": ToolSpec("_control.status", "read", "Add-on Status", internal=True),
}


def get(name: str) -> Optional[ToolSpec]:
    return TOOLS.get(name) or INTERNAL_TOOLS.get(name)


def risk_of(name: str, default: str = "high") -> str:
    spec = get(name)
    return spec.risk if spec else default


def is_mutating(name: str) -> bool:
    spec = get(name)
    return bool(spec and spec.mutating)


def catalogue() -> List[Dict[str, Any]]:
    return [s.to_dict() for s in _SPECS]


def names() -> List[str]:
    return sorted(TOOLS)


def namespace_groups() -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for name in names():
        ns = name.split(".", 1)[0]
        out.setdefault(ns, []).append(name)
    return out

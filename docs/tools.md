# Werkzeug-Referenz

Alle **67** Werkzeuge, die der Connector anbietet. Diese Datei wird aus
`blenderbridge/tools.py` erzeugt (`python tools/gen_docs.py`) und ist damit
immer identisch mit dem, was ein Agent über `GET /v1/tools` sieht.

> Ein Agent kann **nur** diese Werkzeuge aufrufen. Es gibt keinen Endpunkt für
> beliebigen Python-Code außer `script.run`, und der ist dreifach gesperrt.


## Risikostufen

| Stufe | Bedeutung | Freigabe ab Schwelle `moderate` (Standard) |
|---|---|---|
| `read` | nur lesen | nein |
| `low` | fügt hinzu | nein |
| `moderate` | ändert Bestehendes | **ja** |
| `high` | löscht / Topologie | **ja** |
| `critical` | beliebiger Code | **ja** |

## Übersicht

| Namensraum | Werkzeuge |
|---|---|
| `scene` | Szene (5) |
| `bridge` | Brücke / Verbindung (3) |
| `object` | Objekte (16) |
| `mesh` | Mesh und Modellierung (11) |
| `region` | Benannte Bereiche (8) |
| `anchor` | Form-Anker (2) |
| `relation` | Räumliche Beziehungen (4) |
| `preview` | Vorschau-Rendering (2) |
| `check` | Prüfungen (3) |
| `file` | Dateien, Sicherungen, Export (6) |
| `script` | Skript-Modus (gesperrt) (1) |


## Szene (`scene.*`)

### `scene.frame`

**Risiko:** `read`

Aktueller Frame, Frame-Range, FPS.

### `scene.info`

**Risiko:** `read`

Blender-Version, geöffnete Datei, Einheiten, Collections, Objekt- und Polygonzahlen.

**Parameter:** detail: 'summary'|'full'

### `scene.set_frame`

**Risiko:** `low` _verändert die Szene_

Setzt den aktuellen Frame.

**Parameter:** frame: int

### `scene.stats`

**Risiko:** `read`

Gesamtzahlen: Objekte, Vertices, Flächen, Materialien, Größenextent der Szene.

### `scene.tree`

**Risiko:** `read`

Hierarchie aus Collections und Objekten mit Typ, Parent und Kinderzahl.

**Parameter:** root: optionaler Collectionname, depth: int


## Brücke / Verbindung (`bridge.*`)

### `bridge.info`

**Risiko:** `read`

Version und Fähigkeiten des Add-ons, Blender-Version, aktive Einstellungen.

### `bridge.ping`

**Risiko:** `read`

Lebenszeichen inkl. Latenz.

### `bridge.selection`

**Risiko:** `read`

Aktuelle Auswahl in Blender: Objekte, Bearbeitungsmodus, ausgewählte Vertices.


## Objekte (`object.*`)

### `object.add_constraint`

**Risiko:** `moderate` _verändert die Szene_

Fügt einen Constraint hinzu (Child Of, Copy Transforms, Damped Track …).

**Parameter:** object, type, target, options

### `object.add_modifier`

**Risiko:** `moderate` _verändert die Szene_

Fügt einen Modifier hinzu (Bevel, Subsurf, Solidify, Mirror, Array, Shrinkwrap …).

**Parameter:** object, type, options, name

### `object.apply_transform`

**Risiko:** `moderate` _verändert die Szene_

Wendet Rotation/Skalierung an (wichtig für Roblox-Export).

**Parameter:** object(s), location, rotation, scale

### `object.create_primitive`

**Risiko:** `low` _verändert die Szene_

Erzeugt Würfel/Kugel/Zylinder/Kegel/Torus/Plane/Ebene-Kurve mit Namen und Transform.

**Parameter:** kind, name, location, rotation_deg, scale, collection, dimensions

### `object.delete`

**Risiko:** `high` _verändert die Szene_

Löscht Objekte. Sicherung vorher empfohlen.

**Parameter:** object(s), confirm_count: int

**Freigabehinweis:** Gelöschte Objekte sind nur über eine Sicherung wiederherstellbar.

### `object.duplicate`

**Risiko:** `low` _verändert die Szene_

Dupliziert Objekte (verlinkt oder als Kopie) an eine neue Position.

**Parameter:** object(s), count, offset, linked: bool

### `object.get`

**Risiko:** `read`

Detail zu einem Objekt: Transform, Mesh-Statistik, Modifier, Constraints, Materialien, Vertex-Gruppen, gespeicherte Tags.

**Parameter:** object: str

### `object.list`

**Risiko:** `read`

Alle Objekte mit Typ, Collection, sichtbarem Namen und Kennzahlen.

**Parameter:** type: optional Filter, collection: optional, limit: int

### `object.modifier_action`

**Risiko:** `moderate` _verändert die Szene_

Ändert/entfernt/wendet Modifier an.

**Parameter:** object, modifier, action, options

### `object.move_to_collection`

**Risiko:** `low` _verändert die Szene_

Verschiebt Objekte in eine Collection (erzeugt sie bei Bedarf).

**Parameter:** object(s), collection, create: bool

### `object.parent`

**Risiko:** `moderate` _verändert die Szene_

Setzt/entfernt Parenting inkl. 'keep transform'.

**Parameter:** object(s), parent, keep_transform

### `object.probe`

**Risiko:** `read`

Vermessung: Bounding-Box, Maße, Achsen, Anker, Oberflächennormale, Teile. Grundlage für räumliche Beziehungen.

**Parameter:** object, part, include_paths

### `object.rename`

**Risiko:** `low` _verändert die Szene_

Benennt Objekte um.

**Parameter:** object, name

### `object.set_origin`

**Risiko:** `moderate` _verändert die Szene_

Setzt den Objektursprung (Pivot) auf Geometrie/3D-Cursor/Bounding-Box-Punkt.

**Parameter:** object(s), mode: 'geometry'|'cursor'|'bbox_bottom'|'bbox_center'

### `object.transform`

**Risiko:** `moderate` _verändert die Szene_

Setzt oder ändert Position/Rotation/Skalierung, absolut oder relativ.

**Parameter:** object(s), location, rotation_deg, scale, relative: bool

**Freigabehinweis:** Bestehende Positionen können überschrieben werden.

### `object.vertex_group`

**Risiko:** `low` _verändert die Szene_

Erzeugt/ändert Vertex-Gruppen (Gewichte) für lokale Einflüsse.

**Parameter:** object, group, mode, vertices


## Mesh und Modellierung (`mesh.*`)

### `mesh.edit`

**Risiko:** `moderate` _verändert die Szene_

Lokale Verformung mit weichem Einfluss: move, inflate, scale, taper, bend, smooth, flatten, noise. Unterstützt geschützte Bereiche.

**Parameter:** object, region(s), op, amount/direction/axis …, profile, protect

**Freigabehinweis:** Verändert bestehende Geometrie. Vorher wird bei Bedarf gesichert.

### `mesh.extrude`

**Risiko:** `moderate` _verändert die Szene_

Zieht aus einem Bereich neue Geometrie heraus (Äste, Arme, Fortsätze).

**Parameter:** object, region, distance, direction, taper, segments

### `mesh.inspect`

**Risiko:** `read`

Technische Mesh-Analyse: Punkte/Kanten/Flächen, Inseln, N-Gons, nicht-mannigfaltige Kanten, Normalen, UVs, Vertex-Farben, Materialien.

**Parameter:** object, max_islands

### `mesh.join`

**Risiko:** `high` _verändert die Szene_

Fügt Objekte zu einem Mesh zusammen.

**Parameter:** objects, name

### `mesh.merge_by_distance`

**Risiko:** `moderate` _verändert die Szene_

Verschmilzt doppelte Punkte.

**Parameter:** object, distance

### `mesh.points`

**Risiko:** `read`

Liefert Vertex-Koordinaten (gefiltert nach Region/Bereich/Stichprobe).

**Parameter:** object, region, bbox, limit, offset

### `mesh.separate`

**Risiko:** `moderate` _verändert die Szene_

Trennt Bereiche/Inseln/Materialien in eigene Objekte.

**Parameter:** object, mode, region

### `mesh.shade`

**Risiko:** `low` _verändert die Szene_

Setzt Shading (smooth/flat) und Auto-Smooth.

**Parameter:** object, mode, angle

### `mesh.subdivide`

**Risiko:** `moderate` _verändert die Szene_

Unterteilt ausgewählte Flächen/Kanten für mehr Detail.

**Parameter:** object, region, cuts, smoothness

### `mesh.uv`

**Risiko:** `moderate` _verändert die Szene_

Erzeugt/aktualisiert UVs (Smart Project, Unwrap, Box).

**Parameter:** object, mode, angle_limit

### `mesh.validate`

**Risiko:** `read`

Prüft, ob ein Mesh für Export/Roblox brauchbar ist.


## Benannte Bereiche (`region.*`)

### `region.check`

**Risiko:** `read`

Prüft, ob gespeicherte Bereiche noch zur aktuellen Topologie passen (Staleness).

### `region.create`

**Risiko:** `low` _verändert die Szene_

Legt einen benannten Bereich an: aus Auswahl, Box, Kugel, Achsenband, Höhenanteil, Material oder loser Insel. Optional mit weichem Übergang.

**Parameter:** object, name, source, spec, soft, profile, protected

### `region.delete`

**Risiko:** `low` _verändert die Szene_

Löscht einen Bereich (und seine Vertex-Gruppe).

**Parameter:** object, name

### `region.from_selection`

**Risiko:** `low` _verändert die Szene_

Übernimmt die aktuell in Blender ausgewählten Vertices als Bereich.

**Parameter:** object, name, soft

### `region.get`

**Risiko:** `read`

Details eines Bereichs inkl. Gewichtsverteilung.

### `region.list`

**Risiko:** `read`

Alle gespeicherten Bereiche/Teile eines Objekts.

### `region.remap`

**Risiko:** `moderate` _verändert die Szene_

Repariert einen veralteten Bereich nach Topologieänderungen.

**Parameter:** object, name, strategy

### `region.update`

**Risiko:** `low` _verändert die Szene_

Ändert Metadaten/Schutz/Notiz eines Bereichs.

**Parameter:** object, name, note, protected, kind


## Form-Anker (`anchor.*`)

### `anchor.list`

**Risiko:** `read`

Alle Anker eines Objekts.

### `anchor.set`

**Risiko:** `low` _verändert die Szene_

Speichert einen benannten Form-Anker (Punkt + Richtung).

**Parameter:** object, name, position, direction


## Räumliche Beziehungen (`relation.*`)

### `relation.apply`

**Risiko:** `moderate` _verändert die Szene_

Wendet eine berechnete Beziehung auf Objekte an (einmalige Platzierung).

**Parameter:** object(s), placement, target(s), rotation, size

### `relation.distribute`

**Risiko:** `moderate` _verändert die Szene_

Verteilt Kopien gleichmäßig entlang einer Kante/eines Verlaufs/zwischen Punkten.

**Parameter:** object, count, target, path, between

### `relation.persist`

**Risiko:** `moderate` _verändert die Szene_

Macht eine Beziehung dauerhaft: Parenting, Constraints oder Shrinkwrap.

**Parameter:** object(s), target, mode, options

### `relation.resolve`

**Risiko:** `read`

Berechnet aus einer beschriebenen Beziehung (auf/neben/zwischen/entlang) Position, Rotation und Größe - ohne etwas zu verändern.

**Parameter:** placement, target(s), anchor, rotation, size, gap


## Vorschau-Rendering (`preview.*`)

### `preview.render`

**Risiko:** `low`

Rendert Ansichten (perspektive, vorne, seite, oben, ausgewählt, Region) als PNG in den Job-Ordner. Nutzt vorhandene Kamera oder eine temporäre.

**Parameter:** views, object, region, resolution, engine, samples, wireframe

### `preview.snapshot_selection`

**Risiko:** `read`

Beschreibt die aktuelle Auswahl so, dass ein Agent sie nachvollziehen kann.


## Prüfungen (`check.*`)

### `check.relations`

**Risiko:** `read`

Prüft, ob gespeicherte Beziehungen/Constraints noch sinnvoll sind.

**Parameter:** object(s)

### `check.roblox`

**Risiko:** `read`

Roblox-Vorbereitung: Maße in Studs, Ursprung, Objektaufteilung, Polygonbudget, Material-/UV-Hinweise, Export-Empfehlung.

**Parameter:** object(s)

### `check.technical`

**Risiko:** `read`

Technische Prüfung: nicht-mannigfaltig, N-Gons, lose Geometrie, Normalen, angewandte Skalierung, Materialzahl, UVs, Polygonbudget.

**Parameter:** object(s), budget


## Dateien, Sicherungen, Export (`file.*`)

### `file.artifact`

**Risiko:** `read`

Liest eine erzeugte Datei (z. B. PNG) als Base64 zurück.

**Parameter:** path, max_bytes

### `file.export`

**Risiko:** `low`

Exportiert als glTF/GLB oder FBX für Roblox Studio.

**Parameter:** object(s), format, path, options

### `file.list_snapshots`

**Risiko:** `read`

Liste der Sicherungen mit Zeit, Größe und Label.

### `file.restore`

**Risiko:** `high` _verändert die Szene_

Stellt eine Sicherung wieder her. Alle Änderungen seit der Sicherung gehen verloren.

**Parameter:** snapshot, confirm

### `file.save`

**Risiko:** `moderate` _verändert die Szene_

Speichert die geöffnete .blend (optional unter neuem Pfad).

**Parameter:** path, copy

### `file.snapshot`

**Risiko:** `low`

Sichert die aktuelle .blend in den Job-Ordner (vor riskanten Änderungen).

**Parameter:** label, include_preview


## Skript-Modus (gesperrt) (`script.*`)

### `script.run`

**Risiko:** `critical` _verändert die Szene_

Führt beliebigen Python-Code in Blender aus. Standardmäßig aus, verlangt ausdrückliche Freigabe und wird vollständig protokolliert.

**Parameter:** code, description, timeout_s

**Freigabehinweis:** Voller Zugriff auf Blender und damit auf deinen Benutzeraccount.


## Antwortformat

Jedes Werkzeug antwortet mit demselben Rahmen (siehe `docs/protocol.md`):

```json
{
  "ok": true,
  "tool": "object.probe",
  "request_id": "req_9f3...",
  "data": { ... },
  "warnings": [],
  "timing": {"ms": 12.4}
}
```

Fehler:

```json
{
  "ok": false,
  "error": {
    "code": "region_incomplete",
    "message": "Im Bereich liegen keine vollständigen Flächen ...",
    "details": {"region": "stamm"}
  }
}
```

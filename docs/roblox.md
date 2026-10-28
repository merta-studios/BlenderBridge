# Von Blender nach Roblox Studio

Dieses Dokument trennt drei Dinge strikt:

1. **Was das Programm geprüft hat** (Messwerte, keine Meinungen),
2. **Empfehlungen** (begründet, aber nicht erzwungen),
3. **Was du in Studio selbst prüfen musst** (kann niemand von außen sehen).

---

## 1. Die Einheiten

Roblox rechnet in **Studs**. Blender rechnet in Metern (Standard-Szene).
BlenderBridge rechnet mit **1 Stud = 0,28 m**.

* `check.roblox` gibt beide Werte aus.
* `check.technical` prüft zusätzlich die Blender-Seite (Skalierung, Non-Manifold,
  N-Gons, UVs, Dreieckszahl).
* Der Faktor ist in `blenderbridge/geometry.py` als Konstante hinterlegt; wenn
  dein Projekt anders skaliert, sag es dem Agenten ausdrücklich.

---

## 2. Was geprüft wird (nachvollziehbar)

| Prüfung | Werkzeug | Aussage |
|---|---|---|
| Maße in m und Studs | `object.probe`, `check.roblox` | Bounding-Box des Objekts (Weltkoordinaten) |
| Pivot/Ursprung | `object.probe` (`origin_inside`, `pivot`) | Liegt der Ursprung in der Geometrie? |
| Angewandte Skalierung | `object.get`, `check.technical` | `scale != (1,1,1)` ist beim Export brandgefährlich |
| Dreieckszahl | `check.technical` | Flächen → Dreiecke (N-Gons gezählt) |
| N-Gons | `check.technical` | Für Roblox immer triangulieren lassen |
| Non-Manifold-Kanten | `check.technical` | Löcher, offene Kanten, doppelte Flächen |
| UV-Maps | `check.technical` | Vorhanden? Anzahl Layer? |
| Materialien | `object.get` | Namen, Basisfarbe, Rauheit, Texturen |

Beispielausgabe (`check.roblox`):

```json
{
  "objects": [
    {
      "name": "Palme",
      "dimensions_m": [4.1, 4.1, 8.0],
      "dimensions_studs": [14.6, 14.6, 28.6],
      "vertex_count": 1820,
      "triangle_count": 3620,
      "triangles_hint": "Prop (≤ 1.500) überschritten, Gebäude (≤ 12.000) ok",
      "pivot": {"inside": true, "location": [0, 0, 4.0]},
      "scale": [1.0, 1.0, 1.0]
    }
  ],
  "budget": {"total_triangles": 4210, "rating": "ok"},
  "warnings": []
}
```

Zahlen sind echte Messungen aus deiner Szene – keine Schätzungen.

---

## 3. Empfehlungen (kein Automatismus)

### Größe

* Eine Spielfigur ist ca. **5 Studs** hoch (1,4 m). Eine Tür ist ca. **7 Studs**.
* Prüfe dein Modell gegen eine Referenzfigur, bevor du es exportierst.
  `check.roblox` rechnet dir die Studs aus; ob sie *stimmig* sind, entscheidest du.

### Pivot

Ein guter Pivot liegt an der **Basis** des Objekts, mittig – dann lässt es sich in
Studio sauber auf den Boden setzen und drehen.

```
object.set_origin  {"object": "Palme", "mode": "bbox_bottom"}
```

### Skalierung und Rotation anwenden

```
object.apply_transform  {"object": "Palme", "location": false, "rotation": true, "scale": true}
```

Nicht angewendete Skalierung führt beim glTF-Export zu überraschenden Größen in
Studio.

### Aufteilen

Ein Modell mit 30 000 Dreiecken lässt sich in Studio kaum bearbeiten. Teile
sinnvoll:

* Palme: `Trunk`, `Leaves_L1..L4`, `Coconuts`
* Gebäude: `Walls`, `Roof`, `Windows`, `Door`

```
mesh.separate  {"object": "Palme", "mode": "material"}   // oder "islands", "region"
```

Danach hat jedes Teil einen eigenen Pivot und kann einzeln verschoben werden.

### Dreiecks-Budget (Anhaltspunkte, keine Regeln)

| Kategorie | Dreiecke |
|---|---|
| kleines Prop (Tasse, Keks) | ≤ 500 |
| Prop (Stuhl, Kiste) | ≤ 1 500 |
| Detail-Prop | ≤ 5 000 |
| Gebäude-Modul | ≤ 12 000 |
| Hero-Asset | ≤ 30 000 |

`check.technical` markiert Überschreitungen, blockiert aber nichts.

### Materialien und Texturen

* Roblox nutzt `SurfaceAppearance` (PBR: Albedo, Normal, Roughness, Metalness)
  oder einfache `Color3` + `Material`.
* glTF-PBR wird beim Import **teilweise** übernommen; komplexe Node-Setups
  (Blender-Shader mit Mischknoten) gehen verloren.
* Empfehlung: ein Material pro sichtbarem Teil, einfache Principled-BSDF-Werte
  (Basisfarbe, Rauheit, Metall), Texturen als PNG in Zweierpotenzen
  (512, 1024, 2048).
* UVs: `mesh.uv` mit `mode: "box"` (Standard, funktioniert immer) oder `"smart"`
  reicht für farbige Flächen; für gemalte Texturen brauchst du echtes Unwrapping
  (`mode: "unwrap"` mit gesetzten Nähten) – das ist Handarbeit.

### Export

```
file.export  {"format": "glb", "objects": ["Palme"], "path": "palme.glb"}
```

* **GLB** ist der zuverlässigste Weg: ein Datei-Paket, inklusive Texturen.
* FBX geht auch, ist aber empfindlicher (Achsen, Skalierung, Einheiten).
* Der Export landet im Ausgabeordner des Jobs und ist über
  `/v1/jobs/<id>/artifacts/<name>` herunterladbar – auch über den Relay.

### Kollisionen

Im Export gibt es keine Kollisionsdaten. In Studio:

* `CanCollide` nur für Teile, die wirklich blockieren sollen.
* `CollisionFidelity`: `Box` (billig, ungenau) für einfache Formen,
  `Hull` für konvexe, `PreciseConvexDecomposition` nur wo nötig (teuer).
* Für Bäume: Stamm kollidierbar, Blätter nicht.

---

## 4. Was du in Studio selbst prüfen musst

Das kann **kein** externes Programm für dich tun:

- [ ] **Maßstab** gegen eine Spielfigur (Avatar neben das Modell stellen).
- [ ] **Kollisionen**: Laufen, Springen, Hängenbleiben.
- [ ] **Materialwirkung**: Beleuchtung in Studio ist anders als in Blender.
- [ ] **Performance**: viele Teile + hohe Dreieckszahlen + Streaming.
- [ ] **Pivot im Einsatz**: Drehen, Aufstellen, Werkzeug-Ankerpunkte.
- [ ] **Streaming**: `StreamingEnabled` bei großen Karten.
- [ ] **Rechte**: Import als Gruppe/User, nicht als persönliches Asset, wenn
      andere es nutzen sollen.

---

## 5. Typischer Ablauf in einem Auftrag

```jsonc
// 1) prüfen, was da ist
{"tool": "check.roblox", "params": {"object": "Palme"}}
// 2) Pivot an die Basis, Skalierung anwenden
{"tool": "object.set_origin", "params": {"object": "Palme", "mode": "bbox_bottom"}}
{"tool": "object.apply_transform", "params": {"object": "Palme", "scale": true, "rotation": true}}
// 3) in Teile trennen
{"tool": "mesh.separate", "params": {"object": "Palme", "mode": "material"}}
// 4) exportieren
{"tool": "file.export", "params": {"format": "glb", "path": "palme.glb",
                                     "objects": ["Palme_Trunk", "Palme_Leaves"]}}
// 5) Bilder für den Vergleich
{"tool": "preview.render", "params": {"views": ["perspective", "front"], "resolution": 800}}
```

Danach: Datei herunterladen, in Studio importieren, den Maßstab prüfen und –
falls nötig – den Agenten mit **einer konkreten Zahl** korrigieren lassen
(„der Stamm ist doppelt so dick wie gewünscht“).

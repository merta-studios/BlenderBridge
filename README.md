# BlenderBridge

**Ein lokaler Connector, der einem KI-Agenten kontrollierten Zugriff auf deine
geöffnete Blender-Szene gibt** – mit Freigaben, Sicherungen, Vorschau-Renderings,
benannten Mesh-Bereichen und räumlichen Beziehungen („auf der Palme“, „10 % der
Höhe“) statt blind geratener Koordinaten.

Du startest ein Windows-Programm per Doppelklick, installierst einmal das
Blender-Add-on, und ein Agent kann danach:

* die Szene **untersuchen** (Objekte, Meshes, Materialien, Maße, Hierarchie),
* **innerhalb eines Meshes** arbeiten (Bereiche wie „linke Wange“ benennen,
  weich verformen, schützen, extrudieren),
* **Beziehungen** berechnen und anwenden (auf/neben/zwischen/entlang, Größe
  relativ zum Zielobjekt),
* **Vorschaubilder rendern** und technische Prüfungen fahren,
* **gezielt überarbeiten** statt alles neu zu bauen – mit Sicherung vorher.

---

## Inhalt

1. [Was funktioniert – und was nicht](#1-was-funktioniert--und-was-nicht)
2. [Voraussetzungen](#2-voraussetzungen)
3. [Die EXE herunterladen und starten](#3-die-exe-herunterladen-und-starten)
4. [Blender-Add-on installieren](#4-blender-add-on-installieren)
5. [Verbindung prüfen](#5-verbindung-prüfen)
6. [Erster Modellier-Ablauf](#6-erster-modellier-ablauf)
7. [Freigaben, Sicherungen, Wiederherstellung](#7-freigaben-sicherungen-wiederherstellung)
8. [Externer Zugang (Cloudflare / Relay)](#8-externer-zugang-cloudflare--relay)
9. [Sicherheit – bitte lesen](#9-sicherheit--bitte-lesen)
10. [Roblox Studio](#10-roblox-studio)
11. [Häufige Fehler](#11-häufige-fehler)
12. [Bekannte Grenzen](#12-bekannte-grenzen)
13. [Entwicklung und Tests](#13-entwicklung-und-tests)
14. [Projektstruktur](#14-projektstruktur)

---

## 1. Was funktioniert – und was nicht

### Implementiert und getestet

| Bereich | Umfang |
|---|---|
| **Connector (Windows)** | Web-Oberfläche, Job-Queue, Freigaben, Not-Aus, Audit-Log, Agent-Schlüssel, Rate-Limit, konfigurierbare Risikoschwellen |
| **Blender-Add-on** | 67 Werkzeuge über 10 Namensräume, Socket-Server nur auf `127.0.0.1`, Ausführung ausschließlich im Blender-Hauptthread |
| **Szenen-Analyse** | Objekte, Collections, Meshes (Punkte/Kanten/Flächen/Inseln/N-Gons/nicht-mannigfaltig), Materialien, UVs, Modifier, Constraints, Maße, dominante Achse, Oberflächennormale, Anker |
| **Mesh-Bereiche** | Auswahl/Box/Kugel/Achsenband/Höhenband/Material/Insel, weicher Einfluss, geschützte Bereiche, Vertex-Gruppen-Spiegelung, Fingerprint gegen veraltete Zuordnungen, Reparatur |
| **Lokale Verformung** | `move, inflate, scale, taper, bend, smooth, flatten, noise, stretch, pinch`, Extrusion aus einem Bereich, Unterteilen, Verschmelzen, Trennen, Zusammenfügen |
| **Räumliche Beziehungen** | 12 Platzierungen, 7 Rotationsmodi, 4 Größenmodi, Verteilung entlang Kante/Pfad, dauerhafte Beziehungen (Parent, Vertex-Parent, Constraints, Shrinkwrap), explizite Annahmen + Rückfragen bei Mehrdeutigkeit |
| **Visuelle Kontrolle** | Renderings aus 8 Blickrichtungen, Objekt-/Bereichsfokus, Isolieren, Engine-Fallback, echte PNG-Dateien über die API abrufbar |
| **Prüfungen** | Technik (Non-Manifold, N-Gons, Skalierung, UVs, Budget) und Roblox-Vorbereitung (Studs, Pivot, Tri-Zahl, Export) |
| **Sicherung** | `.blend`-Schnappschüsse vor jeder Änderung, Index, automatische Aufräumung, Wiederherstellung mit zusätzlicher Sicherung |
| **Externer Zugang** | Cloudflare Quick Tunnel (optional, aus per Default, mit Erreichbarkeits-Selbsttest) **und** Relay über ein GitHub-Issue für Umgebungen, die nur GitHub erreichen |
| **Skript-Modus** | `script.run` – dreifach gesperrt (Connector-Einstellung, Freigabe, Add-on-Schalter), vollständig protokolliert |
| **Build** | GitHub-Actions-Workflow baut `BlenderBridge.exe`, prüft sie mit einem Smoke-Test und packt EXE + Add-on-ZIP zusammen |

### Ehrlich: was nicht (vollständig) geht

* **Keine automatische semantische Erkennung** fremder Modelle. Das System weiß
  nicht von selbst, wo „die Wange“ ist. Bereiche legst **du** an (Auswahl im
  Viewport, Box, Kugel, Höhenband) – oder der Agent schlägt sie vor und du
  bestätigst. Das ist Absicht: raten wäre schlimmer als fragen.
* **Kein Sculpting-Ersatz.** Die Verformungen arbeiten auf Vertex-Positionen mit
  weichem Einfluss. Für organische High-End-Formen ist Blender-Sculpting weiter
  besser; das Programm kann aber gezielt Bereiche vorbereiten und nachbearbeiten.
* **Retopologie, UV-Packing in Profiqualität, Rigging** sind nicht abgedeckt.
  UVs gibt es (Box/Smart/Unwrap), aber ohne Qualitätsgarantie.
* **Rücknahme ist nicht perfekt.** Blender-Undo (Strg+Z) greift für die meisten
  Aktionen; für alles darüber hinaus zählt die Sicherung vorher. Wenn eine
  Sicherung fehlschlägt, bricht der Connector riskante Aktionen ab.
* **Starke Topologieänderungen** (Extrudieren, Unterteilen, Zusammenfügen)
  machen gespeicherte Bereiche ungültig. Das wird erkannt und gemeldet, die
  Reparatur ist eine Schätzung.
* **Rendering blockiert Blender kurz.** Das ist eine Blender-Eigenschaft, keine
  Wahl. Deshalb sind Vorschauen klein (Standard 640 px, 16 Samples).
* **Die EXE ist nicht signiert.** Windows wird warnen (siehe Abschnitt 3).

---

## 2. Voraussetzungen

| Was | Version |
|---|---|
| Windows | 10 oder 11 (64 Bit) |
| Blender | **4.0 bis 4.5** (entwickelt und getestet gegen 4.2 LTS) |
| Internet | nur für den Download; das Programm selbst arbeitet lokal |
| Python | **nicht nötig** – die EXE bringt alles mit |
| optional | `cloudflared.exe` für den externen Zugang |

Der Connector braucht kein Blender-Python und keine Installation. Er schreibt
seine Daten nach `%APPDATA%\BlenderBridge\` (Konfiguration, Logs, Sicherungen,
Vorschaubilder). Nichts davon verlässt deinen PC.

---

## 3. Die EXE herunterladen und starten

Es gibt zwei Wege. **Weg A (empfohlen für dich)** ist ein echter Release-Download
ohne GitHub-Login.

### Weg A: GitHub Release

1. Öffne `https://github.com/merta-studios/BlenderBridge/releases`.
2. Lade unter **Assets** die Datei
   `BlenderBridge-<version>-windows-x64.zip` herunter.
3. Entpacken (Rechtsklick → „Alle extrahieren“). Du erhältst:
   * `BlenderBridge.exe`
   * `blenderbridge_addon.zip`
   * `START-HERE.txt`, `README.md`
4. **`BlenderBridge.exe` doppelklicken.**

### Weg B: Actions-Artefakt

1. Öffne den Reiter **Actions** im Repository.
2. Wähle links **„Build Windows EXE“** und den neuesten grünen Lauf.
3. Unten unter **Artifacts**: `BlenderBridge-windows-x64` herunterladen
   (ZIP mit EXE, Add-on-ZIP und Kurzanleitung).
   Hinweis: Artefakte sind nur 90 Tage verfügbar und der Download verlangt
   einen GitHub-Login.

### Was beim Start passiert

Es öffnet sich ein Konsolenfenster (dort stehen Status, Port und Fehler) **und**
der Browser mit der Oberfläche `http://127.0.0.1:8765`.

* Das Konsolenfenster **offen lassen** – es ist das Programm.
* Beenden: `Strg+C` im Konsolenfenster oder Fenster schließen.

> **Windows-SmartScreen:** Die EXE ist nicht digital signiert (ein
> Code-Signing-Zertifikat kostet Geld). Windows zeigt deshalb eventuell
> „Windows hat den Start dieser App verhindert“. Klicke auf
> **„Weitere Informationen“ → „Trotzdem ausführen“**. Wenn du das nicht willst,
> kannst du das Programm auch direkt aus dem Quellcode starten
> (`python -m blenderbridge`), siehe Abschnitt 13.

### Selbsttest

```
BlenderBridge.exe --selftest
```

prüft die gesamte Kette ohne Blender (Protokoll, Verschlüsselung, Freigaben,
Job-Engine, HTTP-API, Artefakt-Download) und endet mit „Alles in Ordnung“.
Genau das führt auch der GitHub-Workflow nach dem Build aus.

---

## 4. Blender-Add-on installieren

1. Blender starten.
2. **Edit → Preferences → Add-ons → Install…**
3. `blenderbridge_addon.zip` auswählen (nicht entpacken!).
4. Häkchen bei **BlenderBridge** setzen.
5. Im 3D-Fenster **`N`** drücken → Reiter **BlenderBridge** →
   **„Server starten“**.

Der Reiter zeigt danach Port, Anzahl Anfragen und ein kurzes Protokoll.
Der gemeinsame Token wird automatisch zwischen Connector und Add-on
ausgetauscht (Datei `addon_token.txt` im Konfigurationsordner). Falls das
einmal nicht klappt, kannst du ihn im Reiter **„Verbindung & Agent“** kopieren
und im Add-on-Panel einfügen.

**Wichtig:** Der Add-on-Server hört **nur** auf `127.0.0.1`. Er ist aus dem
Internet nicht erreichbar – den Außenkontakt macht ausschließlich der Connector,
und nur wenn du ihn einschaltest.

---

## 5. Verbindung prüfen

Im Browserfenster oben:

* **Connector** – grün, sobald die Oberfläche läuft.
* **Blender** – grün, wenn das Add-on antwortet; darunter Blender-Version und
  Dateipfad.
* **Externer Zugang** – bleibt grau, solange du ihn nicht einschaltest.

Zusätzlich im Add-on-Panel: „Anfragen“ zählt hoch, sobald der Connector
Verbindung hält (Keepalive alle 5 s).

Wenn Blender rot bleibt:

1. Add-on aktiviert und „Server starten“ geklickt?
2. Port belegt? Dann in Blender den Port ändern (z. B. 9877) und im Connector
   unter Einstellungen `addon_port` anpassen (oder Neustart mit
   `--addon-port 9877`).
3. Token: Reiter „Verbindung & Agent“ → „Token kopieren“ → im Add-on einfügen.

---

## 6. Erster Modellier-Ablauf

Das ist der Ablauf, für den das System gebaut ist. Er funktioniert genauso mit
einem Agenten, der die API benutzt, wie mit dir von Hand (siehe
`docs/protocol.md`).

```
1. scene.info / object.list      → Was ist da? (Agent versteht die Szene)
2. object.probe "Palm"           → Maße, Anker, Achse, Teile
3. region.list / region.create   → "Stamm", "linke Wange" benennen
4. mesh.edit / mesh.extrude      → lokal verändern, Geschütztes bleibt stehen
5. preview.render                → 2–3 Blickwinkel ansehen
6. check.technical / check.roblox→ Technik und Roblox-Vorbereitung
7. file.snapshot                 → vor dem nächsten großen Schritt sichern
8. file.export format="glb"      → für Roblox Studio
```

### Beispiel: Palme überarbeiten (ohne sie neu zu bauen)

Der Agent würde in etwa so vorgehen (Werkzeugaufrufe gekürzt):

```jsonc
// 1) verstehen
{"tool": "object.probe", "params": {"object": "Palme"}}
// 2) Stamm als Bereich markieren (Höhenband 0–60 %)
{"tool": "region.create", "params": {"object": "Palme", "name": "stamm",
   "source": "height", "range": [0.0, 0.6]}}
// 3) oberes Drittel stärker biegen, unten bleibt es stehen
{"tool": "mesh.edit", "params": {"object": "Palme", "region": "stamm",
   "op": "bend", "angle_deg": 25, "axis": [0,0,1], "direction": [1,0,0],
   "t_range": [0.4, 1.0]}}
// 4) nach oben schmaler
{"tool": "mesh.edit", "params": {"object": "Palme", "region": "stamm",
   "op": "taper", "start_factor": 1.0, "end_factor": 0.55}}
// 5) Kokosnuss relativ platzieren: 10 % der Palmenhöhe, auf der Krone
{"tool": "relation.apply", "params": {"object": "Kokosnuss",
   "placement": "on", "target": {"object": "Palme", "anchor": "top"},
   "size": {"mode": "relative", "fraction": 0.1}}}
// 6) anschauen
{"tool": "preview.render", "params": {"views": ["perspective", "front"],
   "resolution": 800}}
```

### Beispiel: „Mach die linke Wange voller, Augen nicht anfassen“

1. In Blender: in den Edit-Modus, Wangen-Punkte auswählen, Reiter
   **BlenderBridge → „Auswahl als Bereich“**, Name `linke_wange`.
2. Augen-Punkte auswählen → Bereich `augen` anlegen und **als geschützt**
   markieren (Häkchen im Bereichsdialog oder `region.update`).
3. Der Agent ruft:

```jsonc
{"tool": "mesh.edit", "params": {"object": "Kopf", "region": "linke_wange",
  "op": "inflate", "amount": 0.03, "protect": ["augen"]}}
```

Das Ergebnis: nur die Wangenregion wird dicker, weich auslaufend, die Augen
bleiben unverändert. `region.check` bestätigt anschließend, dass die
Zuordnungen noch gültig sind.

### „Auf der Palme“ – was das System daraus macht

`relation.resolve` liefert nicht nur Zahlen, sondern auch Annahmen und
Rückfragen:

```jsonc
{"placement": "on", "target": {"object": "Palme", "anchor": "tip"},
 "size": {"mode": "relative", "fraction": 0.1}}
```

Antwort (gekürzt):

```jsonc
{
  "center": [0.05, 0.0, 8.3],
  "scale": [2.67, 2.67, 2.67],
  "size_m": [0.8, 0.8, 0.8],
  "assumptions": ["scaled so the subject's height equals 10.0 % of the target (0.800 m), uniform scale x2.667"],
  "ambiguities": [{
    "topic": "size_dimension",
    "question": "10 % of *what*? Height, width, depth or volume of the target?",
    "default": "height (8.000 m)",
    "options": {"height": 8.0, "width": 0.8, "depth": 0.8, "volume": 5.12}
  }],
  "needs_confirmation": true
}
```

`needs_confirmation: true` heißt: der Connector zeigt das im Freigabe-Dialog,
bevor etwas verschoben wird.

---

## 7. Freigaben, Sicherungen, Wiederherstellung

### Risikostufen

Jedes Werkzeug hat eine Stufe. Ab der eingestellten Schwelle (Standard:
`moderate`) wartet der Auftrag im Reiter **Freigaben**:

| Stufe | Bedeutung | Standard |
|---|---|---|
| `read` | liest nur | automatisch |
| `low` | fügt Neues hinzu | automatisch |
| `moderate` | verändert Bestehendes | **Freigabe** |
| `high` | löscht / baut Topologie um | **Freigabe** |
| `critical` | `script.run` (beliebiger Python-Code) | **Freigabe, immer** |

Im Freigabe-Dialog siehst du Werkzeug, Risiko, Zusammenfassung, Notiz des
Agenten und die vollständigen Parameter. „Erlauben“ oder „Ablehnen“.

* **Vertrauensfenster:** „2 Minuten alles erlauben“ überspringt Freigaben
  befristet – für schnelle Iterationen, bewusst gestartet, sichtbar im Kopf.
* **Ablauf:** Ohne Antwort verfällt eine Freigabe nach 5 Minuten
  (`approval_timeout_s`) und wird **nicht** ausgeführt.

### Sicherungen

Vor jeder Aktion, die etwas verändert, legt das Add-on eine echte `.blend`-Kopie an:

* Ordner: neben deiner Projektdatei → `blenderbridge_backups/`
  (bei ungespeicherter Datei: `%USERPROFILE%\blenderbridge_backups\`)
* Dateiname: `20261028-153012_before mesh.edit.blend`
* Index: `index.json` mit Zeitpunkt, Label, Größe, Quelle
* automatische Aufräumung (Standard: die letzten 60)

Schlägt die Sicherung bei einer `high`/`critical`-Aktion fehl, **bricht der
Connector die Aktion ab**. Bei `moderate` wird sie mit Warnung ausgeführt.

### Wiederherstellen

* **Blender-Undo** (`Strg+Z`) – für die meisten Verformungen ausreichend.
* **Sicherung öffnen:** Datei in Blender öffnen, oder
  `file.restore` mit `{"snapshot": "...", "confirm": true}`.
  Dabei wird vorher automatisch eine Sicherung des *aktuellen* Standes
  angelegt, damit auch die Wiederherstellung umkehrbar ist.

**Was garantiert ist:** Dateien im Sicherungsordner sind vollständige
`.blend`-Dateien; sie lassen sich immer mit Blender öffnen.
**Was nicht garantiert ist:** dass nach der Wiederherstellung externe
Referenzen (Bilddateien, Libraries) noch stimmen – das hängt von deinem Projekt ab.

---

## 8. Externer Zugang (Cloudflare / Relay)

Standardmäßig **aus**. Beides braucht dein bewusstes Einschalten.

### Variante A: Cloudflare Quick Tunnel

Voraussetzung: `cloudflared.exe` liegt neben der EXE oder in einem Unterordner
`bin\`. Download und Prüfung: siehe `docs/security.md` (nur von
`github.com/cloudflare/cloudflared/releases`, SHA-256 vergleichen).

Ablauf: Oberfläche → **Übersicht → Externer Zugang → „Einschalten“**.
Der Connector startet `cloudflared tunnel --url http://127.0.0.1:8765` und zeigt
eine Adresse wie `https://silber-blatt-1234.trycloudflare.com`.

* Die Verbindung ist von außen nach Cloudflare **HTTPS**; Cloudflare tunnelt zu
  deinem Rechner. Der lokale Teil bleibt auf `127.0.0.1`.
* Die URL ist **öffentlich erreichbar**. Deshalb: jeder Aufruf braucht trotzdem
  einen gültigen Agent-Schlüssel (`Authorization: Bearer …`).
* Nach dem Einschalten prüft der Connector selbst, ob die URL wirklich
  antwortet, und zeigt das Ergebnis an. Wenn dein Netz ausgehende Tunnel
  blockiert, steht das dort – dann nutze Variante B.
* **Ausschalten:** Knopf „Ausschalten“ oder **NOT-AUS**. Beim Beenden des
  Programms wird der Tunnel immer beendet.

### Variante B: Relay über ein GitHub-Issue

Für Agenten, die nur eine Handvoll Hosts erreichen (z. B. nur GitHub). Der
Connector **fragt selbst** ein Issue ab und antwortet dort – kein offener Port,
keine eingehende Verbindung.

1. Privates Repository (oder ein Issue, das dir gehört) anlegen.
2. Feingranulares Token mit **Issues: Read and write** nur für dieses Repo.
3. Oberfläche → **Verbindung & Agent → Relay**: Repo `owner/name`,
   Issue-Nummer, Token eintragen, „Speichern“, „Verbindung testen“, „Relay starten“.
4. Der Agent schreibt verschlüsselte Anfragen als Kommentar und liest die
   Antwort ebenso. Format, Verschlüsselung und ein fertiges CLI:
   `docs/relay.md` und `tools/bb_relay_agent.py`.

Inhalte werden zusätzlich mit einem gemeinsamen Geheimnis versiegelt
(HMAC-basiert), damit sie nicht im Klartext im Issue stehen.

---

## 9. Sicherheit – bitte lesen

1. **Der Link ist keine Authentifizierung.** Auch über den Tunnel gilt:
   ohne Agent-Schlüssel kein Zugriff. Schlüssel werden gehasht gespeichert und
   nur einmal angezeigt.
2. **Externer Zugang ist aus, bis du ihn einschaltest**, und wird beim Beenden
   automatisch geschlossen.
3. **Risikoreiche Aktionen brauchen dein Ja.** Der Skript-Modus zusätzlich
   einen zweiten Schalter im Add-on.
4. **Not-Aus** (oben rechts): bricht alle Aufträge ab, schließt den Tunnel,
   beendet das Vertrauensfenster. Optional alle Schlüssel sperren.
5. **Alles wird protokolliert** (`%APPDATA%\BlenderBridge\logs\audit.jsonl`):
   Zeitpunkt, Werkzeug, Risiko, Status, Parameter (redigiert).
6. **Keine unnötigen Rechte.** Der Connector hört nur auf `127.0.0.1`, das
   Add-on ebenso. Es gibt keinen Endpunkt für Betriebssystem-Befehle.
   `script.run` ist der einzige Weg zu beliebigem Code – und der ist dreifach
   gesperrt.
7. **Blender-Python ist kein Sandbox.** Wenn du den Skript-Modus erlaubst, kann
   dieser Code alles, was Blender darf: Dateien lesen/schreiben, Prozesse
   starten. Das steht so auch im Freigabedialog.

Details, Bedrohungsmodell und Prüfliste: `docs/security.md`.

---

## 10. Roblox Studio

**Was das Programm prüft** (`check.roblox`, `check.technical`):

* Maße in Metern **und Studs** (1 Stud = 0,28 m)
* Pivot/Ursprung innerhalb der Geometrie?
* nicht angewendete Skalierung (geht beim Export oft verloren)
* Dreieckszahl gegen Richtwerte (Prop ≈ 1 500, Gebäude ≈ 12 000, Hero ≈ 30 000)
* UV-Maps vorhanden?
* N-Gons, nicht-mannigfaltige Kanten, degenerierte Flächen

**Empfehlungen** (nicht automatisch erzwungen):

* Objekte sinnvoll aufteilen (Turm, Dach, Fenster), damit du in Studio
  einzelne Teile bearbeiten kannst
* Pivot an die Basis legen (`object.set_origin` mit `bbox_bottom`)
* Rotation/Skalierung anwenden (`object.apply_transform`)
* Export als **GLB** (`file.export`), Import über den 3D-Importer

**Was du selbst in Studio prüfen musst:** tatsächliche Größe im Verhältnis zu
Spielfiguren, Kollisionen (`CollisionFidelity`, `CanCollide`), Materialwirkung
(Roblox nutzt `SurfaceAppearance`/`MaterialVariant`, nicht 1:1 glTF-PBR),
Streaming-Verhalten großer Modelle.

Mehr: `docs/roblox.md`.

---

## 11. Häufige Fehler

| Symptom | Ursache / Lösung |
|---|---|
| „Blender ist nicht verbunden“ | Add-on nicht aktiviert, kein „Server starten“, falscher Port (Standard 9876) |
| „ungültiger Token“ | Token kopieren (Reiter Verbindung & Agent) und im Add-on einfügen; danach neu starten |
| Oberfläche lädt nicht | Port 8765 belegt – der Connector nimmt automatisch den nächsten freien Port; die richtige Adresse steht im Konsolenfenster |
| Fenster schließt sofort | EXE aus einem geschützten Ordner gestartet oder von SmartScreen blockiert; Konsole zeigt die Ursache |
| „Sicherung fehlgeschlagen – Aktion abgebrochen“ | Zielordner nicht beschreibbar (z. B. Netzlaufwerk, OneDrive-Sperre). Projekt lokal speichern oder `snapshot_before_risky` in den Einstellungen anpassen |
| Vorschau bleibt schwarz | Szene ohne Licht/Kamera – das Add-on setzt temporär beides, aber ein sehr großer Maßstab oder Clip-End kann abschneiden. `preview.render` mit `object` fokussieren |
| „Rendering mit BLENDER_WORKBENCH statt CYCLES“ | Engine nicht verfügbar (kein GPU-Kontext) – Warnung steht im Ergebnis; Bild ist trotzdem gültig |
| Bereich „veraltet“ | Topologie wurde geändert. `region.remap` oder Bereich neu anlegen |
| Tunnel startet nicht | `cloudflared.exe` fehlt oder Netz blockiert ausgehende Tunnel. Relay verwenden |
| Relay „403“ | Token braucht **Issues: Read and write** für genau dieses Repository |
| Agent bekommt `401` | Schlüssel falsch/gesperrt. Neuen Schlüssel im UI erzeugen |
| Alles hängt | Ein Rendering läuft; Blender blockiert solange. `resolution`/`samples` senken |

Logs: `%APPDATA%\BlenderBridge\logs\connector.log` und `audit.jsonl`.
Selbsttest: `BlenderBridge.exe --selftest`.

---

## 12. Bekannte Grenzen

* Semantische Bereiche müssen von dir oder dem Agenten angelegt werden.
* Verformungen sind vertexbasiert; dreiecks-/topologieerhaltend, aber kein
  Ersatz für Sculpting oder Retopologie.
* Nach Topologieänderungen können Bereiche, UVs und Anker veralten – es wird
  gewarnt, nicht repariert um jeden Preis.
* Automatisches UV-Mapping ist brauchbar, aber nicht „sauber“.
* Beziehungen über Constraints/Parenting halten Änderungen am Ziel aus, aber
  nicht jede Kombination (z. B. Vertex-Parenting + starker Remesh).
* Der externe Zugang hängt vom Netzwerk ab. Cloudflare-Tunnel können blockiert
  sein; der Relay ist der robustere Weg für Agenten-Sandboxes.
* Keine automatische Modellqualitäts-Bewertung: die Renderings musst du (oder
  der Agent) tatsächlich ansehen.

---

## 13. Entwicklung und Tests

```bash
git clone https://github.com/merta-studios/BlenderBridge.git
cd BlenderBridge
python -m venv .venv && .venv/Scripts/activate     # Windows
pip install -r requirements-dev.txt

python -m blenderbridge.app --selftest      # Selbsttest ohne Blender
python -m pytest tests -q --ignore=tests/blender
```

Blender-Tests (echtes Blender als Python-Modul):

```bash
pip install "bpy==4.2.0"        # nur CPython 3.11
python -m pytest tests/blender -q
```

Add-on-ZIP bauen:

```bash
python tools/build_addon_zip.py      # -> dist/blenderbridge_addon.zip
```

EXE bauen (Windows):

```powershell
pip install pyinstaller
pyinstaller packaging/blenderbridge.spec --clean --noconfirm
.\dist\BlenderBridge.exe --selftest
```

Nach Änderungen an `blenderbridge/{protocol,geometry,regions,relations}.py`:

```bash
python tools/sync_shared.py     # kopiert in blenderbridge_addon/shared/
```

Ein Test schlägt fehl, wenn du das vergisst.

### Testabdeckung

| Datei | Prüft |
|---|---|
| `tests/test_protocol.py` | Validierung, Envelopes, IDs, Fehlercodes |
| `tests/test_geometry.py` | Vektor-/Matrixmathematik, Falloffs, Studs |
| `tests/test_relations.py` | alle Platzierungen, Größen-, Rotationsmodi, Mehrdeutigkeiten, Persistenz |
| `tests/test_regions.py` | Probes, Deformer, Fingerprints, Serialisierung |
| `tests/test_security.py` | Schlüssel, Policy, Rate-Limit, Redaktion, DPAPI-Fallback, Config |
| `tests/test_api.py` | vollständige HTTP-API gegen Fake-Blender: Auth, Rollen, CSRF, Host-Check, Freigaben, Abbruch, Artefakte, Not-Aus |
| `tests/test_relay.py` | Versiegelung, Chunking, GitHub-Client und Mailbox gegen einen Fake-Server |
| `tests/test_packaging.py` | Spec, Hidden-Imports, Add-on-ZIP-Inhalt, Workflow-Dateien |
| `tests/test_sync.py` | Add-on-Kopien identisch mit dem Quellcode |
| `tests/blender/test_blender_integration.py` | **echtes Blender 4.2**: Bereiche, Biegung, Verjüngung, Extrusion, geschützte Bereiche, Beziehungen, Rendering, Sicherung, Skript-Sperre, Socket-Protokoll |

---

## 14. Projektstruktur

```
blenderbridge/               Connector (Python, keine Abhängigkeiten)
  app.py                     Zusammenbau + CLI + Startbanner
  api.py                     HTTP-API + lokale Web-Oberfläche
  ui.html                    Oberfläche (eine Datei, kein Build)
  jobs.py                    Job-Queue, Freigaben, Timeouts, Artefakte
  proxy.py                   Socket-Verbindung zum Add-on
  security.py                Schlüssel, Policy, Redaktion, DPAPI
  tunnel.py                  Cloudflare-Tunnel
  relay.py                   GitHub-Issue-Postfach
  sealing.py                 Versiegelung (HMAC-Keystream + MAC)
  protocol.py  geometry.py  regions.py  relations.py
                             gemeinsam genutzter, reiner Python-Kern
  fakeaddon.py               Fake-Blender für Tests/Selbsttest
  selftest.py                `--selftest`

blenderbridge_addon/         Blender-Add-on (installierbar als ZIP)
  __init__.py                bl_info, Preferences, Operatoren
  server.py                  Socket-Server + Hauptthread-Queue
  helpers.py  facts.py       Mesh-/Szenen-Helfer und Vermessung
  ops/                       scene, objects, mesh, region, meta, relation,
                             preview, check, file, script
  shared/                    generierte Kopien der gemeinsamen Module
  ui.py                      Sidebar-Panel

packaging/                   PyInstaller-Spec, Entry-Point, START-HERE.txt
.github/workflows/           ci.yml, build-windows.yml, release.yml
tools/                       build_addon_zip.py, sync_shared.py,
                             bb_client.py, bb_relay_agent.py, get_cloudflared.py
docs/                        protocol, tools, security, relay, roblox, workflows
tests/                       Unit-, API-, Relay-, Packaging- und Blender-Tests
```

---

## Lizenz

MIT – siehe `LICENSE`. Benutzung auf eigene Verantwortung; insbesondere der
Skript-Modus und der externe Zugang sind mächtige Werkzeuge.

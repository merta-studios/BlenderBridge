# Protokoll-Referenz

Das Protokoll hat zwei Ebenen:

1. **HTTP-API** zwischen Agent/Browser und Connector (`http://127.0.0.1:8765/v1/...`)
2. **Socket-Protokoll** zwischen Connector und Blender-Add-on (`127.0.0.1:9876`,
   eine JSON-Zeile pro Nachricht)

Beide Ebenen benutzen dieselben Fehlercodes und denselben Antwortrahmen. Das
Add-on ist **nie** direkt aus dem Netz erreichbar.

Version: `1.0` (`blenderbridge.protocol.PROTOCOL_VERSION`).

---

## 1. HTTP-API

### 1.1 Authentifizierung

| Rolle | Header | Darf |
|---|---|---|
| **Agent** | `Authorization: Bearer bbk_…` | lesen, Jobs einreichen, Freigaben beantworten, Artefakte laden |
| **UI** (Browser) | Session-Token, beim Laden in die Seite eingebettet | zusätzlich: Einstellungen, Schlüssel, Tunnel, Not-Aus |

Jeder **zustandsändernde** Aufruf (POST/DELETE) braucht zusätzlich den Header
`X-BB-Client: 1`. Ein fremdes HTML-Formular kann diesen Header nicht setzen –
damit ist CSRF ausgeschlossen. Der `Host`-Header wird gegen eine Positivliste
geprüft (`127.0.0.1`, `localhost`, `[::1]`, Rechnername, konfigurierte Hosts);
alles andere gibt `403 forbidden` (Schutz gegen DNS-Rebinding).

Ein Agent-Schlüssel darf **keine** Einstellungen ändern, keine Schlüssel
erzeugen und keinen Tunnel öffnen. Das begrenzt den Schaden bei einem
abgeflossenen Schlüssel.

### 1.2 Endpunkte

| Methode | Pfad | Rolle | Zweck |
|---|---|---|---|
| `GET` | `/v1/health` | öffentlich | Lebenszeichen, Version, **keine** Geheimnisse |
| `GET` | `/v1/status` | Agent | Blender-Verbindung, Queue, Pending-Freigaben, Tunnel-/Relay-Status |
| `GET` | `/v1/tools` | Agent | vollständiger Werkzeugkatalog inkl. Risiko und Parametern |
| `POST` | `/v1/jobs` | Agent | Auftrag einreichen → `202` |
| `GET` | `/v1/jobs` | Agent | Aufträge auflisten (`?limit=`, `?state=`) |
| `GET` | `/v1/jobs/<id>` | Agent | ein Auftrag; `?wait=<sek>` blockiert bis fertig |
| `GET` | `/v1/jobs/<id>/artifacts/<name>` | Agent | Ergebnisdatei (PNG, GLB, ZIP …) |
| `POST` | `/v1/jobs/<id>/cancel` | Agent | Auftrag abbrechen |
| `POST` | `/v1/panic` | **UI** | Not-Aus (`{"stop_tunnel": true, "revoke_keys": false}`) |
| `GET` | `/v1/approvals` | Agent | offene Freigaben (mit `seconds_left`) |
| `POST` | `/v1/jobs/<id>/approve` | **UI** | `{"reason": "..."}` |
| `POST` | `/v1/jobs/<id>/deny` | **UI** | `{"reason": "..."}` |
| `GET` | `/v1/logs` | Agent | Audit-Log + Debug-Zeilen (letzte Einträge) |
| `GET/POST/DELETE` | `/v1/keys` | **UI** | Agent-Schlüssel verwalten |
| `GET/POST` | `/v1/config` | **UI** | Einstellungen lesen/ändern |
| `GET/POST/DELETE` | `/v1/tunnel` | **UI** | externer Zugang |
| `GET/POST/DELETE` | `/v1/relay` | **UI** | Relay konfigurieren/starten/stoppen |
| `POST`/`DELETE` | `/v1/trust` | **UI** | Vertrauensfenster öffnen/schließen (`{"seconds": 120}`) |

### 1.3 Auftrag einreichen

```http
POST /v1/jobs
Authorization: Bearer bbk_…
X-BB-Client: 1
Content-Type: application/json

{
  "tool": "mesh.edit",
  "params": {"object": "Palme", "region": "stamm", "op": "bend", "angle_deg": 25},
  "timeout_s": 120,
  "dry_run": false,
  "note": "Stamm leicht biegen, Handarbeit unten erhalten",
  "request_id": "mein-eigener-name-001",
  "protocol": "1.0"
}
```

* `tool` – genau ein Name aus `/v1/tools` (Muster `ns.name`)
* `params` – objektspezifisch, max. 2 MB
* `timeout_s` – 1 … 1800, Standard 120
* `dry_run` – nur rechnen/schätzen, nichts verändern
* `note` – erscheint im Freigabedialog (max. 2000 Zeichen)
* `request_id` – **optional**, aber empfohlen: derselbe Wert liefert denselben
  Auftrag zurück (idempotent, kein doppeltes Ausführen bei Netzwerk-Retry)

Antwort `202 Accepted`:

```json
{
  "ok": true,
  "job": {
    "job_id": "job-7c1ax9k2m4p8q1zt",
    "protocol": "1.0",
    "tool": "mesh.edit",
    "params": {"object": "Palme", "region": "stamm", "op": "bend", "angle_deg": 25},
    "risk": "moderate",
    "status": "pending_approval",
    "requires_approval": true,
    "approval_reason": "Risiko 'moderate': Verändert bestehende Objekte oder Geometrie.",
    "request_id": "mein-eigener-name-001",
    "note": "Stamm leicht biegen, Handarbeit unten erhalten",
    "created_at": "2026-10-28T13:12:09Z",
    "timeout_s": 120.0,
    "actor": "agent",
    "artifacts": []
  }
}
```

`status` ist einer von:

`pending_approval` → `queued` → `running` → `succeeded` | `failed` |
`denied` | `timed_out` | `cancelled`

### 1.4 Ergebnis abholen

```http
GET /v1/jobs/job-7c1ax9k2m4p8q1zt?wait=60
```

`wait` blockiert serverseitig bis zu 60 s (max. 120) und antwortet, sobald der
Auftrag fertig ist – das ist billiger als Polling.

```json
{
  "ok": true,
  "job": {
    "job_id": "job-7c1ax9k2m4p8q1zt",
    "status": "succeeded",
    "tool": "mesh.edit",
    "started_at": "2026-10-28T13:12:41Z",
    "finished_at": "2026-10-28T13:12:41Z",
    "result": {
      "object": "Palme",
      "region": "stamm",
      "op": "bend",
      "moved_vertices": 412,
      "max_displacement": 0.2134,
      "regions_after": ["stamm"],
      "stale_regions": []
    },
    "warnings": ["12 Punkte im geschützten Bereich 'augen' wurden nicht bewegt"],
    "assumptions": ["Drehachse aus dem Bereich übernommen (Z)"],
    "artifacts": [
      {"name": "view_perspective.png", "kind": "image", "bytes": 182344,
       "url": "/v1/jobs/job-7c1ax9k2m4p8q1zt/artifacts/view_perspective.png"}
    ]
  }
}
```

Artefakte werden **nur** über ihren Namen aus dieser Liste ausgeliefert
(keine Pfade, `..` wird abgewiesen, Endungen sind auf Bild-/Modell-/Textformate
begrenzt).

### 1.5 Freigabe beantworten

Die Freigabe kommt über die UI-Sitzung, nicht über den Agent-Schlüssel – der
Agent kann sich also nicht selbst erlauben.

```http
POST /v1/jobs/job-7c1ax9k2m4p8q1zt/approve
X-BB-Client: 1
Content-Type: application/json

{"reason": "nur leicht biegen"}
```

Danach läuft der Auftrag weiter und `GET /v1/jobs/<id>?wait=…` liefert das
Ergebnis. `deny` bricht ihn mit `approval_denied` ab. Ohne Antwort verfällt die
Freigabe nach `approval_timeout_s` (Standard 300 s).

### 1.6 Fehler

```json
{
  "ok": false,
  "error": {
    "code": "region_stale",
    "message": "Der Bereich 'stamm' passt nicht mehr zur aktuellen Topologie.",
    "details": {"region": "stamm", "hint": "region.remap oder Bereich neu anlegen"}
  }
}
```

| Code | HTTP | Bedeutung |
|---|---|---|
| `bad_request` | 400 | Aufbau falsch (unbekanntes Feld, kaputtes JSON) |
| `param_invalid` | 400 | Parameter unbrauchbar |
| `unauthorized` | 401 | Token fehlt/falsch |
| `forbidden` | 403 | Rolle darf das nicht, Host nicht erlaubt, CSRF-Header fehlt |
| `not_found` | 404 | Auftrag/Artefakt/Werkzeug unbekannt |
| `conflict` | 409 | Zustand passt nicht (z. B. Freigabe schon beantwortet) |
| `rate_limited` | 429 | zu viele Anfragen (Standard 120/min) |
| `payload_too_large` | 413 | > 4 MB Body oder > 2 MB `params` |
| `approval_required` | 403 | Freigabe nötig, wurde aber umgangen |
| `approval_denied` | 403 | Der Nutzer hat abgelehnt |
| `approval_timeout` | 403 | Freigabe nicht beantwortet |
| `blender_unavailable` | 503 | Add-on antwortet nicht |
| `blender_busy` | 503 | Blender rechnet gerade (z. B. Rendering) |
| `tool_unknown` | 400 | Werkzeug nicht im Katalog |
| `precondition_failed` | 409 | z. B. Edit-Modus aktiv, falscher Objekttyp |
| `object_not_found` / `mesh_not_found` / `region_not_found` | 404 | Ziel fehlt |
| `region_stale` | 409 | Bereich passt nicht zur Topologie |
| `no_geometry` | 409 | Auswahl/Bereich enthält keine Flächen |
| `timeout` | 504 | Auftrag zu langsam |
| `cancelled` | 409 | abgebrochen (Nutzer oder Not-Aus) |
| `internal` | 500 | unerwarteter Fehler (Details im Log, nicht in der Antwort) |
| `tunnel_error` / `relay_error` / `artifact_missing` | 502/404 | Netz-/Dateiprobleme |

Die Liste steht auch im Code: `blenderbridge.protocol.ErrorCode`. Ein Agent
sollte auf `code` reagieren, nicht auf den deutschen Text.

---

## 2. Socket-Protokoll (Connector ↔ Add-on)

Rahmenbedingungen:

* TCP auf `127.0.0.1`, Standardport 9876, **eine JSON-Zeile pro Nachricht**
  (UTF-8, `\n` als Trenner, keine Zeilenumbrüche im JSON)
* Die erste Nachricht muss `{"type": "hello", "token": "…", "client": "connector"}`
  sein. Falscher Token → `{"type": "error", "error": {...}}` und Verbindung zu.
* Token wird aus einer gemeinsamen Datei gelesen (siehe `docs/security.md`),
  per Konstantzeit-Vergleich geprüft, Fehlversuche werden gezählt.
* Keepalive: Connector schickt alle 5 s `{"type": "ping"}`, das Add-on
  antwortet mit `{"type": "pong"}`.
* **Ausführung nur im Hauptthread.** Das Add-on legt Anfragen in eine Queue und
  führt sie in einem `bpy.app.timers`-Callback aus – `bpy` wird nie aus einem
  fremden Thread angefasst.

### Anfrage

```json
{
  "type": "request",
  "id": "req_5b2c…",
  "tool": "object.probe",
  "params": {"object": "Palme"},
  "dry_run": false,
  "deadline": 1761658329.4,
  "meta": {"job_id": "job_7c1a…", "actor": "agent"}
}
```

### Antwort (Erfolg)

```json
{
  "type": "result",
  "id": "req_5b2c…",
  "ok": true,
  "data": {"object": "Palme", "dimensions": [4.1, 4.1, 8.0], "…": "…"},
  "warnings": [],
  "timing": {"ms": 12.4}
}
```

### Antwort (Fehler)

```json
{
  "type": "result",
  "id": "req_5b2c…",
  "ok": false,
  "error": {"code": "object_not_found", "message": "Kein Objekt 'Palme'.",
            "details": {"available": ["Palm_Tree", "Coconut"]}}
}
```

### Ereignisse (ohne `id`)

```json
{"type": "event", "event": "progress", "job_id": "job_7c1a…", "percent": 40,
 "message": "Rendere Ansicht 2/8"}
{"type": "event", "event": "scene_changed", "objects": ["Palme"]}
{"type": "event", "event": "hello_ok", "info": {"blender_version": "4.2.0", …}}
```

Ereignisse sind Hinweise für die Oberfläche (Fortschritt, „Nutzer hat die Szene
geändert“). Sie ersetzen keine Antwort.

### Grenzen

| Größe | Limit |
|---|---|
| Nachricht | 8 MB pro Zeile |
| gleichzeitige Anfragen | 1 (Blender ist single-threaded, der Rest wartet) |
| Standard-Timeout | 120 s, Max 1800 s |
| ungültige JSON-Zeile | Verbindung wird geschlossen (kein Blindflug) |

---

## 3. Verträge, auf die sich ein Agent verlassen kann

1. **Werkzeugnamen sind stabil.** `GET /v1/tools` ist die Wahrheit; unbekannte
   Namen geben `tool_unknown` statt irgendetwas auszuführen.
2. **Jede Veränderung ist nachvollziehbar.** Jeder Auftrag bekommt eine
   `audit_id`; das Audit-Log enthält Zeitpunkt, Werkzeug, Risiko, Status und
   redigierte Parameter.
3. **Fehler ändern nichts.** Codes in `protocol.NON_MUTATING_ERRORS` garantieren:
   die Szene wurde nicht angefasst.
4. **`dry_run` ändert nichts.** Wo es sinnvoll ist, liefert es
   `would_move`/`estimated_max_displacement`/`vertices_affected`.
5. **Topologieänderungen werden gemeldet.** Werkzeuge, die Topologie ändern,
   geben `regions_after` und `stale_regions` zurück.
6. **Nichts passiert ohne Freigabe über der Schwelle**, außer der Nutzer hat
   ausdrücklich ein Vertrauensfenster geöffnet.
7. **Idempotenz:** gleiche `request_id` → derselbe Auftrag (`job_id`), auch nach
   einem Retry. Wichtig, wenn die Verbindung mittendrin abbricht.

---

## 4. Minimaler Client (Python, ohne Abhängigkeiten)

```python
import json, time, urllib.request

BASE = "http://127.0.0.1:8765"
KEY = "bbk_…"          # im UI unter "Verbindung & Agent"

def call(tool, params=None, wait=60, dry_run=False):
    body = json.dumps({"tool": tool, "params": params or {}, "dry_run": dry_run}).encode()
    req = urllib.request.Request(
        BASE + "/v1/jobs", data=body, method="POST",
        headers={"Authorization": f"Bearer {KEY}", "X-BB-Client": "1",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        job = json.loads(r.read())["job"]
    while True:
        req = urllib.request.Request(
            f"{BASE}/v1/jobs/{job['job_id']}?wait=60",
            headers={"Authorization": f"Bearer {KEY}"})
        with urllib.request.urlopen(req, timeout=80) as r:
            job = json.loads(r.read())["job"]
        if job["status"] in ("succeeded", "failed", "denied", "cancelled", "timed_out"):
            return job
        time.sleep(0.5)

print(call("scene.info")["result"])
```

Ein vollständigerer Client mit Warteschlange, Artefakt-Download und
Klartextfehlern liegt in `tools/bb_client.py`.

---

## 5. Versionierung

* `PROTOCOL_VERSION` steht in jeder Antwort (`/v1/status`, `/v1/health`).
* Der Connector lehnt unbekannte Protokollversionen mit `unsupported` ab.
* Innerhalb einer Hauptversion werden nur Felder **ergänzt**, nie entfernt oder
  umbenannt. Agents sollen unbekannte Felder ignorieren.

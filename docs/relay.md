# Relay: GitHub-Issue als Postfach

## Warum es das gibt

Ein Agent läuft oft in einer Sandbox, die nur wenige Ziele im Internet erreichen
darf – häufig `github.com` und sonst nichts. Ein Cloudflare-Tunnel hilft dann
nicht: der Agent kann die Tunnel-URL gar nicht aufrufen.

Der Relay dreht die Richtung um: **dein Rechner fragt selbst**. Der Connector
liest regelmäßig die Kommentare eines GitHub-Issues. Anfragen und Antworten
stehen dort als versiegelte JSON-Kommentare. Kein offener Port, keine eingehende
Verbindung, keine Annahme über die Netzwerkfähigkeiten der Gegenseite.

```
Agent ──POST Kommentar──▶ GitHub-Issue ◀──GET alle 3 s── Connector ──▶ Blender
Agent ◀──GET Kommentare── GitHub-Issue ◀──POST Antwort── Connector
```

Ehrliche Nachteile:

* Latenz: mindestens ein Poll-Intervall (Standard 3 s) pro Richtung.
* Alles landet in einem GitHub-Issue. Es ist versiegelt, aber die **Metadaten**
  (Zeitpunkt, Anzahl, Größe) sind sichtbar.
* Das Repository sollte **privat** sein.
* GitHub-Rate-Limits begrenzen den Durchsatz (5000 Anfragen/Stunde).

---

## Einrichtung (einmal, ca. 5 Minuten)

### 1. Repository und Issue

Nimm ein **privates** Repository, das dir gehört (das BlenderBridge-Repo selbst
geht auch, ein separates ist sauberer). Lege ein Issue an, z. B. mit dem Titel
`BlenderBridge Relay` und einem beliebigen Text.

Notiere `owner/name` und die Issue-Nummer (steht in der URL).

### 2. Feingranulares Token

GitHub → *Settings → Developer settings → Personal access tokens → Fine-grained
tokens → Generate new token*

* **Repository access:** nur dieses eine Repository
* **Permissions → Issues:** `Read and write`
* Alles andere: keine Rechte
* Ablaufdatum setzen (z. B. 90 Tage)

Das Token beginnt mit `github_pat_…` (oder `ghp_…` bei klassischen Tokens).

### 3. Im Connector eintragen

Oberfläche → **Verbindung & Agent → Relay**:

| Feld | Wert |
|---|---|
| Repository | `owner/name` |
| Issue | z. B. `7` |
| Token | `github_pat_…` (wird verschlüsselt gespeichert, nie angezeigt) |
| Poll-Intervall | 3 (Sekunden) |

Dann:

1. **„Verbindung testen“** → ruft `GET /repos/…/issues/<n>` und zeigt, ob das
   Token reicht. Bei `403` fehlt das Issue-Recht, bei `404` stimmt Repo/Issue
   nicht.
2. **„Geheimnis erzeugen“** → erzeugt ein gemeinsames Geheimnis (mind. 24
   Zeichen). Es wird **einmal** angezeigt – jetzt kopieren.
3. **„Relay starten“**.

Der Status zeigt danach `running`, `last_poll` und wie viele Nachrichten
verarbeitet wurden.

---

## Auf der Agentenseite

```bash
export BB_RELAY_REPO='owner/name'
export BB_RELAY_ISSUE='7'
export BB_RELAY_SECRET='<das kopierte Geheimnis>'
export GITHUB_TOKEN='github_pat_…'

python tools/bb_relay_agent.py call scene.info
python tools/bb_relay_agent.py call object.probe --params '{"object":"Palme"}'
python tools/bb_relay_agent.py call preview.render \
    --params '{"views":["perspective","front"]}' --out ./renders
```

Ausgabe: das Ergebnis als JSON auf stdout, Hinweise/Fehler auf stderr,
Bilder werden bei `--out` gespeichert. Exit-Codes: `0` ok, `1` Werkzeugfehler,
`2` Netz/Config, `3` Timeout.

Der Client ist absichtlich eigenständig (nur Standardbibliothek, ~350 Zeilen),
damit du ihn in eine Sandbox kopieren kannst. Ein Test prüft, dass seine
Versiegelung byte-kompatibel zum Connector ist.

---

## Nachrichtenformat

Jeder Kommentar ist ein JSON-Objekt mit `bb`, `id`, `type` und `sealed`:

```json
{"bb":1,"id":"agent-3f9a1c7d","type":"request",
 "sealed":"<base64: nonce|ciphertext|tag>"}
```

Bei großen Nutzlasten wird aufgeteilt:

```json
{"bb":1,"id":"agent-3f9a1c7d","type":"request","part":0,"parts":3,"sealed":"…"}
```

Fehlt ein Teil, wird die Nachricht nicht entschlüsselt (kein stiller
Datenverlust).

Nach innen enthält `sealed` eine normale Job-Anfrage (siehe `docs/protocol.md`):

```json
{"type":"request","id":"agent-3f9a1c7d","tool":"object.probe",
 "params":{"object":"Palme"},"note":"…","timeout_s":120}
```

Antworttypen, die der Agent sieht:

| `type` | Bedeutung |
|---|---|
| `result` | fertig; enthält `status`, `result`/`error`, `warnings`, `assumptions`, `artifacts` |
| `pending_approval` | wartet auf das OK des Nutzers im Connector-Fenster |
| `error` | Anfrage konnte nicht angenommen werden (unbekanntes Werkzeug, kaputte Nutzlast, fehlendes Geheimnis) |

Bilder bis 280 KB werden bei `result` als Base64 eingebettet
(`artifacts[].base64`), damit ein Agent ohne Netzzugriff zum PC trotzdem etwas
zu sehen bekommt.

---

## Sicherheit

* **Token-Rechte klein halten.** Nur Issues auf einem Repository. Das Token
  liegt per DPAPI verschlüsselt in `secrets.bin`.
* **Geheimnis geheim halten.** Wer es hat, kann Anfragen einschleusen. Bei
  Verdacht: „Geheimnis erzeugen“ (neues Geheimnis, alter Agent funktioniert
  nicht mehr) und das Issue durchsuchen.
* **Repo privat halten.** Sonst sind Kommentare öffentlich lesbar – versiegelt,
  aber immer noch öffentlich.
* **Freigaben bleiben lokal.** Ein Relay-Auftrag mit `moderate` wartet im
  Connector-Fenster; der Agent bekommt `pending_approval`. Der Agent kann sich
  **nicht** selbst erlauben.
* **Not-Aus** beendet auch den Relay.

---

## Wenn es nicht läuft

| Symptom | Ursache |
|---|---|
| `last_error: 404` | Repo/Issue falsch, oder das Token sieht das Repo nicht (feingranular = nur ausgewählte Repos) |
| `last_error: 403` | Token ohne `Issues: Read and write` oder Rate-Limit erschöpft |
| Agent bekommt `error: relay secret missing` | Im Connector wurde noch kein Geheimnis erzeugt |
| Agent meldet „authentication failed“ | Geheimnisse stimmen nicht überein (Tippfehler, Zeilenumbruch beim Kopieren) |
| Antworten kommen doppelt | Mehrere Relay-Instanzen laufen (zwei Connector-Fenster) – eines schließen |
| Nichts passiert | Relay gestartet? Der Knopf startet nur, wenn Token, Repo und Issue gesetzt sind |

Debug: `GET /v1/relay` zeigt `state`, `last_poll`, `last_error`, `processed`,
`posted`. Im Audit-Log stehen `relay_request`-Einträge mit Werkzeug und ID.

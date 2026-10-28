# Sicherheit

Dieses Dokument beschreibt, **wovor** BlenderBridge schützt, **wie**, und was es
**nicht** kann. Es ist absichtlich nüchtern: Sicherheitsversprechen ohne Grenzen
sind gefährlich.

Kurzfassung:

* Alles läuft lokal auf `127.0.0.1`. Das Add-on ist **nie** aus dem Netz
  erreichbar.
* Externer Zugang ist **aus**, bis du ihn einschaltest, und wird beim Beenden
  geschlossen.
* Ein Link ist **keine** Authentifizierung. Jeder Aufruf braucht einen Schlüssel.
* Riskante Aktionen brauchen dein OK.
* Es gibt **keinen** Endpunkt für Betriebssystem-Befehle. `script.run` ist der
  einzige Weg zu beliebigem Code – dreifach gesperrt.
* Not-Aus ist immer sichtbar und wirkt sofort.
* Alles wird protokolliert.

---

## 1. Bedrohungsmodell

Wer oder was könnte angreifen?

| Angreifer | Ziel | Schutz |
|---|---|---|
| **Eine Webseite, die du offen hast** | heimlich Anfragen an `127.0.0.1:8765` schicken | `Host`-Prüfung (DNS-Rebinding), `Origin`-Prüfung, Pflicht-Header `X-BB-Client` (CSRF), kein CORS |
| **Ein anderer Nutzer im selben Netz** | auf die API zugreifen | Bindet nur an `127.0.0.1`; extern nur über Tunnel, und dort nur mit Schlüssel |
| **Jemand mit der Tunnel-URL** | die Szene manipulieren | Agent-Schlüssel nötig, Rate-Limit, Freigaben, Audit-Log, Not-Aus |
| **Ein Agent mit gültigem Schlüssel, der Unsinn macht** | Dateien löschen, Skripte ausführen | Risikostufen + Freigaben, kein OS-Zugriff, Sicherungen vorher, `dry_run`, Not-Aus |
| **Ein Angreifer mit Zugriff auf deinen Benutzerkonto** | Schlüssel/Token stehlen | Schlüssel nur gehasht gespeichert, Secrets per DPAPI, Token-Dateien mit `0600` |
| **Malware, die `cloudflared.exe` ersetzt** | beliebiges Programm starten | nur manuell installierte, geprüfte Binärdatei (Hash-Vergleich), Pfad in den Einstellungen sichtbar |
| **Ein manipulierter Relay-Inhalt** (GitHub-Issue) | gefälschte Anfragen einschleusen | HMAC-Versiegelung mit gemeinsamem Geheimnis, Zeitfenster, Nonce |

**Nicht im Bedrohungsmodell:** ein bereits vollständig kompromittierter Rechner,
bösartige Blender-Add-ons von Dritten, und alles, was passiert, nachdem du
`script.run` erlaubt hast.

---

## 2. Netzwerk

### Lokal

```
Browser ──HTTP──▶ 127.0.0.1:8765  Connector ──TCP/JSON──▶ 127.0.0.1:9876  Blender-Add-on
```

* Beide Server binden an `127.0.0.1`. Ein Test prüft, dass die Add-on-Adresse
  keine `0.0.0.0` ist.
* Der Connector erlaubt nur bekannte `Host`-Header (`127.0.0.1`, `localhost`,
  `[::1]`, Rechnername, explizit konfigurierte). Alles andere: `403`.
* Zustandsändernde Anfragen brauchen `X-BB-Client: 1`. Ein HTML-Formular kann
  diesen Header nicht setzen; damit ist klassisches CSRF wirkungslos.
* CORS ist aus (`allow_cors: false`). Damit kann eine fremde Seite die Antworten
  nicht lesen.
* Fehlt der Header oder stimmt der Host nicht, wird **nichts** ausgeführt.

### Extern (opt-in)

Der Tunnel wird **von innen** aufgebaut (`cloudflared`). Es lauscht nichts auf
einer öffentlichen Adresse, es gibt keine Portfreigabe im Router und keine
eingehende Verbindung.

* Aus per Default (`tunnel_enabled: false`). Beim Start wird nie automatisch ein
  Tunnel geöffnet.
* Beim Beenden und bei Not-Aus wird er beendet.
* Die URL ist trotzdem **öffentlich**. Deshalb bleibt der Agent-Schlüssel
  Pflicht; ohne ihn: `401`.
* Der Connector testet nach dem Start selbst, ob die URL wirklich antwortet, und
  zeigt das Ergebnis an. Behauptungen werden nicht geglaubt, sondern geprüft.
* Nach jedem Tunnelstart steht im Audit-Log: Zeitpunkt, Host, Dauer.

**Ehrlich:** Wer die URL kennt **und** einen gültigen Schlüssel hat, kann alles,
was die Freigabeschwelle erlaubt. Deshalb: Schwelle nicht leichtfertig senken,
Vertrauensfenster kurz halten, Schlüssel pro Agent, bei Verdacht sofort
widerrufen.

---

## 3. Authentifizierung und Rollen

| Rolle | Wie | Darf |
|---|---|---|
| **UI** | Session-Token, pro Start neu erzeugt, in die Seite eingebettet | alles außer Code |
| **Agent** | `Authorization: Bearer bbk_…` | lesen, Jobs, Freigaben beantworten lassen (nicht selbst), Artefakte |

* Agent-Schlüssel bestehen aus einem Präfix `bbk_`, 32 Byte Zufall (Base64url)
  und werden **nur als HMAC-SHA256** mit zufälligem Salt gespeichert. Der
  Klartext existiert genau einmal: in der Antwort auf `POST /v1/keys`.
* Vergleich mit `hmac.compare_digest` (keine Timing-Seite).
* Jeder Schlüssel hat ein Label, Erstellungsdatum, letzte Nutzung, Zähler und
  kann einzeln widerrufen werden.
* Der Agent kann **keine** Einstellungen ändern, keine Schlüssel erzeugen und
  keinen Tunnel öffnen. Ein abgeflossener Agent-Schlüssel ist damit unangenehm,
  aber begrenzt.
* Rate-Limit: 120 Anfragen/Minute (konfigurierbar), danach `429`.
* Der UI-Token wird bei jedem Start neu erzeugt; ein altes Browserfenster
  funktioniert danach nicht mehr.

---

## 4. Freigaben

`ApprovalPolicy.requires_approval(risk)` vergleicht die Risikostufe des
Werkzeugs mit der Schwelle (Standard `moderate`).

* `read`, `low` → laufen durch.
* `moderate`, `high`, `critical` → Auftrag wartet als `pending_approval`.
* Der Dialog zeigt Werkzeug, Risiko, Kurzfassung, Notiz des Agenten und die
  **vollständigen** Parameter (redigiert um Geheimnisse).
* Nach `approval_timeout_s` (Standard 300 s) verfällt die Anfrage. Es wird
  **nichts** ausgeführt.
* Ablehnen → Status `denied`, nichts passiert, Audit-Eintrag.
* **Vertrauensfenster** (optional, max. 1 h): überspringt Freigaben befristet.
  Sichtbar in der Oberfläche, endet bei Not-Aus und beim Beenden.
* `script.run` verlangt **immer** eine Freigabe, unabhängig von der Schwelle
  (`script_requires_approval`).

Der Agent kann eine Freigabe **nicht** selbst erteilen: `approve`/`deny` sind
UI-Rollen.

---

## 5. Skript-Modus (`script.run`)

Das ist der gefährlichste Teil und deshalb dreifach gesperrt:

1. **Connector-Einstellung** `allow_script_mode` (Standard: `true`, aber jede
   Ausführung braucht Freigabe). Ist sie `false`, wird `script.run` schon nicht
   mehr im Werkzeugkatalog angezeigt.
2. **Freigabe durch dich** bei jeder Ausführung (`script_requires_approval`).
3. **Add-on-Einstellung** `allow_script_mode` (Standard: **`false`**). Ohne
   diesen Haken in Blender lehnt das Add-on grundsätzlich ab.

Zusätzlich:

* Nur der Token-Inhaber kommt überhaupt an das Add-on.
* Der Code wird **im Blender-Prozess** ausgeführt. Das ist keine Sandbox.
  `bpy` darf alles, was dein Benutzer darf: Dateien lesen/schreiben, Prozesse
  starten, Netzzugriffe. Der Dialog sagt das ausdrücklich.
* Jeder Aufruf wird mit Zeitpunkt, Länge und SHA-256 des Codes protokolliert
  (der Code selbst wird gekürzt gespeichert, damit du nachvollziehen kannst,
  was lief).
* `script.run` hat ein eigenes Timeout und blockiert Blender während der
  Ausführung.
* Empfehlung: nur einschalten, wenn du wirklich einen Sonderfall hast, danach
  wieder ausschalten.

---

## 6. Dateien und Geheimnisse

| Datei | Inhalt | Rechte |
|---|---|---|
| `config.json` | Einstellungen, **keine** Geheimnisse | normal |
| `secrets.bin` | Token und Relay-Geheimnisse, per **DPAPI** verschlüsselt (Windows) | `0600` |
| `addon_token.txt` | gemeinsamer Token Connector ↔ Add-on (Auto-Pairing) | `0600` |
| `logs/audit.jsonl` | Audit-Log, rotiert bei 4 MB, 3 Dateien | normal |
| (in `secrets.bin`) | Agent-Schlüssel – nur Hashes, nie Klartext | `0600` |
| `logs/connector.log` | technisches Log | normal |

* DPAPI bindet `secrets.bin` an dein Windows-Benutzerkonto: Kopieren auf einen
  anderen Rechner macht es unbrauchbar (dann werden die Geheimnisse neu
  erzeugt, nicht entschlüsselt).
* Auf Linux/macOS (Entwicklung) gibt es kein DPAPI; dort steht im Log eine
  ehrliche Warnung, dass nur Dateirechte schützen.
* Passwörter, Token und Schlüssel werden in Logs **redigiert**
  (`security.redact`): alles, was wie `token`, `key`, `secret`, `password`,
  `authorization` aussieht, wird zu `***`.
* Artefakt-Downloads akzeptieren nur Dateinamen, keine Pfade (`..` → `404`), und
  nur erlaubte Endungen (`.png`, `.json`, `.glb`, `blend`, `.txt`, …).

---

## 7. Sicherungen und Wiederherstellung

* Vor jeder Aktion ab Risiko `moderate` legt das Add-on eine `.blend`-Kopie an.
* Scheitert die Sicherung bei `high`/`critical`, wird die Aktion **abgebrochen**.
* Der Sicherungsordner liegt neben deiner Datei (`blenderbridge_backups/`),
  nicht im Repo, nicht im Konfigordner – so findest du ihn wieder.
* Alte Sicherungen werden nach `snapshot_keep` (Standard 40) gelöscht.

**Grenze:** Eine Sicherung ist eine Datei, kein Transaktions-Log. Änderungen, die
du **nach** der Sicherung und **vor** dem Absturz gemacht hast, sind nur über
Blenders eigenes Undo oder die Autosave-Datei zu retten.

---

## 8. Not-Aus

Der rote Knopf oben rechts (`POST /v1/panic`) macht in dieser Reihenfolge:

1. alle laufenden und wartenden Aufträge abbrechen,
2. das Vertrauensfenster schließen,
3. den Tunnel beenden,
4. optional alle Agent-Schlüssel widerrufen,
5. Audit-Eintrag `panic` mit den ausgeführten Schritten.

Ein laufendes Rendering in Blender kann nicht von außen unterbrochen werden –
es endet, wenn Blender fertig ist. Alles danach wird nicht mehr ausgeführt.

---

## 9. `cloudflared` – Herkunft und Prüfung

`cloudflared` ist ein offizielles Cloudflare-Programm, aber es läuft mit deinen
Rechten. Deshalb:

1. Nur von `https://github.com/cloudflare/cloudflared/releases` laden.
2. SHA-256 vergleichen: Cloudflare veröffentlicht pro Release eine
   `cloudflared-windows-amd64.exe` und zugehörige Prüfsummen.
3. `python tools/get_cloudflared.py --check` zeigt Version und Hash der Datei,
   die BlenderBridge benutzen würde.
4. Der Pfad ist in den Einstellungen sichtbar (`cloudflared_path`). BlenderBridge
   lädt **nichts** automatisch nach und führt keine fremden Skripte aus.
5. Fehlt die Datei, bleibt der Tunnel einfach aus – mit klarer Meldung.

---

## 10. Relay (GitHub-Issue als Postfach)

* Der Connector **fragt** regelmäßig (Standard: alle 3 s) die Kommentare eines
  Issues ab. Es gibt keine eingehende Verbindung.
* Inhalte sind mit einem gemeinsamen Geheimnis versiegelt: HMAC-SHA256 über
  Kopf + Nutzlast, XOR-Keystream aus demselben Schlüssel. Das ist
  **Integrität + Vertraulichkeit gegenüber Mitlesern**, aber kein
  Modernes-Krypto-Design mit eigener Schlüsselableitung – für ein privates
  Repo-Issue mit einem Token, das nur Issues darf, ist es angemessen.
* Das GitHub-Token sollte **feingranular** sein: nur `Issues: Read and write`
  für genau dieses Repository. Kein `repo`-Scope.
* Alte Kommentare werden ignoriert (Zeitstempel + bereits gesehene IDs).
* Antworten werden gelöscht? Nein – GitHub-Kommentare bleiben als Audit-Spur
  stehen. Sie sind versiegelt, aber **gehen davon aus, dass das Repo privat
  ist**. Bei einem öffentlichen Repo ist das ein Datenleck: nicht tun.

---

## 11. Was BlenderBridge bewusst **nicht** tut

* Keine `eval`/`exec`-Endpunkte außer dem gesperrten `script.run`.
* Keine Shell-Befehle, kein Datei-Browser für beliebige Pfade, kein
  `subprocess` auf Zuruf eines Agenten.
* Keine automatische Installation von Add-ons oder Abhängigkeiten.
* Keine Telemetrie, keine Cloud, kein Konto.
* Keine stillen Änderungen an deiner `.blend`-Datei: gespeichert wird nur auf
  ausdrücklichen Aufruf (`file.save`), Sicherungen sind separate Dateien.

---

## 12. Prüfliste vor dem ersten Einsatz mit externem Zugang

- [ ] Blender-Projekt gespeichert und eine Sicherung vorhanden.
- [ ] `approval_threshold` steht auf `moderate` oder strenger.
- [ ] Für den Agenten ein **eigener** Schlüssel mit sprechendem Label.
- [ ] Vertrauensfenster aus (oder sehr kurz).
- [ ] `script.run` im Add-on ausgeschaltet, wenn nicht gebraucht.
- [ ] Tunnel nur einschalten, solange du arbeitest.
- [ ] Nach der Arbeit: Tunnel aus, Not-Aus, Programm beenden.
- [ ] `logs/audit.jsonl` einmal überflogen – steht da, was du erwartest?

---

## 13. Sicherheitslücke gefunden?

Bitte kein öffentliches Issue mit ausnutzbaren Details. Schreib eine kurze Mail
an den Maintainer (siehe Repository-Profil) mit Reproduktionsschritten und
Version. Antwort innerhalb von 7 Tagen.

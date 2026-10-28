# Bauen, Testen, Veröffentlichen (GitHub Actions)

Alles, was hier steht, ist so gebaut und **gelaufen** – nicht nur geplant. Die
Läufe sind im Reiter **Actions** des Repositories nachvollziehbar.

---

## 1. Die drei Workflows

| Datei | Wann | Was |
|---|---|---|
| `.github/workflows/ci.yml` | jeder Push, jeder PR, manuell | Unit-Tests (Linux + Windows), echte Blender-4.2-Tests, Add-on-ZIP als Artefakt |
| `.github/workflows/build-windows.yml` | Push auf `main`/`arena/**`, Tags `v*`, PRs mit Codeänderungen, manuell | baut `BlenderBridge.exe`, **Smoke-Test der EXE**, ZIP-Paket, Artefakt |
| `.github/workflows/release.yml` | Tag `v*` oder manuell mit Version | baut und veröffentlicht ein GitHub-Release mit ZIP |

### ci.yml im Detail

**Job `unit-tests`** (ubuntu-latest, Python 3.11)

1. `pip install -r requirements-dev.txt`
2. `python -m pytest tests -q --ignore=tests/blender` → 109 Tests
3. `python -m blenderbridge.app --selftest` → 26 Prüfungen, muss „bestanden“ sagen
4. `python tools/build_addon_zip.py` → `dist/blenderbridge_addon.zip`
5. Artefakt `blenderbridge-addon` (90 Tage, mit Login herunterladbar)

**Job `blender-tests`** (ubuntu-**22.04**, Python 3.11)

1. Systembibliotheken für Blender installieren
2. `pip install bpy==4.2.0` (nur für CPython 3.11 verfügbar)
3. `python -c "import bpy"` → muss die Version ausgeben
4. `python tools/run_blender_tests.py -- tests/blender -v -rs` → 19 echte
   Blender-Tests

Der Wrapper ist wichtig und ehrlich: `bpy` stürzt beim **Beenden** des
Interpreters manchmal ab (SIGSEGV, Exit-Code 139/-11). Das ist ein Fehler im
Blender-Wheel, nicht in den Tests. Der Wrapper liest die pytest-Zusammenfassung
und schlägt nur fehl, wenn wirklich ein Test fehlschlägt; sonst gibt er eine
Warnung aus. Alles andere wäre entweder „rot wegen nichts“ oder „grün obwohl
kaputt“.

Alternativ kann man die Blender-Tests mit echtem Blender starten:

```bash
blender --background --python-expr "import sys, pytest; sys.exit(pytest.main(['tests/blender','-q']))"
```

**Job `windows-smoke`** (windows-latest)

Führt die Unit-Tests unter Windows aus – dort fallen Dinge auf, die unter Linux
unsichtbar sind (Pfadtrenner, CRLF in Dateien, `os.replace` auf offenen Dateien).
Genau so wurde ein echter Fehler gefunden: die Prüfsummen der gemeinsamen
Module waren zeilenendeabhängig. Behoben durch LF-Normalisierung beim Hashen
plus `.gitattributes`.

---

## 2. Der Windows-Build

```powershell
pip install "pyinstaller>=6.3"
python tools/build_addon_zip.py
pyinstaller packaging/blenderbridge.spec --clean --noconfirm
.\dist\BlenderBridge.exe --selftest     # muss 0 zurückgeben und "bestanden" enthalten
```

Der Spec (`packaging/blenderbridge.spec`):

* eine Datei (`--onefile` ist das Standardverhalten von `EXE(pyz, …, a.binaries, a.datas)`)
* **Konsole an** (`console=True`): dort stehen URL, Token und Fehler. Eine
  unsichtbare EXE, die im Hintergrund lauscht, wäre unheimlich und
  unbrauchbar.
* `ui.html` wird mit eingepackt (`datas`), sonst hätte die Oberfläche keine Seite
* alle `blenderbridge.*`-Module als `hiddenimports`, weil sie dynamisch
  importiert werden
* `excludes`: tkinter, numpy, PIL, pytest – spart Größe, wird nicht gebraucht

Ergebnis: `dist/BlenderBridge.exe` (PyInstaller, typisch 10–20 MB), im CI in
unter einer Minute gebaut.

**Nicht signiert.** Ein Code-Signing-Zertifikat kostet Geld und ist an eine
Firma gebunden. Windows SmartScreen warnt deshalb beim ersten Start
(„Weitere Informationen“ → „Trotzdem ausführen“). Das ist im README und in
`START-HERE.txt` erklärt – lieber ehrlich als überraschend.

### Der Smoke-Test in CI

```powershell
$output = & ".\dist\BlenderBridge.exe" --selftest 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) { exit 1 }
if ($output -notmatch "bestanden") { exit 1 }
```

Damit ist bewiesen, dass die gebaute EXE wirklich startet, ihre Module findet
und die komplette Kette (Protokoll, Verschlüsselung, Jobs, HTTP) durchläuft –
ohne dass jemand sie von Hand anklicken muss.

---

## 3. Wo die Datei liegt

**Release (empfohlen, kein Login):**
`https://github.com/merta-studios/BlenderBridge/releases`
→ Asset `BlenderBridge-<version>-windows-x64.zip`

**Actions-Artefakt (90 Tage, Login nötig):**
Actions → *Build Windows EXE* → neuester grüner Lauf → unten *Artifacts*
→ `BlenderBridge-windows-x64`

Inhalt des ZIPs:

```
BlenderBridge-0.1.0-windows-x64/
  BlenderBridge.exe
  blenderbridge_addon.zip
  START-HERE.txt
  README.md
```

---

## 4. Ein Release machen

```bash
git tag v0.1.0
git push origin v0.1.0
```

Das startet `build-windows.yml` (baut + prüft + hängt die Dateien an ein
Release, `softprops/action-gh-release`) und `release.yml` (eigenständiger
Release-Lauf mit Versionseingabe).

Manuell: Actions → *Release* → *Run workflow* → Version eingeben
(`v0.1.1`).

Vorher prüfen:

- [ ] `python -m blenderbridge.app --selftest` lokal grün
- [ ] `python -m pytest tests -q --ignore=tests/blender` lokal grün
- [ ] README-Version und `blenderbridge/__init__.py` (`__version__`) gleich
- [ ] `python tools/sync_shared.py` gelaufen (sonst schlägt der Test fehl)

---

## 5. Was hier bewusst fehlt

* **Kein automatisches Veröffentlichen auf main**, ohne dass Tests grün sind –
  der Build-Job hängt an den Tests.
* **Kein Auto-Update.** Die EXE lädt nichts nach. Ein Update heißt: neue Datei
  herunterladen.
* **Kein Upload zu einem Store / Signatur-Dienst.** Das bräuchte Secrets im
  Repo, die ich nicht erfinden kann.
* **Keine macOS-/Linux-Binaries.** Der Connector läuft dort aus dem Quellcode
  (`python -m blenderbridge`), ein Paket gibt es nur für Windows, weil das die
  Zielplattform ist.

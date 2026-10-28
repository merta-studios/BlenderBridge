#!/usr/bin/env python3
"""Generate docs/tools.md from the tool catalogue (single source of truth)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from blenderbridge import tools  # noqa: E402

GROUPS = [
    ("scene", "Szene"),
    ("bridge", "Brücke / Verbindung"),
    ("object", "Objekte"),
    ("mesh", "Mesh und Modellierung"),
    ("region", "Benannte Bereiche"),
    ("anchor", "Form-Anker"),
    ("relation", "Räumliche Beziehungen"),
    ("preview", "Vorschau-Rendering"),
    ("check", "Prüfungen"),
    ("file", "Dateien, Sicherungen, Export"),
    ("script", "Skript-Modus (gesperrt)"),
    ("memory", "Projektgedächtnis"),
]

RISK_TEXT = {
    "read": "nur lesen",
    "low": "fügt hinzu",
    "moderate": "ändert Bestehendes",
    "high": "löscht / Topologie",
    "critical": "beliebiger Code",
}


def main() -> int:
    specs = [tools.get(n) for n in tools.names()]
    by_ns = {}
    for spec in specs:
        ns = spec.name.split(".", 1)[0]
        by_ns.setdefault(ns, []).append(spec)

    out = []
    out.append("# Werkzeug-Referenz\n")
    out.append(f"Alle **{len(specs)}** Werkzeuge, die der Connector anbietet. Diese Datei wird aus\n"
               "`blenderbridge/tools.py` erzeugt (`python tools/gen_docs.py`) und ist damit\n"
               "immer identisch mit dem, was ein Agent über `GET /v1/tools` sieht.\n")
    out.append("> Ein Agent kann **nur** diese Werkzeuge aufrufen. Es gibt keinen Endpunkt für\n"
               "> beliebigen Python-Code außer `script.run`, und der ist dreifach gesperrt.\n")
    out.append("\n## Risikostufen\n")
    out.append("| Stufe | Bedeutung | Freigabe ab Schwelle `moderate` (Standard) |")
    out.append("|---|---|---|")
    for risk in ("read", "low", "moderate", "high", "critical"):
        need = "nein" if risk in ("read", "low") else "**ja**"
        out.append(f"| `{risk}` | {RISK_TEXT[risk]} | {need} |")
    out.append("")

    out.append("## Übersicht\n")
    out.append("| Namensraum | Werkzeuge |")
    out.append("|---|---|")
    for ns, label in GROUPS:
        if ns in by_ns:
            out.append(f"| `{ns}` | {label} ({len(by_ns[ns])}) |")
    out.append("")

    for ns, label in GROUPS:
        specs_ns = by_ns.get(ns)
        if not specs_ns:
            continue
        out.append(f"\n## {label} (`{ns}.*`)\n")
        for spec in specs_ns:
            flags = []
            if spec.mutating:
                flags.append("verändert die Szene")
            if spec.internal:
                flags.append("intern")
            flag_text = f" _{', '.join(flags)}_" if flags else ""
            out.append(f"### `{spec.name}`\n")
            out.append(f"**Risiko:** `{spec.risk}`{flag_text}\n")
            out.append(spec.summary + "\n")
            if spec.params:
                out.append(f"**Parameter:** {spec.params}\n")
            if spec.approval_hint:
                out.append(f"**Freigabehinweis:** {spec.approval_hint}\n")
            if spec.examples:
                out.append("**Beispiele:**\n")
                for ex in spec.examples:
                    out.append("```json")
                    out.append(ex)
                    out.append("```")
                out.append("")

    out.append("\n## Antwortformat\n")
    out.append("Jedes Werkzeug antwortet mit demselben Rahmen (siehe `docs/protocol.md`):\n")
    out.append("```json")
    out.append('{\n  "job_id": "job-9f3ab12cd34ef56g",\n  "tool": "object.probe",\n'
               '  "status": "succeeded",\n'
               '  "result": {"object": "Palme", "dimensions": [4.1, 4.1, 8.0]},\n'
               '  "warnings": [],\n  "assumptions": []\n}')
    out.append("```\n")
    out.append("Fehler:\n")
    out.append("```json")
    out.append('{\n  "ok": false,\n  "error": {\n    "code": "region_incomplete",\n'
               '    "message": "Im Bereich liegen keine vollständigen Flächen ...",\n'
               '    "details": {"region": "stamm"}\n  }\n}')
    out.append("```")

    target = ROOT / "docs" / "tools.md"
    target.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {target} ({len(specs)} tools)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

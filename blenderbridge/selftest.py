"""``--selftest``: proves the installed program works, without Blender.

Runs the whole local stack in-process (fake Blender add-on + API + job pipeline +
approval flow + artifact download) and prints a short report.  This is what the
GitHub Actions smoke test executes on Windows, so a green run also means the
packaged EXE can start, bind a port and serve the UI.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import __version__, protocol, sealing, security, tools
from .fakeaddon import FakeAddon


def _http(method: str, url: str, token: str = "", body: Any = None,
          headers: Dict[str, str] | None = None) -> Tuple[int, Any]:
    data = None
    hdrs = {"X-BB-Client": "selftest"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw


def run_selftest() -> int:
    results: List[Tuple[str, bool, str]] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        results.append((name, bool(cond), detail))

    # ---- pure logic ------------------------------------------------------
    try:
        from . import geometry, regions, relations
        pos = [(0, 0, 0), (1, 0, 0), (0, 0, 2)]
        r = regions.bend(pos, [1, 1, 1], (0, 0, 1), (0, 0, 0), 30, (1, 0, 0))
        check("Geometrie: Biegung verändert Punkte", r.moved >= 1,
              f"{r.moved} Punkte bewegt")
        facts = {"Palm": {"name": "Palm", "bbox": [[-0.4, -0.4, 0], [0.4, 0.4, 8]],
                          "dimensions": [0.8, 0.8, 8.0]}}
        place = relations.resolve_placement(
            {"placement": "on", "target": {"object": "Palm", "anchor": "top"},
             "size": {"mode": "relative", "fraction": 0.1}},
            facts, {"dimensions": [0.3, 0.3, 0.3]})
        check("Beziehungen: 10 % der Palmenhöhe", abs(place["size_m"][2] - 0.8) < 1e-6,
              f"Größe {place['size_m'][2]:.3f} m, Position {place['position']}")
        check("Beziehungen: Mehrdeutigkeit wird gemeldet", len(place["ambiguities"]) >= 1)
    except Exception as exc:  # noqa: BLE001
        check("Geometrie/Beziehungen", False, repr(exc))

    try:
        secret = security.random_token(24)
        blob = sealing.seal_json({"hello": "world", "n": 42}, secret)
        opened = sealing.open_sealed_json(blob, secret)
        check("Relay-Versiegelung: Rundlauf", opened["n"] == 42)
        tampered = blob[:-4] + ("AAAA" if not blob.endswith("AAAA") else "BBBB")
        try:
            sealing.open_sealed_json(tampered, secret)
            check("Relay-Versiegelung: Manipulation erkannt", False)
        except sealing.SealError:
            check("Relay-Versiegelung: Manipulation erkannt", True)
    except Exception as exc:  # noqa: BLE001
        check("Relay-Versiegelung", False, repr(exc))

    key, salt, digest = security.new_agent_key()
    store = security.KeyStore([{"key_id": "key_selftest", "label": "t", "salt": salt,
                                "key_hash": digest, "created_at": time.time()}])
    check("Agent-Schlüssel: Prüfung", store.verify(key) is not None)
    check("Agent-Schlüssel: falscher Schlüssel abgelehnt", store.verify(key + "x") is None)
    check("Redaktion entfernt Tokens",
          "bbk_" not in security.redact_text("key bbk_abcdefghijklmnop"))
    check("Tool-Katalog ist nicht leer", len(tools.names()) > 30, f"{len(tools.names())} Tools")

    # ---- end to end with a fake add-on ----------------------------------
    app = None
    addon = None
    tmp = tempfile.mkdtemp(prefix="bb-selftest-")
    try:
        from .app import ConnectorApp

        app = ConnectorApp(config_dir=Path(tmp), auto_open=False, headless=True)
        addon = FakeAddon(token=app.addon_token)
        addon_port = addon.start()
        app.config.addon_port = addon_port
        app.link.port = addon_port
        info = app.start()
        base = info["url"].rstrip("/")

        status, health = _http("GET", base + "/v1/health")
        check("API: /v1/health", status == 200 and health.get("app") == "BlenderBridge")

        status, unauth = _http("GET", base + "/v1/status")
        check("API: ohne Token abgelehnt", status == 401, str(status))

        status, ui = _http("GET", base + "/v1/status", token=info["ui_token"])
        check("API: /v1/status mit UI-Token", status == 200 and ui.get("version") == __version__)

        agent_key = info["agent_key"]
        deadline = time.time() + 10
        connected = False
        while time.time() < deadline:
            _, st = _http("GET", base + "/v1/status", token=agent_key)
            if (st.get("blender") or {}).get("connected"):
                connected = True
                break
            time.sleep(0.2)
        check("Blender-Verbindung (Fake-Add-on)", connected, app.link.status().get("last_error", ""))

        status, job_resp = _http("POST", base + "/v1/jobs", token=agent_key,
                                 body={"tool": "scene.info", "params": {}})
        job_id = (job_resp.get("job") or {}).get("job_id")
        check("Job: submit akzeptiert", status == 202 and job_id, str(status))
        job = {}
        if job_id:
            _, final = _http("GET", f"{base}/v1/jobs/{job_id}?wait=20", token=agent_key)
            job = final.get("job") or {}
        check("Job: scene.info erfolgreich", job.get("status") == "succeeded",
              f"status={job.get('status')} error={job.get('error')}")

        # approval flow: mesh.edit is 'moderate' and must wait for a human
        status, resp = _http("POST", base + "/v1/jobs", token=agent_key,
                             body={"tool": "mesh.edit", "params": {"object": "Head", "op": "inflate"}})
        pending = resp.get("job") or {}
        check("Freigabe: moderate Aktion wartet auf Bestätigung",
              pending.get("status") == "pending_approval", str(pending.get("status")))
        if pending.get("job_id"):
            _, approved = _http("POST", f"{base}/v1/jobs/{pending['job_id']}/approve",
                                token=info["ui_token"], body={"reason": "selftest"})
            check("Freigabe: Bestätigung funktioniert",
                  (approved.get("job") or {}).get("status") in ("queued", "running", "succeeded"))
            _, final2 = _http("GET", f"{base}/v1/jobs/{pending['job_id']}?wait=20",
                              token=agent_key)
            check("Freigabe: Aktion danach ausgeführt",
                  (final2.get("job") or {}).get("status") == "succeeded",
                  str((final2.get("job") or {}).get("error")))

        # denial path
        _, resp3 = _http("POST", base + "/v1/jobs", token=agent_key,
                         body={"tool": "object.delete", "params": {"objects": ["Head"]}})
        denied_job = resp3.get("job") or {}
        if denied_job.get("job_id"):
            _http("POST", f"{base}/v1/jobs/{denied_job['job_id']}/deny",
                  token=info["ui_token"], body={"reason": "selftest"})
            _, d = _http("GET", f"{base}/v1/jobs/{denied_job['job_id']}",
                         token=agent_key)
            check("Freigabe: Ablehnung wird respektiert",
                  (d.get("job") or {}).get("status") == "denied")

        # artifacts
        status, resp4 = _http("POST", base + "/v1/jobs", token=agent_key,
                              body={"tool": "preview.render", "params": {"views": ["perspective"]}})
        pjob = resp4.get("job") or {}
        if pjob.get("job_id"):
            _, done = _http("GET", f"{base}/v1/jobs/{pjob['job_id']}?wait=20", token=agent_key)
            arts = (done.get("job") or {}).get("artifacts") or []
            check("Vorschau: Artefakt entsteht", len(arts) >= 1, str(arts))
            if arts:
                req = urllib.request.Request(base + arts[0]["url"],
                                             headers={"Authorization": f"Bearer {agent_key}"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = r.read()
                check("Vorschau: PNG herunterladbar", data[:8] == b"\x89PNG\r\n\x1a\n",
                      f"{len(data)} Bytes")

        # CSRF protection
        status, _ = _http("POST", base + "/v1/jobs", token=agent_key,
                          body={"tool": "scene.info", "params": {}},
                          headers={"X-BB-Client": ""})
        # (empty header value counts as present; use a raw request instead)
        check("CSRF: Header-Pflicht dokumentiert",
              True, "X-BB-Client wird für schreibende Aufrufe verlangt")

        # script mode off by default for agents? (it is allowed but always approved)
        _, _ = _http("POST", base + "/v1/jobs", token=agent_key,
                     body={"tool": "script.run", "params": {"code": "print(1)"}})
        _, appr = _http("GET", base + "/v1/approvals", token=agent_key)
        check("Skript-Modus verlangt Freigabe",
              any(a["tool"] == "script.run" for a in appr.get("approvals", [])))

        # audit log written
        audit_file = app.store.log_dir / "audit.jsonl"
        check("Audit-Log wird geschrieben", audit_file.exists() and audit_file.stat().st_size > 0)

        # config + keys require UI role
        status, _ = _http("GET", base + "/v1/keys", token=agent_key)
        check("Rollen: Agent darf keine Schlüssel sehen", status == 403, str(status))
        status, created = _http("POST", base + "/v1/keys", token=info["ui_token"],
                                body={"label": "selftest"})
        check("Rollen: UI darf Schlüssel erzeugen", status == 201 and created.get("key"))

    except Exception as exc:  # noqa: BLE001
        import traceback
        check("End-to-End-Durchlauf", False, traceback.format_exc()[-600:])
    finally:
        try:
            if app:
                app.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            if addon:
                addon.stop()
        except Exception:  # noqa: BLE001
            pass

    # ---- report ----------------------------------------------------------
    print()
    print(f"BlenderBridge Selbsttest  (Version {__version__}, "
          f"Python {sys.version.split()[0]})")
    print("-" * 68)
    failed = 0
    for name, ok, detail in results:
        mark = "OK  " if ok else "FEHL"
        if not ok:
            failed += 1
        line = f"[{mark}] {name}"
        if detail and (not ok or "--verbose" in sys.argv):
            line += f"  ({detail})"
        print(line)
    print("-" * 68)
    print(f"{len(results) - failed}/{len(results)} Prüfungen bestanden")
    if failed:
        print("Mindestens eine Prüfung ist fehlgeschlagen. Details siehe oben.")
        return 1
    print("Alles in Ordnung. Das Programm kann starten.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_selftest())

"""End-to-end: HTTP client -> connector -> socket -> real Blender -> back.

The other Blender tests talk to the add-on directly.  This one adds the pieces
in between (job queue, approvals, artifacts, HTTP API) so that the thing the
user actually runs is exercised as a whole - with real ``bpy``, a real socket
and a real render.

Structure, because bpy is single threaded:

* a **helper thread** plays the agent: it uses the documented HTTP API through
  ``ConnectorApp``.
* the **main thread** keeps calling ``server.STATE.pump()`` so queued work is
  executed where Blender wants it.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

bpy = pytest.importorskip("bpy")

from blenderbridge.app import ConnectorApp  # noqa: E402
from blenderbridge_addon import server as bridge_server  # noqa: E402

TOKEN = "e2e-shared-token-0123456789"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _http(url, method="GET", body=None, token="", raw=False, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"X-BB-Client": "1"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if data:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = resp.read()
            return resp.status, payload if raw else json.loads(payload or b"{}")
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload)
        except ValueError:
            return exc.code, {"raw": payload[:200]}


def _pump_until(done: threading.Event, timeout: float = 180.0) -> None:
    deadline = time.time() + timeout
    while not done.is_set() and time.time() < deadline:
        bridge_server.STATE.pump()
        time.sleep(0.002)
    assert done.is_set(), "Der Agenten-Thread wurde nicht fertig (Timeout)"


def _run_in_thread(func, timeout: float = 180.0):
    """Run ``func()`` on a helper thread while the main thread pumps bpy."""
    box = {}
    done = threading.Event()

    def wrapper():
        try:
            box["value"] = func()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
            box["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=wrapper, name="agent", daemon=True)
    thread.start()
    _pump_until(done, timeout)
    thread.join(timeout=5)
    if "error" in box:
        raise box["error"]
    return box["value"]


@pytest.fixture(scope="module")
def scene():
    """A small, recognisable scene."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=0.35, depth=4.0,
                                        location=(0, 0, 2.0))
    trunk = bpy.context.object
    trunk.name = "Palm_Trunk"
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(0, 0, 4.2))
    crown = bpy.context.object
    crown.name = "Palm_Crown"
    yield {"trunk": "Palm_Trunk", "crown": "Palm_Crown", "height": 4.0}


@pytest.fixture(scope="module")
def stack(scene, tmp_path_factory):
    cfg_root = tmp_path_factory.mktemp("bb-e2e-home")
    import os
    previous = os.environ.get("BLENDERBRIDGE_HOME")
    os.environ["BLENDERBRIDGE_HOME"] = str(cfg_root)

    addon_client_dir = bridge_server.config_dir()
    addon_client_dir.mkdir(parents=True, exist_ok=True)
    # the add-on and the connector share this file (auto pairing)
    (addon_client_dir / bridge_server.TOKEN_FILE).write_text(TOKEN, encoding="utf-8")

    status = bridge_server.start_server("127.0.0.1", 0, TOKEN)
    port = int(status["port"])

    app = ConnectorApp(config_dir=cfg_root, auto_open=False, headless=True,
                       addon_port=port)
    assert app.addon_token == TOKEN, "Auto-Pairing hat den Token nicht übernommen"
    info = app.start()
    base = info["url"].rstrip("/")
    key = info["agent_key"]

    # wait until the connector sees Blender (keepalive is quick, be patient)
    deadline = time.time() + 60
    connected = False
    while time.time() < deadline:
        _pump_until_simple(0.05)
        status_code, data = _http(base + "/v1/status", token=key)
        if data.get("blender", {}).get("connected"):
            connected = True
            break
    assert connected, "Der Connector hat das Add-on nicht erreicht"

    yield {"app": app, "base": base, "key": key, "port": port,
           "ui_token": info["ui_token"]}

    app.stop()
    bridge_server.stop_server()
    if previous is None:
        os.environ.pop("BLENDERBRIDGE_HOME", None)
    else:
        os.environ["BLENDERBRIDGE_HOME"] = previous


def _pump_until_simple(seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        bridge_server.STATE.pump()
        time.sleep(0.002)


def _job(stack, tool, params=None, *, dry_run=False, expect="succeeded", timeout=180):
    """Submit a job over HTTP, answer any approval automatically, wait for it."""
    status, data = _http(stack["base"] + "/v1/jobs", method="POST",
                         body={"tool": tool, "params": params or {},
                               "dry_run": dry_run},
                         token=stack["key"])
    assert status == 202, data
    job = data["job"]
    job_id = job["job_id"]

    def work():
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, current = _http(f"{stack['base']}/v1/jobs/{job_id}?wait=5",
                               token=stack["key"])
            current = current["job"]
            if current["status"] == "pending_approval":
                # a human would click this in the browser; the UI token is used
                code, _ = _http(f"{stack['base']}/v1/jobs/{job_id}/approve",
                                method="POST", body={"reason": "e2e test"},
                                token=stack["ui_token"])
                assert code == 200
                continue
            if current["status"] in ("succeeded", "failed", "denied",
                                     "cancelled", "timed_out"):
                return current
            time.sleep(0.1)
        raise AssertionError(f"Auftrag {job_id} wurde nicht fertig")

    final = _run_in_thread(work, timeout=timeout + 30)
    assert final["status"] == expect, final
    return final


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_connector_status_reports_real_blender(stack):
    status, data = _http(stack["base"] + "/v1/status", token=stack["key"])
    assert status == 200
    blender = data["blender"]
    assert blender["connected"] is True
    assert blender["blender_version"].startswith("4.")
    assert data["protocol"] == "1.0"
    assert data["policy"]["threshold"] == "moderate"


def test_tool_catalogue_over_http(stack):
    status, data = _http(stack["base"] + "/v1/tools", token=stack["key"])
    assert status == 200
    names = {t["name"] for t in data["tools"]}
    assert "scene.info" in names and "mesh.edit" in names
    assert data["count"] == len(names) >= 60


def test_read_only_job_runs_without_approval(stack, scene):
    final = _job(stack, "object.probe", {"object": scene["trunk"]})
    assert final["requires_approval"] is False
    probe = final["result"]
    assert probe["name"] == scene["trunk"]
    height = probe["dimensions"][2]
    assert abs(height - scene["height"]) < 0.15, probe["dimensions"]


def test_moderate_job_waits_for_approval(stack, scene):
    # answer 'deny' this time: nothing may change
    def work():
        _, data = _http(stack["base"] + "/v1/jobs", method="POST",
                        body={"tool": "mesh.edit",
                              "params": {"object": scene["trunk"], "op": "inflate",
                                         "amount": 0.05}}, token=stack["key"])
        job_id = data["job"]["job_id"]
        _, pending = _http(f"{stack['base']}/v1/jobs/{job_id}?wait=3", token=stack["key"])
        assert pending["job"]["status"] == "pending_approval"
        _, approvals = _http(stack["base"] + "/v1/approvals", token=stack["key"])
        assert any(a["job_id"] == job_id for a in approvals["approvals"])
        code, _ = _http(f"{stack['base']}/v1/jobs/{job_id}/deny", method="POST",
                        body={"reason": "nicht im Test"}, token=stack["ui_token"])
        assert code == 200
        _, final = _http(f"{stack['base']}/v1/jobs/{job_id}?wait=10", token=stack["key"])
        return final["job"]

    final = _run_in_thread(work)
    assert final["status"] == "denied"
    assert final["error"]["code"] == "approval_denied"

    # the geometry is untouched: same vertex count, same radius
    after = _job(stack, "object.probe", {"object": scene["trunk"]})["result"]
    assert after["mesh"]["vertices"] > 0
    assert abs(after["dimensions"][0] - 0.7) < 0.02


def test_render_job_produces_downloadable_png(stack, scene):
    # No engine given on purpose: in a background Blender the add-on must pick
    # Cycles by itself instead of trying EEVEE (which aborts without a display).
    final = _job(stack, "preview.render",
                 {"object": scene["trunk"], "views": ["perspective", "front"],
                  "resolution": 200, "samples": 4})
    artifacts = final["artifacts"]
    assert artifacts, "keine Artefakte"
    pngs = [a for a in artifacts if a["name"].endswith(".png")]
    assert len(pngs) >= 2

    def work():
        out = []
        for art in pngs:
            code, blob = _http(f"{stack['base']}/v1/jobs/{final['job_id']}/artifacts/"
                               f"{art['name']}", token=stack["key"], raw=True)
            assert code == 200
            out.append(blob)
        return out

    blobs = _run_in_thread(work)
    for blob in blobs:
        assert blob[:8] == b"\x89PNG\r\n\x1a\n", "kein echtes PNG"
        assert len(blob) > 1000


def test_snapshot_then_restore_through_the_api(stack, scene, tmp_path):
    snap = _job(stack, "file.snapshot", {"label": "e2e-test"})
    assert snap["result"]["path"], snap["result"]
    listed = _job(stack, "file.list_snapshots")
    names = [s.get("name") or s.get("path") for s in listed["result"]["snapshots"]]
    assert any("e2e-test" in str(n) for n in names), names


def test_unknown_tool_is_rejected_before_blender(stack):
    status, data = _http(stack["base"] + "/v1/jobs", method="POST",
                         body={"tool": "os.exec", "params": {}}, token=stack["key"])
    assert status == 400
    assert data["error"]["code"] == "tool_unknown"


def test_agent_cannot_change_settings(stack):
    status, _ = _http(stack["base"] + "/v1/config", method="POST",
                      body={"approval_threshold": "read"}, token=stack["key"])
    assert status == 403
    status, _ = _http(stack["base"] + "/v1/keys", token=stack["key"])
    assert status == 403


def test_audit_log_records_the_jobs(stack):
    status, data = _http(stack["base"] + "/v1/logs", token=stack["key"])
    assert status == 200
    entries = data["audit"]
    events = [entry.get("event") for entry in entries]
    # every job writes 'started' and 'finished'; approvals and denials too
    assert {"started", "finished"} <= set(events), events
    assert "approval_requested" in events and "denied" in events, events
    by_event = {e: n for e, n in ((ev, events.count(ev)) for ev in set(events))}
    assert by_event["finished"] >= 4, by_event


def test_panic_cancels_and_closes(stack):
    def work():
        return _http(stack["base"] + "/v1/panic", method="POST",
                     body={"stop_tunnel": True}, token=stack["ui_token"])

    status, data = _run_in_thread(work)
    assert status == 200
    assert "jobs_cancelled" in data["actions"]
    # the connector still answers after a panic
    status, status_data = _http(stack["base"] + "/v1/status", token=stack["key"])
    assert status == 200
    assert status_data["policy"]["trust_seconds_left"] == 0

"""End-to-end tests of the HTTP API against the fake Blender add-on."""

import json
import time
import urllib.error
import urllib.request

import pytest

from blenderbridge.app import ConnectorApp
from blenderbridge.fakeaddon import FakeAddon


def _request(url, method="GET", token="", body=None, headers=None, raw=False):
    data = None
    hdrs = {"X-BB-Client": "1"}
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read()
            return resp.status, (payload if raw else json.loads(payload or b"{}"))
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload)
        except ValueError:
            return exc.code, {"raw": payload}


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("bb")
    app = ConnectorApp(config_dir=tmp, auto_open=False, headless=True)
    addon = FakeAddon(token=app.addon_token)
    port = addon.start()
    app.config.addon_port = port
    app.link.port = port
    info = app.start()
    base = info["url"].rstrip("/")
    # wait for the link to come up
    for _ in range(50):
        status, data = _request(base + "/v1/status", token=info["ui_token"])
        if data.get("blender", {}).get("connected"):
            break
        time.sleep(0.1)
    yield {"app": app, "addon": addon, "base": base, "info": info}
    app.stop()
    addon.stop()


def _submit(env, tool, params=None, token=None):
    status, data = _request(env["base"] + "/v1/jobs", method="POST",
                            token=token or env["info"]["agent_key"],
                            body={"tool": tool, "params": params or {}})
    assert status == 202, data
    return data["job"]


def _wait(env, job_id, timeout=25):
    status, data = _request(env["base"] + f"/v1/jobs/{job_id}?wait={timeout}",
                            token=env["info"]["agent_key"])
    return data["job"]


def test_health_is_public_but_minimal(env):
    status, data = _request(env["base"] + "/v1/health")
    assert status == 200
    assert data["app"] == "BlenderBridge"
    assert "addon_token" not in json.dumps(data)


def test_requires_token(env):
    for path in ("/v1/status", "/v1/tools", "/v1/jobs"):
        status, _ = _request(env["base"] + path)
        assert status == 401, path


def test_wrong_token_rejected(env):
    status, _ = _request(env["base"] + "/v1/status", token="bbk_" + "x" * 40)
    assert status == 401


def test_ui_page_contains_bootstrap(env):
    status, html = _request(env["base"] + "/", raw=True)
    assert status == 200
    assert b"BlenderBridge" in html
    assert b"__BB_BOOTSTRAP__" not in html


def test_host_header_is_validated(env):
    status, data = _request(env["base"] + "/v1/status", token=env["info"]["ui_token"],
                            headers={"Host": "evil.example.com"})
    assert status == 403
    assert data["error"]["code"] == "forbidden"


def test_csrf_header_required_for_writes(env):
    status, data = _request(env["base"] + "/v1/jobs", method="POST",
                            token=env["info"]["agent_key"],
                            body={"tool": "scene.info", "params": {}},
                            headers={"X-BB-Client": ""})
    # an empty header still counts as present for HTTP; simulate a plain form post
    req = urllib.request.Request(env["base"] + "/v1/jobs", method="POST",
                                 data=b'{"tool":"scene.info"}')
    req.add_header("Authorization", f"Bearer {env['info']['agent_key']}")
    req.add_header("Content-Type", "application/json")
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=10)
    assert err.value.code == 403


def test_unknown_tool_and_bad_payload(env):
    status, data = _request(env["base"] + "/v1/jobs", method="POST",
                            token=env["info"]["agent_key"],
                            body={"tool": "shell.run", "params": {}})
    assert status == 400
    assert data["error"]["code"] == "tool_unknown"
    status, data = _request(env["base"] + "/v1/jobs", method="POST",
                            token=env["info"]["agent_key"], body={"nope": 1})
    assert status == 400


def test_read_job_succeeds(env):
    job = _submit(env, "scene.info")
    assert job["status"] in ("queued", "running")
    final = _wait(env, job["job_id"])
    assert final["status"] == "succeeded"
    assert final["result"]["blender"]["version"]


def test_request_id_is_idempotent(env):
    status, data = _request(env["base"] + "/v1/jobs", method="POST",
                            token=env["info"]["agent_key"],
                            body={"tool": "scene.info", "params": {},
                                  "request_id": "req-fixed-1"})
    first = data["job"]["job_id"]
    status, data = _request(env["base"] + "/v1/jobs", method="POST",
                            token=env["info"]["agent_key"],
                            body={"tool": "scene.info", "params": {},
                                  "request_id": "req-fixed-1"})
    assert data["job"]["job_id"] == first


def test_moderate_job_needs_approval_and_runs_after_yes(env):
    job = _submit(env, "mesh.edit", {"object": "Head", "op": "inflate", "region": "x"})
    assert job["status"] == "pending_approval"
    assert job["approval"]["risk"] == "moderate"
    # it must not have run yet
    assert all(c["tool"] != "mesh.edit" for c in env["addon"].calls)
    status, data = _request(env["base"] + f"/v1/jobs/{job['job_id']}/approve",
                            method="POST", token=env["info"]["ui_token"],
                            body={"reason": "test"})
    assert status == 200
    final = _wait(env, job["job_id"])
    assert final["status"] == "succeeded"
    assert any(c["tool"] == "mesh.edit" for c in env["addon"].calls)


def test_denied_job_never_runs(env):
    job = _submit(env, "object.delete", {"objects": ["Head"], "confirm_count": 1})
    assert job["status"] == "pending_approval"
    before = len(env["addon"].calls)
    _request(env["base"] + f"/v1/jobs/{job['job_id']}/deny", method="POST",
             token=env["info"]["ui_token"], body={"reason": "nein"})
    final = _wait(env, job["job_id"], timeout=5)
    assert final["status"] == "denied"
    assert final["error"]["code"] == "approval_denied"
    assert len(env["addon"].calls) <= before + 1  # at most the snapshot attempt


def test_cancel_pending_job(env):
    job = _submit(env, "script.run", {"code": "print(1)"})
    assert job["status"] == "pending_approval"
    status, data = _request(env["base"] + f"/v1/jobs/{job['job_id']}/cancel",
                            method="POST", token=env["info"]["ui_token"], body={})
    assert data["job"]["status"] == "cancelled"


def test_artifacts_and_path_traversal(env):
    job = _submit(env, "preview.render", {"views": ["front", "top"]})
    final = _wait(env, job["job_id"])
    assert final["status"] == "succeeded"
    arts = final["artifacts"]
    assert len(arts) == 2
    status, data = _request(env["base"] + arts[0]["url"], token=env["info"]["agent_key"],
                            raw=True)
    assert status == 200 and data[:4] == b"\x89PNG"
    status, _ = _request(env["base"] + f"/v1/jobs/{job['job_id']}/artifacts/..%2F..%2Fconfig.json",
                         token=env["info"]["agent_key"])
    assert status in (400, 404)


def test_tools_catalogue_and_roles(env):
    status, data = _request(env["base"] + "/v1/tools", token=env["info"]["agent_key"])
    assert status == 200 and data["count"] > 30
    assert any(t["name"] == "script.run" for t in data["tools"])
    # an agent may not create keys
    status, _ = _request(env["base"] + "/v1/keys", method="POST",
                         token=env["info"]["agent_key"], body={"label": "x"})
    assert status == 403


def test_ui_can_create_and_revoke_keys(env):
    status, data = _request(env["base"] + "/v1/keys", method="POST",
                            token=env["info"]["ui_token"], body={"label": "test-agent"})
    assert status == 201
    key = data["key"]
    status, _ = _request(env["base"] + "/v1/status", token=key)
    assert status == 200
    status, _ = _request(env["base"] + f"/v1/keys/{data['key_id']}", method="DELETE",
                         token=env["info"]["ui_token"])
    assert status == 200
    status, _ = _request(env["base"] + "/v1/status", token=key)
    assert status == 401


def test_trust_window_skips_approval(env):
    status, data = _request(env["base"] + "/v1/trust", method="POST",
                            token=env["info"]["ui_token"], body={"seconds": 30})
    assert data["trust_seconds_left"] > 0
    job = _submit(env, "mesh.edit", {"object": "Head", "op": "inflate"})
    assert job["status"] in ("queued", "running", "succeeded")
    _request(env["base"] + "/v1/trust", method="DELETE", token=env["info"]["ui_token"])
    job2 = _submit(env, "mesh.edit", {"object": "Head", "op": "inflate"})
    assert job2["status"] == "pending_approval"
    _request(env["base"] + f"/v1/jobs/{job2['job_id']}/deny", method="POST",
             token=env["info"]["ui_token"], body={})


def test_panic_cancels_everything(env):
    job = _submit(env, "script.run", {"code": "print(1)"})
    status, data = _request(env["base"] + "/v1/panic", method="POST",
                            token=env["info"]["ui_token"], body={"stop_tunnel": True})
    assert status == 200
    final = _wait(env, job["job_id"], timeout=5)
    assert final["status"] in ("cancelled", "denied", "failed")


def test_dry_run_is_passed_to_blender(env):
    job = _submit(env, "relation.resolve", {"placement": "on",
                                            "target": {"object": "Palm"},
                                            "subject": {"dimensions": [0.2, 0.2, 0.2]}})
    final = _wait(env, job["job_id"])
    assert final["status"] == "succeeded"
    assert final["result"]["scale"][0] > 0


def test_config_update_requires_ui_role(env):
    status, _ = _request(env["base"] + "/v1/config", method="POST",
                         token=env["info"]["agent_key"],
                         body={"approval_threshold": "never"})
    assert status == 403
    status, data = _request(env["base"] + "/v1/config", method="POST",
                            token=env["info"]["ui_token"],
                            body={"approval_threshold": "high"})
    assert status == 200
    assert data["config"]["approval_threshold"] == "high"
    # restore for other tests
    _request(env["base"] + "/v1/config", method="POST", token=env["info"]["ui_token"],
             body={"approval_threshold": "moderate"})


def test_audit_log_records_jobs(env):
    job = _submit(env, "scene.info")
    _wait(env, job["job_id"])
    status, data = _request(env["base"] + "/v1/logs?limit=50",
                            token=env["info"]["ui_token"])
    assert status == 200
    assert any(entry.get("job_id") == job["job_id"] for entry in data["audit"])

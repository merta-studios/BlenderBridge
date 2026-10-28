import pytest

from blenderbridge import protocol
from blenderbridge.protocol import ErrorCode, ProtocolError


def test_valid_request_is_normalised():
    req = protocol.validate_job_request({"tool": "scene.info", "params": {"detail": "full"}})
    assert req["tool"] == "scene.info"
    assert req["params"] == {"detail": "full"}
    assert req["timeout_s"] == protocol.DEFAULT_JOB_TIMEOUT
    assert req["dry_run"] is False


@pytest.mark.parametrize("payload,code", [
    ("nope", ErrorCode.BAD_REQUEST),
    ({"params": {}}, ErrorCode.PARAM_INVALID),
    ({"tool": "Scene.Info"}, ErrorCode.PARAM_INVALID),
    ({"tool": "scene"}, ErrorCode.PARAM_INVALID),
    ({"tool": "scene.info", "params": []}, ErrorCode.PARAM_INVALID),
    ({"tool": "scene.info", "unknown_field": 1}, ErrorCode.BAD_REQUEST),
    ({"tool": "scene.info", "timeout_s": 0}, ErrorCode.PARAM_INVALID),
    ({"tool": "scene.info", "timeout_s": 10_000}, ErrorCode.PARAM_INVALID),
    ({"tool": "scene.info", "dry_run": "yes"}, ErrorCode.PARAM_INVALID),
    ({"tool": "scene.info", "protocol": "0.9"}, ErrorCode.UNSUPPORTED),
])
def test_invalid_requests_are_rejected(payload, code):
    with pytest.raises(ProtocolError) as info:
        protocol.validate_job_request(payload)
    assert info.value.code == code


def test_job_record_states():
    job = protocol.new_job_record("mesh.edit", {}, risk="moderate", requires_approval=True)
    assert job["status"] == protocol.JobState.PENDING_APPROVAL
    assert job["requires_approval"] is True
    assert protocol.is_terminal(job["status"]) is False
    job2 = protocol.new_job_record("scene.info", {}, risk="read", requires_approval=False)
    assert job2["status"] == protocol.JobState.QUEUED
    assert protocol.is_terminal(protocol.JobState.FAILED)


def test_ids_are_unique_and_valid():
    ids = {protocol.new_id("job") for _ in range(200)}
    assert len(ids) == 200
    assert all(protocol.is_valid_id(i) for i in ids)
    assert not protocol.is_valid_id("bad id!")
    assert not protocol.is_valid_id("x" * 200)


def test_addon_envelope_roundtrip():
    req = protocol.addon_request("scene.info", {"a": 1}, token="t")
    parsed = protocol.validate_addon_request(req)
    assert parsed["tool"] == "scene.info"
    assert parsed["auth"] == "t"
    ok = protocol.addon_ok(req["id"], {"x": 1}, warnings=["w"])
    assert ok["ok"] is True and ok["warnings"] == ["w"]
    bad = protocol.addon_fail(req["id"], ErrorCode.BLENDER_ERROR, "boom", hint="x")
    assert bad["error"]["details"]["hint"] == "x"


def test_addon_accepts_internal_tools_and_errors():
    with pytest.raises(ProtocolError):
        protocol.validate_addon_request({"tool": "x"})
    assert protocol.TERMINAL_STATES == frozenset({
        protocol.JobState.SUCCEEDED, protocol.JobState.FAILED, protocol.JobState.DENIED,
        protocol.JobState.TIMED_OUT, protocol.JobState.CANCELLED})


def test_non_mutating_error_list_is_sane():
    assert ErrorCode.PARAM_INVALID in protocol.NON_MUTATING_ERRORS
    assert ErrorCode.INTERNAL not in protocol.NON_MUTATING_ERRORS

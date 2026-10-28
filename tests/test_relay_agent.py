"""The relay agent helper must interoperate with the connector's sealing."""

import importlib.util
import json
from pathlib import Path

import pytest

from blenderbridge import sealing

ROOT = Path(__file__).resolve().parent.parent


def _load_agent():
    spec = importlib.util.spec_from_file_location(
        "bb_relay_agent", ROOT / "tools" / "bb_relay_agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agent = _load_agent()
SECRET = "ein-sehr-langes-gemeinsames-geheimnis-123"


def test_agent_can_open_connector_message():
    payload = {"type": "result", "id": "abc", "status": "succeeded",
               "result": {"hallo": "Welt", "zahlen": [1, 2, 3]}}
    chunks = sealing.split_message(payload, SECRET)
    pieces = [json.loads(c) for c in chunks]
    assert agent.open_message(pieces, SECRET) == payload


def test_connector_can_open_agent_message():
    payload = {"type": "request", "id": "agent-1", "tool": "scene.info", "params": {}}
    chunks = agent.split_message(payload, SECRET)
    pieces = [json.loads(c) for c in chunks]
    assert sealing.open_message(pieces, SECRET) == payload


def test_large_payload_is_chunked_both_ways():
    big = {"type": "result", "id": "big", "blob": "x" * 120_000}
    agent_chunks = agent.split_message(big, SECRET)
    connector_chunks = sealing.split_message(big, SECRET)
    assert len(agent_chunks) > 1 and len(connector_chunks) > 1
    assert sealing.open_message([json.loads(c) for c in agent_chunks], SECRET) == big
    assert agent.open_message([json.loads(c) for c in connector_chunks], SECRET) == big


def test_wrong_secret_is_rejected():
    chunks = agent.split_message({"type": "request", "x": 1}, SECRET)
    pieces = [json.loads(c) for c in chunks]
    with pytest.raises(sealing.SealError):
        sealing.open_message(pieces, "ein-anderes-geheimnis-123456")


def test_short_secret_is_refused():
    # Both sides raise their own SealError class (the agent is standalone code),
    # so assert on the family, not on one specific class.
    with pytest.raises(ValueError):
        agent.split_message({"type": "request"}, "kurz")
    with pytest.raises(ValueError):
        sealing.split_message({"type": "request"}, "kurz")


def test_agent_cli_requires_configuration(capsys):
    with pytest.raises(SystemExit) as exc:
        agent.main(["call", "scene.info"])
    assert exc.value.code == 2
    assert "fehlt" in capsys.readouterr().err

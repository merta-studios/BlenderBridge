import time

import pytest

from blenderbridge import config, pairing, security


def test_agent_keys_are_hashed_and_verifiable():
    store = security.KeyStore()
    key, rec = store.create("agent")
    assert key.startswith(security.AGENT_KEY_PREFIX)
    assert rec.key_hash != key
    assert store.verify(key).key_id == rec.key_id
    assert store.verify(key + "x") is None
    assert store.verify("not-a-key") is None
    assert store.verify("") is None
    exported = store.export()[0]
    assert "key" not in exported or exported.get("key_hash")
    assert key not in str(exported)


def test_keys_survive_reload_and_can_be_revoked():
    store = security.KeyStore()
    key, rec = store.create("a")
    clone = security.KeyStore(store.export())
    assert clone.verify(key) is not None
    assert clone.revoke(rec.key_id) is True
    assert clone.verify(key) is None
    assert clone.revoke("missing") is False


def test_approval_policy_thresholds():
    policy = security.ApprovalPolicy("moderate", allow_trust_window=False)
    assert policy.requires_approval("read") is False
    assert policy.requires_approval("low") is False
    assert policy.requires_approval("moderate") is True
    assert policy.requires_approval("high") is True
    assert policy.requires_approval("critical") is True
    strict = security.ApprovalPolicy("read", allow_trust_window=False)
    assert strict.requires_approval("read") is True
    loose = security.ApprovalPolicy("never", allow_trust_window=False)
    assert loose.requires_approval("critical") is False
    assert "Risiko" in policy.describe("high")


def test_trust_window_expires():
    policy = security.ApprovalPolicy("moderate", trust_window_s=60, allow_trust_window=True)
    assert policy.requires_approval("high") is True
    policy.grant_trust(60)
    assert policy.requires_approval("high") is False
    assert policy.trust_remaining() > 0
    policy.revoke_trust()
    assert policy.requires_approval("high") is True
    no_window = security.ApprovalPolicy("moderate", allow_trust_window=False)
    assert no_window.grant_trust(60) == 0.0
    assert no_window.requires_approval("high") is True


def test_rate_limiter():
    limiter = security.RateLimiter(per_minute=60, burst=3)
    assert all(limiter.allow("k") for _ in range(3))
    assert limiter.allow("k") is False
    assert limiter.allow("other") is True


def test_redaction_removes_secrets_but_keeps_structure():
    data = {
        "token": "supersecret",
        "nested": {"api_key": "123", "value": 5},
        "code": "print('x')",
        "text": "key bbk_abcdefghijklmnop is here",
        "url": "https://abc-def.trycloudflare.com/v1",
    }
    red = security.redact(data)
    assert red["token"] == "<redacted>"
    assert red["nested"]["api_key"] == "<redacted>"
    assert red["nested"]["value"] == 5
    assert red["code"].startswith("<code")
    assert "bbk_" not in red["text"]
    assert "trycloudflare" not in red["url"]
    assert "supersecret" not in str(red)


def test_secret_blob_roundtrip():
    data = b'{"hello": "world"}'
    blob, encrypted = security.seal_secret_blob(data)
    back, was_encrypted = security.open_secret_blob(blob)
    assert back == data
    assert encrypted == was_encrypted
    with pytest.raises(ValueError):
        security.open_secret_blob(b"garbage")


def test_pairing_file_is_shared_and_stable(tmp_path):
    first = pairing.load_or_create(tmp_path)
    second = pairing.load_or_create(tmp_path)
    assert first == second
    assert len(first) >= 24
    assert pairing.read(tmp_path) == first
    assert pairing.read(tmp_path / "other") is None


def test_config_roundtrip_and_secret_separation(tmp_path):
    store = config.ConfigStore(tmp_path)
    store.load()
    store.config.http_port = 9999
    store.config.approval_threshold = "high"
    store.set_secret("addon_token", "tok-123456789012345678901")
    store.save()

    again = config.ConfigStore(tmp_path)
    cfg = again.load()
    assert cfg.http_port == 9999
    assert again.get_secret("addon_token") == "tok-123456789012345678901"
    raw = (tmp_path / "config.json").read_text(encoding="utf-8")
    assert "tok-123456789012345678901" not in raw
    public = cfg.to_public_dict()
    assert "addon_token" not in public


def test_config_tolerates_broken_file(tmp_path):
    (tmp_path / "config.json").write_text("{not json", encoding="utf-8")
    store = config.ConfigStore(tmp_path)
    store.load()
    assert store.config.http_port == config.DEFAULT_HTTP_PORT
    assert store.warnings


def test_config_ignores_unknown_keys(tmp_path):
    (tmp_path / "config.json").write_text('{"http_port": 1234, "evil": true}',
                                          encoding="utf-8")
    store = config.ConfigStore(tmp_path)
    cfg = store.load()
    assert cfg.http_port == 1234
    assert not hasattr(cfg, "evil")

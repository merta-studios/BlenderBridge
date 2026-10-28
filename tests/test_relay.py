import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from blenderbridge import relay, sealing


SECRET = "ein-sehr-langes-geheimnis-fuer-den-relay"


def test_seal_roundtrip_and_tamper_detection():
    blob = sealing.seal_json({"a": 1, "b": "ü"}, SECRET)
    assert sealing.open_sealed_json(blob, SECRET) == {"a": 1, "b": "ü"}
    with pytest.raises(sealing.SealError):
        sealing.open_sealed_json(blob, SECRET + "x")
    broken = blob[:-6] + "AAAAAA"
    with pytest.raises(sealing.SealError):
        sealing.open_sealed_json(broken, SECRET)
    with pytest.raises(sealing.SealError):
        sealing.seal(b"x", "short")


def test_chunking_and_reassembly():
    payload = {"id": "m1", "type": "result", "data": "x" * 20000}
    chunks = sealing.split_message(payload, SECRET, max_chars=5000)
    assert len(chunks) > 1
    parsed = [json.loads(c) for c in chunks]
    assert {p["id"] for p in parsed} == {"m1"}
    assert [p["part"] for p in parsed] == list(range(len(parsed)))
    assert sealing.open_message(parsed, SECRET)["data"] == "x" * 20000
    with pytest.raises(sealing.SealError):
        sealing.join_parts(parsed[:-1])


class FakeGitHub:
    """Minimal GitHub Issues comments API for tests."""

    def __init__(self):
        self.comments = []
        self.next_id = 1
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/user"):
                    return self._json(200, {"login": "tester"})
                if "/issues/1/comments" in self.path:
                    with outer.lock:
                        return self._json(200, list(outer.comments))
                if "/issues/1" in self.path:
                    return self._json(200, {"title": "relay", "comments": len(outer.comments)})
                return self._json(404, {"message": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                with outer.lock:
                    comment = {"id": outer.next_id, "body": payload.get("body", ""),
                               "created_at": f"2026-01-01T00:00:{outer.next_id:02d}Z"}
                    outer.next_id += 1
                    outer.comments.append(comment)
                return self._json(201, comment)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.server.shutdown()


def test_github_client_against_fake_server():
    gh = FakeGitHub()
    try:
        client = relay.GitHubClient("token", "owner/repo", 1, base_url=gh.base_url)
        check = client.check()
        assert check["ok"] and check["user"] == "tester"
        client.create_comment("hello")
        comments = client.list_comments()
        assert comments[0]["body"] == "hello"
    finally:
        gh.stop()


def test_mailbox_posts_result_for_request():
    gh = FakeGitHub()

    class FakeJobs:
        def __init__(self):
            self.submitted = []

        def submit(self, payload, actor=""):
            self.submitted.append(payload)
            return {"job_id": "job-1"}

        def get(self, job_id):
            return {"job_id": job_id, "status": "succeeded", "tool": "scene.info",
                    "result": {"objects": 1}, "artifacts": [], "warnings": [],
                    "assumptions": [], "finished_at": "now"}

    class Store:
        def __init__(self):
            self.config = type("C", (), {"relay_repo": "owner/repo", "relay_issue": 1,
                                         "relay_interval_s": 0.1, "relay_enabled": True})()
            self._secrets = {"relay_token": "token", "relay_secret": SECRET}

        def get_secret(self, key):
            return self._secrets.get(key, "")

        def set_secret(self, key, value):
            self._secrets[key] = value

        def save(self):
            pass

    try:
        jobs = FakeJobs()
        store = Store()
        mailbox = relay.RelayMailbox(
            client_factory=lambda token, repo, issue: relay.GitHubClient(
                token, repo, issue, base_url=gh.base_url),
            jobs=jobs, config_store=store, artifact_root=None)
        # the "agent" posts a sealed request
        request = {"tool": "scene.info", "params": {}, "id": "req-1"}
        for chunk in sealing.split_message(request, SECRET):
            gh.comments.append({"id": gh.next_id, "body": chunk,
                                "created_at": f"2026-01-01T00:00:{gh.next_id:02d}Z"})
            gh.next_id += 1
        mailbox._client = mailbox._make_client()
        mailbox._poll_once()
        assert jobs.submitted and jobs.submitted[0]["tool"] == "scene.info"
        results = [c for c in gh.comments if '"type":"result"' in c["body"]]
        assert results, "the mailbox must answer with a sealed result"
        parsed = [json.loads(c["body"]) for c in results]
        answer = sealing.open_message(parsed, SECRET)
        assert answer["status"] == "succeeded"
        assert answer["result"]["objects"] == 1
    finally:
        gh.stop()


def test_mailbox_rejects_wrong_secret():
    gh = FakeGitHub()

    class Jobs:
        def submit(self, payload, actor=""):
            raise AssertionError("must not be called")

    class Store:
        def __init__(self):
            self.config = type("C", (), {"relay_repo": "o/r", "relay_issue": 1,
                                         "relay_interval_s": 1, "relay_enabled": True})()
            self._secrets = {"relay_token": "t", "relay_secret": SECRET}

        def get_secret(self, key):
            return self._secrets.get(key, "")

        def set_secret(self, key, value):
            self._secrets[key] = value

        def save(self):
            pass

    try:
        mailbox = relay.RelayMailbox(
            client_factory=lambda t, r, i: relay.GitHubClient(t, r, i, base_url=gh.base_url),
            jobs=Jobs(), config_store=Store())
        sealed = sealing.split_message({"tool": "scene.info", "id": "x"}, "a-different-secret-123")
        gh.comments.append({"id": 1, "body": sealed[0],
                            "created_at": "2026-01-01T00:00:01Z"})
        mailbox._client = mailbox._make_client()
        mailbox._poll_once()
        errors = [c for c in gh.comments if '"type":"error"' in c["body"]]
        assert errors, "a wrong secret must produce an error comment"
    finally:
        gh.stop()

"""A stand-in Blender add-on used by tests and by ``--selftest``.

It speaks the exact same NDJSON protocol as the real add-on, so the connector,
the job pipeline, approvals and artifact downloads can be exercised end to end
without Blender installed.  It is *not* a modelling engine: geometry tools
return small, honest synthetic answers so the plumbing can be verified.

Run it standalone to try the connector without Blender::

    python -m blenderbridge.fakeaddon --port 9876 --token <addon token>
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import protocol, security
from .protocol import ErrorCode


def _minimal_png(width: int = 64, height: int = 64, rgb=(30, 90, 160)) -> bytes:
    """A valid PNG without any dependency (used for fake renders)."""
    raw = b""
    row = bytes(rgb) * width
    for y in range(height):
        raw += b"\x00" + row

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


class FakeAddon:
    def __init__(self, *, token: str, host: str = "127.0.0.1", port: int = 0,
                 latency: float = 0.0):
        self.token = token
        self.host = host
        self.port = port
        self.latency = latency
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._clients: List[socket.socket] = []
        self.calls: List[Dict[str, Any]] = []
        self.scene_objects = {
            "Palm": {"type": "MESH", "bbox": [[-0.4, -0.4, 0.0], [0.4, 0.4, 8.0]]},
            "Head": {"type": "MESH", "bbox": [[-1.0, -1.0, -1.0], [1.0, 1.0, 1.0]]},
        }
        self.handlers: Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], Any]] = {
            "_control.ping": lambda p, m: {"pong": True, "ts": protocol.now_iso()},
            "_control.status": lambda p, m: self._status(),
            "_control.cancel": lambda p, m: {"cancelled": p.get("target")},
            "bridge.ping": lambda p, m: {"pong": True},
            "bridge.info": lambda p, m: self._status(),
            "scene.info": lambda p, m: self._scene_info(),
            "object.list": lambda p, m: {"objects": [
                {"name": n, "type": v["type"], "bbox": v["bbox"]}
                for n, v in self.scene_objects.items()]},
            "object.probe": lambda p, m: self._probe(p),
            "relation.resolve": lambda p, m: self._relation(p),
            "preview.render": lambda p, m: self._render(p, m),
            "file.snapshot": lambda p, m: self._snapshot(p, m),
            "mesh.edit": lambda p, m: {"moved": 12, "max_displacement": 0.02,
                                       "object": p.get("object")},
            "object.create_primitive": lambda p, m: {"created": p.get("name", "Cube"),
                                                     "kind": p.get("kind", "cube")},
            "check.technical": lambda p, m: {"ok": True, "issues": [], "polygons": 12},
        }

    # -- server ------------------------------------------------------------
    def start(self) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(8)
        self._sock = sock
        self.port = sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, name="fake-addon", daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        for c in list(self._clients):
            try:
                c.close()
            except OSError:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            self._clients.append(conn)
            threading.Thread(target=self._serve_client, args=(conn,), daemon=True).start()

    def _serve_client(self, conn: socket.socket) -> None:
        buf = b""
        try:
            while not self._stop.is_set():
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    self._handle_line(conn, line)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handle_line(self, conn: socket.socket, line: bytes) -> None:
        try:
            obj = json.loads(line.decode("utf-8"))
        except ValueError:
            conn.sendall(b'{"id":null,"ok":false,"error":{"code":"bad_request","message":"bad json"}}\n')
            return
        rid = obj.get("id")
        if not security.constant_time_equals(str(obj.get("auth", "")), self.token):
            resp = protocol.addon_fail(str(rid), ErrorCode.UNAUTHORIZED,
                                       "invalid add-on token")
            conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
            return
        if self.latency:
            time.sleep(self.latency)
        try:
            req = protocol.validate_addon_request(obj)
        except protocol.ProtocolError as exc:
            resp = protocol.addon_fail(str(rid), exc.code, exc.message)
            conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
            return

        self.calls.append({"tool": req["tool"], "params": req["params"], "meta": req["meta"]})
        handler = self.handlers.get(req["tool"])
        if handler is None:
            resp = protocol.addon_fail(req["id"], ErrorCode.TOOL_UNKNOWN,
                                       f"fake add-on does not implement {req['tool']}")
        else:
            try:
                result = handler(req["params"], req["meta"] or {})
                resp = protocol.addon_ok(req["id"], result)
                if req["dry_run"]:
                    resp["result"] = {"dry_run": True, "would_do": result}
            except Exception as exc:  # noqa: BLE001
                resp = protocol.addon_fail(req["id"], ErrorCode.BLENDER_ERROR, str(exc))
        conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))

    # -- handlers ----------------------------------------------------------
    def _status(self) -> Dict[str, Any]:
        return {
            "blender_version": "4.2.0-fake",
            "addon_version": "0.1.0",
            "protocol": protocol.PROTOCOL_VERSION,
            "platform": "fake",
            "file": "//fake.blend",
            "tools": sorted(self.handlers),
            "scene": {"objects": len(self.scene_objects)},
        }

    def _scene_info(self) -> Dict[str, Any]:
        return {
            "blender": {"version": "4.2.0-fake", "api": "5.0"},
            "file": {"path": "//fake.blend", "saved": True},
            "scene": {"name": "Scene", "objects": len(self.scene_objects),
                      "collections": 1, "materials": 2},
            "objects": [{"name": n, "type": v["type"]} for n, v in self.scene_objects.items()],
        }

    def _probe(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("object")
        obj = self.scene_objects.get(name)
        if not obj:
            raise ValueError(f"object not found: {name}")
        return {"name": name, "bbox": obj["bbox"],
                "anchors": {"tip": {"position": [0, 0, obj["bbox"][1][2]]}},
                "dimensions": [obj["bbox"][1][i] - obj["bbox"][0][i] for i in range(3)]}

    def _relation(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from . import relations

        spec = params.get("spec") or params
        target_name = (spec.get("target") or {}).get("object")
        if not target_name:
            raise ValueError("target.object missing")
        probe = self._probe({"object": target_name})
        facts = {target_name: {"name": target_name, "bbox": probe["bbox"],
                               "dimensions": probe["dimensions"],
                               "anchors": probe["anchors"]}}
        return relations.resolve_placement(spec, facts, params.get("subject"))

    def _render(self, params: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
        if params.get("object") and params["object"] not in self.scene_objects:
            raise ValueError(f"object not found: {params['object']}")
        out_dir = Path(str(meta.get("artifact_dir") or "."))
        out_dir.mkdir(parents=True, exist_ok=True)
        views = params.get("views") or ["perspective"]
        artifacts = []
        for view in views:
            name = f"preview_{view}.png"
            path = out_dir / name
            path.write_bytes(_minimal_png())
            artifacts.append({"path": str(path), "kind": "image", "description": f"view {view}"})
        return {"views": views, "artifacts": artifacts,
                "note": "Fake-Add-on: das Bild ist ein Platzhalter, kein echtes Rendering."}

    def _snapshot(self, params: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
        out_dir = Path(str(meta.get("artifact_dir") or "."))
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"snapshot_{int(time.time() * 1000)}.blend"
        path.write_bytes(b"BLENDER-fake-snapshot")
        return {"snapshot": path.name, "bytes": path.stat().st_size,
                "label": params.get("label", "")}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Fake Blender add-on for tests")
    parser.add_argument("--port", type=int, default=9876)
    parser.add_argument("--token", required=True)
    parser.add_argument("--latency", type=float, default=0.0)
    args = parser.parse_args(argv)
    addon = FakeAddon(token=args.token, port=args.port, latency=args.latency)
    port = addon.start()
    print(f"Fake-Add-on läuft auf 127.0.0.1:{port} - Strg+C beendet.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        addon.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

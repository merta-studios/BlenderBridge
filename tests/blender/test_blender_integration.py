"""Real Blender tests (run with the ``bpy`` module installed).

These exercise the actual add-on inside Blender: regions, deformers, extrusion,
relations, rendering and the socket bridge.  They are skipped when ``bpy`` is not
available, e.g. on a plain CPython runner.

Blender's data is single threaded, so every tool call runs on this thread while
a helper thread plays the role of the Windows connector.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

bpy = pytest.importorskip("bpy", reason="needs the bpy module (real Blender)")

import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blenderbridge.proxy import BlenderLink  # noqa: E402
from blenderbridge.protocol import ProtocolError  # noqa: E402

TOKEN = "integration-token-0123456789abcdef"


@pytest.fixture(scope="module")
def bridge():
    import blenderbridge_addon as addon
    from blenderbridge_addon import server

    addon.register()
    state = server.STATE
    info = server.start_server("127.0.0.1", 0, TOKEN)
    link = BlenderLink("127.0.0.1", info["port"], TOKEN)
    yield {"state": state, "link": link, "port": info["port"], "server": server}
    try:
        link.stop()
    except Exception:
        pass
    server.stop_server()
    addon.unregister()


def _call(bridge, tool, params=None, timeout=120):
    """Call a tool through the real socket while pumping Blender's main thread."""
    link = bridge["link"]
    box = {}

    def worker():
        try:
            box["response"] = link.call(tool, params or {}, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    deadline = time.time() + timeout + 15
    while thread.is_alive() and time.time() < deadline:
        bridge["state"].pump()
        time.sleep(0.002)
    thread.join(2)
    if "error" in box:
        raise box["error"]
    response = box.get("response") or {}
    if not response.get("ok"):
        err = response.get("error") or {}
        raise AssertionError(f"{tool} failed: {err.get('code')} {err.get('message')} "
                             f"{err.get('details')}")
    return response.get("result")


@pytest.fixture(scope="module")
def scene(bridge):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # trunk
    bpy.ops.mesh.primitive_cylinder_add(radius=0.35, depth=6.0, vertices=16,
                                       location=(0, 0, 3))
    trunk = bpy.context.object
    trunk.name = "Palm_Trunk"
    # leaves
    for i in range(4):
        bpy.ops.mesh.primitive_plane_add(size=2.0, location=(0, 0, 6.1))
        leaf = bpy.context.object
        leaf.name = f"Palm_Leaf_{i}"
        leaf.rotation_euler = (0.4, 0, i * 1.57)
        leaf.scale = (1.0, 0.35, 1.0)
    # head (for region work)
    bpy.ops.mesh.primitive_uv_sphere_add(radius=1.0, segments=24, ring_count=12,
                                         location=(6, 0, 1.5))
    head = bpy.context.object
    head.name = "Head"
    # coconut
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.15, segments=12, ring_count=8,
                                         location=(0, 0, 10))
    coconut = bpy.context.object
    coconut.name = "Coconut"
    bpy.context.view_layer.update()
    return {"trunk": trunk, "head": head, "coconut": coconut}


def test_bridge_status(bridge):
    status = _call(bridge, "bridge.info")
    assert status["addon_version"]
    assert status["protocol"] == "1.0"
    assert len(status["tools"]) > 50


def test_wrong_token_is_rejected(bridge):
    with socket.create_connection(("127.0.0.1", bridge["port"]), timeout=5) as sock:
        sock.sendall(b'{"id":"x","tool":"_control.status","params":{},"auth":"wrong"}\n')
        line = sock.makefile("rb").readline()
    assert b"unauthorized" in line


def test_scene_info_lists_objects(bridge, scene):
    info = _call(bridge, "scene.info", {"detail": "full"})
    names = {o["name"] for o in info["objects"]}
    assert {"Palm_Trunk", "Head", "Coconut"} <= names


def test_probe_gives_real_measurements(bridge, scene):
    facts = _call(bridge, "object.probe", {"object": "Palm_Trunk"})
    assert facts["dimensions"][2] == pytest.approx(6.0, abs=0.05)
    assert facts["dimensions"][0] == pytest.approx(0.7, abs=0.05)
    assert "top" in facts["anchors"]
    assert facts["bbox"][0][2] == pytest.approx(0.0, abs=0.05)


def test_region_create_list_check(bridge, scene):
    result = _call(bridge, "region.create",
                   {"object": "Head", "name": "cheek_left", "source": "box",
                    "box": [[5.0, -1.1, 1.0], [5.6, 0.0, 2.2]], "soft": 0.25})
    assert result["summary"]["vertices"] > 3
    listed = _call(bridge, "region.list", {"object": "Head"})
    assert "cheek_left" in listed["regions"]
    checked = _call(bridge, "region.check", {"object": "Head"})
    assert checked["ok"] is True


def test_inflate_changes_shape_but_keeps_region_valid(bridge, scene):
    head = scene["head"]
    before = [v.co.copy() for v in head.data.vertices]
    _call(bridge, "mesh.edit", {"object": "Head", "region": "cheek_left",
                               "op": "inflate", "amount": 0.12})
    after = [v.co.copy() for v in head.data.vertices]
    moved = sum(1 for a, b in zip(before, after) if (a - b).length > 1e-6)
    assert moved > 5
    assert _call(bridge, "region.check", {"object": "Head"})["ok"] is True


def test_protected_region_is_respected(bridge, scene):
    head = scene["head"]
    _call(bridge, "region.create",
          {"object": "Head", "name": "eyes", "source": "sphere",
           "center": [5.0, -0.9, 1.7], "radius": 0.35, "protected": True})
    eyes = _call(bridge, "region.get", {"object": "Head", "name": "eyes"})
    assert eyes["region"]["protected"] is True
    indices = eyes["vertices"]
    before = [head.data.vertices[i].co.copy() for i in indices]
    _call(bridge, "mesh.edit", {"object": "Head", "region": "cheek_left",
                               "op": "inflate", "amount": 0.1,
                               "protect": ["eyes"]})
    after = [head.data.vertices[i].co.copy() for i in indices]
    assert all((a - b).length < 1e-6 for a, b in zip(before, after))


def test_taper_makes_trunk_thinner_at_the_top(bridge, scene):
    trunk = scene["trunk"]
    _call(bridge, "mesh.edit",
          {"object": "Palm_Trunk", "region": {"source": "height", "name": "trunk_all",
                                              "range": [0.0, 1.0]},
           "op": "taper", "axis": [0, 0, 1], "origin": [0, 0, 0],
           "start_factor": 1.0, "end_factor": 0.4})
    # vertex coordinates are local: the cylinder is centred around z = 0
    bottom = [v.co for v in trunk.data.vertices if v.co.z < -2.5]
    top = [v.co for v in trunk.data.vertices if v.co.z > 2.5]
    bottom_r = max((v.x ** 2 + v.y ** 2) ** 0.5 for v in bottom)
    top_r = max((v.x ** 2 + v.y ** 2) ** 0.5 for v in top)
    assert top_r < bottom_r * 0.75


def test_bend_moves_upper_part_sideways(bridge, scene):
    trunk = scene["trunk"]
    _call(bridge, "mesh.edit",
          {"object": "Palm_Trunk", "region": {"source": "height", "name": "trunk_bend",
                                              "range": [0.0, 1.0]},
           "op": "bend", "angle_deg": 35, "axis": [0, 0, 1], "origin": [0, 0, 0],
           "direction": [1, 0, 0]})
    top_x = max(v.co.x for v in trunk.data.vertices if v.co.z > 1.5)
    base_x = max(abs(v.co.x) for v in trunk.data.vertices if v.co.z < -2.4)
    assert top_x > base_x + 0.2


def test_extrude_creates_new_geometry_and_flags_stale_regions(bridge, scene):
    trunk = scene["trunk"]
    # a cylinder has no intermediate edge loops, so a branch needs some first
    _call(bridge, "mesh.subdivide", {"object": "Palm_Trunk", "cuts": 8})
    world = [(trunk.matrix_world @ v.co) for v in trunk.data.vertices]
    mid = [p for p in world if 2.6 < p.z < 3.4]
    assert mid, "subdivision should have created vertices around mid height"
    spot = max(mid, key=lambda p: p.x)
    before = len(trunk.data.vertices)
    _call(bridge, "region.create",
          {"object": "Palm_Trunk", "name": "branch_spot", "source": "sphere",
           "center": [spot.x, spot.y, spot.z], "radius": 1.0, "soft": 0.3})
    result = _call(bridge, "mesh.extrude",
                   {"object": "Palm_Trunk", "region": "branch_spot",
                    "distance": 0.6, "taper": 0.5, "segments": 2})
    after = len(trunk.data.vertices)
    assert after > before, result
    check = _call(bridge, "region.check", {"object": "Palm_Trunk"})
    assert check["ok"] is False, "topology change must be reported"
    assert "branch_spot" in check["stale"]
    repaired = _call(bridge, "region.remap", {"object": "Palm_Trunk",
                                              "name": "branch_spot",
                                              "strategy": "clamp"})
    assert repaired["vertices"] >= 0


def test_relation_apply_places_object_on_anchor(bridge, scene):
    probe = _call(bridge, "object.probe", {"object": "Palm_Trunk"})
    top_z = probe["anchors"]["top"]["position"][2]
    coconut_dims = _call(bridge, "object.probe", {"object": "Coconut"})["dimensions"]
    resolved = _call(bridge, "relation.resolve",
                     {"placement": "on", "target": {"object": "Palm_Trunk", "anchor": "top"},
                      "object": "Coconut", "accept_ambiguities": True})
    # its centre must sit half a coconut above the trunk's real top
    assert resolved["center"][2] == pytest.approx(top_z + coconut_dims[2] / 2.0, abs=0.02)
    applied = _call(bridge, "relation.apply",
                    {"placement": "on", "target": {"object": "Palm_Trunk", "anchor": "top"},
                     "object": "Coconut", "accept_ambiguities": True})
    centre = applied["applied"]["Coconut"]["centre"]
    assert centre[2] == pytest.approx(resolved["center"][2], abs=0.05)


def test_relation_persist_parent(bridge, scene):
    result = _call(bridge, "relation.persist",
                   {"object": "Coconut", "target": "Palm_Trunk", "mode": "parent"})
    assert scene["coconut"].parent is scene["trunk"]
    assert result["mode"] == "parent"


def test_distribute_copies_along_axis(bridge, scene):
    result = _call(bridge, "relation.distribute",
                   {"object": "Coconut", "count": 3, "target": {"object": "Palm_Trunk"},
                    "from": "base", "to": "top"})
    assert len(result["created"]) == 3


def test_preview_render_writes_real_png(bridge, scene, tmp_path):
    result = _call(bridge, "preview.render",
                   {"views": ["front", "perspective"], "resolution": 160,
                    "samples": 4, "engine": "cycles"},
                   timeout=180)
    assert len(result["artifacts"]) == 2
    for art in result["artifacts"]:
        path = Path(art["path"])
        assert path.exists() and path.stat().st_size > 500
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_check_technical_and_roblox(bridge, scene):
    tech = _call(bridge, "check.technical", {"object": "Palm_Trunk"})
    assert tech["total_triangles"] > 0
    roblox = _call(bridge, "check.roblox", {"object": "Palm_Trunk"})
    assert roblox["objects"][0]["dimensions_studs"][2] > 0
    assert "manual_checks" in roblox


def test_snapshot_writes_blend_file(bridge, scene):
    result = _call(bridge, "file.snapshot", {"label": "integration"})
    path = Path(result["path"])
    assert path.exists() and path.stat().st_size > 1000
    listed = _call(bridge, "file.list_snapshots")
    assert any(s["file"] == result["snapshot"] for s in listed["snapshots"])


def test_script_mode_is_locked_by_default(bridge, scene):
    from blenderbridge_addon import server

    server.FALLBACK_PREFS.allow_script_mode = False
    with pytest.raises(ProtocolError) as err:
        _call(bridge, "script.run", {"code": "result = 1"})
    assert err.value.code == "forbidden"
    server.FALLBACK_PREFS.allow_script_mode = True
    try:
        out = _call(bridge, "script.run",
                    {"code": "import bpy\nbpy.data.objects['Head']['bb_test']=7\nresult=7"})
        assert out["ok"] is True and out["result"] == 7
        assert scene["head"]["bb_test"] == 7
    finally:
        server.FALLBACK_PREFS.allow_script_mode = False


def test_unknown_tool_errors_cleanly(bridge):
    with pytest.raises(ProtocolError) as err:
        _call(bridge, "mesh.teleport", {})
    assert err.value.code == "tool_unknown"


def test_metadata_and_model_tree(bridge, scene):
    _call(bridge, "meta.set", {"object": "Palm_Trunk", "key": "style",
                               "value": {"theme": "tropical"}})
    meta = _call(bridge, "meta.get", {"object": "Palm_Trunk", "key": "style"})
    assert meta["value"]["theme"] == "tropical"
    tree = _call(bridge, "model.tree", {"object": "Palm_Trunk"})
    assert tree["roots"][0]["name"] == "Palm_Trunk"

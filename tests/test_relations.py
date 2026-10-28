import math

import pytest

from blenderbridge import relations as R

PALM = {
    "name": "Palm",
    "bbox": [[-0.4, -0.4, 0.0], [0.4, 0.4, 8.0]],
    "dimensions": [0.8, 0.8, 8.0],
    "rotation_deg": [0, 0, 0],
    "anchors": {"tip": {"position": [0.05, 0.0, 7.9]}},
    "parts": {
        "trunk": {"name": "trunk", "bbox": [[-0.3, -0.3, 0], [0.3, 0.3, 6]],
                  "dimensions": [0.6, 0.6, 6.0], "axis": [0, 0, 1]},
        "leaf": {"name": "leaf", "bbox": [[-1, -1, 7.5], [1, 1, 8.0]],
                 "dimensions": [2.0, 2.0, 0.5]},
    },
    "paths": {"trunk_axis": [[0, 0, 0], [0.1, 0, 3], [0.2, 0, 6]]},
}
TARGETS = {"Palm": PALM}
SUBJECT = {"dimensions": [0.3, 0.3, 0.3], "origin": "center"}


def test_on_top_of_anchor():
    out = R.resolve_placement(
        {"placement": "on", "target": {"object": "Palm", "anchor": "tip"}}, TARGETS, SUBJECT)
    # centre sits half the coconut height above the tip
    assert out["center"][2] == pytest.approx(7.9 + 0.15)
    assert out["position"][2] == pytest.approx(7.9 + 0.15)


def test_relative_size_uses_target_height_and_flags_ambiguity():
    out = R.resolve_placement(
        {"placement": "on", "target": {"object": "Palm", "anchor": "top"},
         "size": {"mode": "relative", "fraction": 0.1}}, TARGETS, SUBJECT)
    assert out["size_m"][2] == pytest.approx(0.8)
    assert out["scale"][0] == pytest.approx(0.8 / 0.3)
    topics = [a["topic"] for a in out["ambiguities"]]
    assert "size_dimension" in topics
    assert out["needs_confirmation"] is True
    # accepting the defaults removes the confirmation requirement
    out2 = R.resolve_placement(
        {"placement": "on", "target": {"object": "Palm", "anchor": "top"},
         "size": {"mode": "relative", "fraction": 0.1},
         "accept_ambiguities": True}, TARGETS, SUBJECT)
    assert out2["needs_confirmation"] is False


def test_relative_size_with_explicit_dimension():
    out = R.resolve_placement(
        {"placement": "on", "target": {"object": "Palm"},
         "size": {"mode": "relative", "fraction": 0.5, "dimension": "width"}},
        TARGETS, SUBJECT)
    assert out["size_m"][0] == pytest.approx(0.4)
    assert out["ambiguities"] == []


def test_left_and_right_world_and_target_frames():
    base = {"placement": "right_of", "target": {"object": "Palm", "anchor": "center"}}
    world = R.resolve_placement({**base, "frame": "world"}, TARGETS, SUBJECT)
    assert world["center"][0] > 0.4
    target = R.resolve_placement({**base, "frame": "target"}, TARGETS, SUBJECT)
    assert target["center"][0] > 0.4
    left = R.resolve_placement({**base, "placement": "left_of", "frame": "world"},
                               TARGETS, SUBJECT)
    assert left["center"][0] < -0.4


def test_between_two_targets():
    targets = {
        "A": {"name": "A", "bbox": [[-3, 0, 0], [-2, 1, 2]], "dimensions": [1, 1, 2]},
        "B": {"name": "B", "bbox": [[2, 0, 0], [3, 1, 2]], "dimensions": [1, 1, 2]},
    }
    out = R.resolve_placement({"placement": "between",
                               "targets": [{"object": "A", "anchor": "center"},
                                           {"object": "B", "anchor": "center"}]},
                              targets, SUBJECT)
    assert out["center"][0] == pytest.approx(0.0)
    assert any("between" in a for a in out["assumptions"])


def test_between_requires_two_targets():
    with pytest.raises(R.RelationError):
        R.resolve_placement({"placement": "between", "target": {"object": "Palm"}},
                            TARGETS, SUBJECT)


def test_on_surface_uses_normal():
    facts = {"Wall": {"name": "Wall", "bbox": [[-2, 0, 0], [2, 0.2, 3]],
                      "dimensions": [4, 0.2, 3], "normal": [0, -1, 0]}}
    out = R.resolve_placement({"placement": "on_surface",
                               "target": {"object": "Wall", "anchor": "center"}},
                              facts, SUBJECT)
    assert out["center"][1] < 0.0


def test_rotation_modes():
    axis_rot = R.resolve_placement(
        {"placement": "on", "target": {"object": "Palm", "part": "trunk"},
         "rotation": {"mode": "align_to_axis"}}, TARGETS, SUBJECT)
    assert axis_rot["rotation_deg"][0] == pytest.approx(0, abs=1e-6)
    copy = R.resolve_placement(
        {"placement": "on", "target": {"object": "Palm"},
         "rotation": {"mode": "copy"}}, TARGETS, SUBJECT)
    assert copy["rotation_deg"] == [0.0, 0.0, 0.0]
    with pytest.raises(R.RelationError):
        R.resolve_placement({"placement": "on", "target": {"object": "Palm"},
                             "rotation": {"mode": "sideways"}}, TARGETS, SUBJECT)


def test_absolute_and_fit_size():
    absolute = R.resolve_placement(
        {"placement": "at", "target": {"object": "Palm"},
         "size": {"mode": "absolute", "size": [0.6, 0.6, 0.6]}}, TARGETS, SUBJECT)
    assert absolute["scale"] == pytest.approx([2.0, 2.0, 2.0])
    fit = R.resolve_placement(
        {"placement": "inside", "target": {"object": "Palm"},
         "size": {"mode": "fit", "axes": "xyz"}}, TARGETS, SUBJECT)
    assert fit["size_m"][0] == pytest.approx(0.8)
    assert fit["size_m"][2] == pytest.approx(8.0)


def test_persistence_plans():
    parent = R.resolve_placement(
        {"placement": "on", "target": {"object": "Palm"},
         "persist": {"mode": "parent"}}, TARGETS, SUBJECT)
    assert parent["persistence"]["kind"] == "parent"
    assert parent["persistence"]["target"] == "Palm"
    surface = R.resolve_placement(
        {"placement": "on", "target": {"object": "Palm"},
         "persist": {"mode": "surface", "offset": 0.01}}, TARGETS, SUBJECT)
    assert surface["persistence"]["shrinkwrap"]["offset"] == 0.01
    with pytest.raises(R.RelationError):
        R.resolve_placement({"placement": "on", "target": {"object": "Palm"},
                             "persist": {"mode": "magic"}}, TARGETS, SUBJECT)


def test_subject_origin_modes():
    base = {"dimensions": [1, 1, 2], "origin": "base"}
    out = R.resolve_placement({"placement": "on", "target": {"object": "Palm", "anchor": "tip"}},
                              TARGETS, base)
    # origin at the base -> origin must sit at the tip, i.e. 1 m below the centre
    assert out["center"][2] - out["position"][2] == pytest.approx(1.0)


def test_unknown_target_and_placement():
    with pytest.raises(R.RelationError):
        R.resolve_placement({"placement": "on", "target": {"object": "Nope"}}, TARGETS, SUBJECT)
    with pytest.raises(R.RelationError):
        R.resolve_placement({"placement": "sideways", "target": {"object": "Palm"}},
                            TARGETS, SUBJECT)
    with pytest.raises(R.RelationError):
        R.resolve_placement({"placement": "on"}, TARGETS, SUBJECT)


def test_part_that_does_not_exist_becomes_ambiguity():
    out = R.resolve_placement({"placement": "on", "target": {"object": "Palm",
                                                             "part": "blossom"}},
                              TARGETS, SUBJECT)
    assert any(a["topic"] == "part" for a in out["ambiguities"])


def test_distribute_along_path_and_line():
    out = R.distribute({"count": 3, "target": {"object": "Palm", "path": "trunk_axis"}}, TARGETS)
    assert len(out["points"]) == 3
    assert out["points"][0][2] == pytest.approx(0.0)
    line = R.distribute({"count": 2, "target": {"object": "Palm"}}, TARGETS)
    assert len(line["points"]) == 2
    with pytest.raises(R.RelationError):
        R.distribute({"count": 0, "target": {"object": "Palm"}}, TARGETS)
    with pytest.raises(R.RelationError):
        R.distribute({"count": 2, "target": {"object": "Palm", "path": "nope"}}, TARGETS)


def test_gap_and_offset():
    out = R.resolve_placement({"placement": "on", "gap": 0.5, "offset": [1, 0, 0],
                               "target": {"object": "Palm", "anchor": "tip"}},
                              TARGETS, SUBJECT)
    assert out["center"][2] == pytest.approx(7.9 + 0.15 + 0.5)
    assert out["position"][0] == pytest.approx(0.05 + 1.0)


def test_sink_pushes_into_surface():
    out = R.resolve_placement({"placement": "on", "sink": 0.5,
                               "target": {"object": "Palm", "anchor": "tip"}},
                              TARGETS, SUBJECT)
    # sink is a fraction of the subject height (0.3 m), so 0.5 -> 0.15 m
    assert out["center"][2] == pytest.approx(7.9 + 0.15 - 0.5 * 0.3, rel=1e-6)

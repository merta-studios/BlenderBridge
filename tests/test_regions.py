import math

import pytest

from blenderbridge import regions as R


CUBE = [(x, y, z) for x in (0.0, 1.0) for y in (0.0, 1.0) for z in (0.0, 1.0)]


def test_region_from_box_and_falloff():
    # CUBE has corners at x/y/z in {0, 1}; this box keeps only x=0,y=0 -> 2 points
    idx, w = R.region_from_box(CUBE, (0, 0, 0), (0.5, 0.5, 1.5))
    assert len(idx) == 2
    assert all(x == 1.0 for x in w)
    base_idx, _ = R.region_from_box(CUBE, (0, 0, 0), (0.0, 0.0, 1.5))
    idx2, w2 = R.region_from_box(CUBE, (0, 0, 0), (0.0, 0.0, 1.5), soft=1.5)
    assert len(idx2) > len(base_idx)          # the soft band adds neighbours
    assert max(w2) == 1.0 and min(w2) < 1.0


def test_region_from_sphere_and_axis_band():
    idx, _ = R.region_from_sphere(CUBE, (0.5, 0.5, 0.5), 0.9)
    assert len(idx) == 8
    idx2, _ = R.region_from_axis_band(CUBE, (0.5, 0.5, -1), (0.5, 0.5, 2), 0.8)
    assert len(idx2) == 8
    # t_range ends where the segment passes z = 0.2, so only the z = 0 layer remains
    idx3, _ = R.region_from_axis_band(CUBE, (0.5, 0.5, -1), (0.5, 0.5, 2), 0.8,
                                      t_range=(0.0, 0.4))
    assert len(idx3) == 4


def test_region_by_height_slices():
    idx, _ = R.region_by_height(CUBE, (0, 0, 1), (0.5, 1.0))
    assert len(idx) == 4
    assert all(CUBE[i][2] == 1.0 for i in idx)


def test_region_from_indices_grows_with_soft():
    idx, w = R.region_from_indices(CUBE, [0], soft=1.5)
    assert len(idx) > 1
    assert w[idx.index(0)] == 1.0


def test_region_validation():
    with pytest.raises(ValueError):
        R.Region("x", "obj", [0, 1], [1.0])
    with pytest.raises(ValueError):
        R.Region("x", "obj", [0], [1.5])
    region = R.Region("x", "obj", [0, 1])
    assert region.weights == [1.0, 1.0]
    assert region.size == 2


def test_displace_scales_with_weight():
    weights = [1.0, 0.5, 0.0]
    out = R.displace(CUBE[:3], weights, (0, 0, 1), 2.0)
    assert out.positions[0][2] == pytest.approx(2.0)
    assert out.positions[1][2] == pytest.approx(1.0 + 1.0)
    assert out.positions[2][2] == pytest.approx(CUBE[2][2])
    assert out.moved == 2


def test_inflate_moves_along_normals():
    normals = [(1, 0, 0)] * 3
    out = R.inflate([(0, 0, 0), (1, 0, 0), (2, 0, 0)], [1, 1, 0], normals, 0.5)
    assert out.positions[0][0] == pytest.approx(0.5)
    assert out.positions[2][0] == pytest.approx(2.0)


def test_scale_about_center():
    out = R.scale_about([(1, 0, 0), (-1, 0, 0)], [1, 1], (0, 0, 0), (2, 2, 2))
    assert out.positions[0][0] == pytest.approx(2.0)


def test_taper_narrows_upper_end():
    pts = [(1, 0, 0), (1, 0, 2)]
    out = R.taper(pts, [1, 1], (0, 0, 1), (0, 0, 0), 1.0, 0.5)
    assert out.positions[0][0] == pytest.approx(1.0)
    assert out.positions[1][0] == pytest.approx(0.5)


def test_bend_keeps_base_and_moves_top():
    pts = [(0, 0, 0), (0, 0, 1), (0, 0, 2), (0, 0, 3)]
    out = R.bend(pts, [1, 1, 1, 1], (0, 0, 1), (0, 0, 0), 45, (1, 0, 0))
    assert out.positions[0] == pytest.approx((0, 0, 0))
    assert out.positions[3][0] > 0.3
    # the arc keeps the length roughly intact instead of shrinking like a rigid rotation
    assert out.positions[3][2] > 2.4


def test_bend_degenerate_axis_is_reported():
    out = R.bend([(0, 0, 0)], [1], (0, 0, 0), (0, 0, 0), 30, (1, 0, 0))
    assert out.moved == 0
    assert out.warnings


def test_smooth_reduces_spikes_but_pins_zero_weights():
    pts = [(0, 0, 0), (1, 0, 0), (2, 0, 5), (3, 0, 0)]
    edges = [[0, 1], [1, 2], [2, 3]]
    out = R.smooth(pts, [0, 1, 1, 0], edges, factor=1.0, iterations=1)
    assert out.positions[2][2] < 5.0
    assert out.positions[0] == pytest.approx(pts[0])
    assert out.positions[3] == pytest.approx(pts[3])


def test_fingerprint_is_stable_under_moves_but_changes_with_topology():
    fp = R.fingerprint(8, 6, [(0, 1), (1, 2)])
    assert fp == R.fingerprint(8, 6, [(0, 1), (1, 2)])
    assert fp != R.fingerprint(9, 6, [(0, 1), (1, 2)])
    assert fp != R.fingerprint(8, 6, [(0, 1), (1, 3)])
    region = R.Region("a", "obj", [0, 1], fingerprint=fp)
    assert R.region_state(region, 8, 6, [(0, 1), (1, 2)]) == "ok"
    assert R.region_state(region, 12, 6, []) == "stale"
    assert R.region_state(region, 1, 6, [(0, 1), (1, 2)]) == "out_of_range"


def test_combine_weights_and_protected():
    assert R.combine_weights([1, 0.5], [0.5, 0.5], "multiply") == [0.5, 0.25]
    assert R.combine_weights([1, 0.5], [0.5, 0.5], "max") == [1, 0.5]
    assert R.relax_protected([1, 1, 1], [1]) == [1, 0, 1]


def test_serialisation_roundtrip():
    region = R.Region("cheek", "Head", [1, 2, 3], [1.0, 0.5, 0.2],
                      kind="region", protected=True, note="links")
    data = R.region_to_dict(region)
    back = R.region_from_dict(data)
    assert back.name == "cheek"
    assert back.vertices == [1, 2, 3]
    assert back.weights == [1.0, 0.5, 0.2]
    assert back.protected is True
    with pytest.raises(ValueError):
        R.region_from_dict({"no_name": 1})


def test_summary_counts_weights():
    summary = R.summarise_weights([1.0, 0.5, 0.0])
    assert summary["full"] == 1
    assert summary["falloff"] == 1
    assert summary["vertices"] == 3

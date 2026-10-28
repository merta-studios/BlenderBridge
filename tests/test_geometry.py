import math

import pytest

from blenderbridge import geometry as G


def test_vector_basics():
    assert G.vadd((1, 2, 3), (1, 1, 1)) == (2.0, 3.0, 4.0)
    assert G.vdot((1, 0, 0), (0, 1, 0)) == 0.0
    assert G.vcross((1, 0, 0), (0, 1, 0)) == (0.0, 0.0, 1.0)
    assert G.vlen((3, 4, 0)) == 5.0
    assert G.vnorm((0, 0, 0)) == (0.0, 0.0, 0.0)
    assert G.vround((1.23456789, 0, 0), 3) == (1.235, 0.0, 0.0)


def test_aabb_helpers():
    box = G.aabb([(0, 0, 0), (2, 4, 6), (-1, 1, 1)])
    assert box == ((-1.0, 0.0, 0.0), (2.0, 4.0, 6.0))
    assert G.aabb_center(box) == (0.5, 2.0, 3.0)
    assert G.aabb_dims(box) == (3.0, 4.0, 6.0)
    assert G.aabb_volume(box) == 72.0
    assert G.aabb_anchor(box, "bottom") == (0.5, 2.0, 0.0)
    assert G.aabb_anchor(box, "top_left") == (-1.0, 2.0, 6.0)
    assert len(G.aabb_corners(box)) == 8
    with pytest.raises(ValueError):
        G.aabb_anchor(box, "sideways")


def test_euler_matrix_matches_known_rotation():
    m = G.euler_matrix(0, 0, 90)
    x, y, z = G.mat_apply(m, (1, 0, 0))
    assert round(x, 6) == 0
    assert round(y, 6) == 1
    assert round(z, 6) == 0
    # round trip through euler extraction
    rx = G.mat_to_euler_deg(m)
    assert abs(rx[2] - 90) < 1e-6


def test_compose_and_align():
    m = G.compose_matrix((1, 2, 3), (0, 0, 0), (2, 2, 2))
    assert G.mat_apply(m, (1, 0, 0)) == (3.0, 2.0, 3.0)
    aligned = G.mat_align_z_to((0, 0, 1), (0, 0, 0))
    ax, ay, az = (G.mat_apply(aligned, v) for v in ((1, 0, 0), (0, 1, 0), (0, 0, 1)))
    assert az == pytest.approx((0, 0, 1))
    assert abs(G.vlen(ax) - 1) < 1e-9


def test_falloff_and_taper():
    assert G.falloff(0, 0.5) == 1.0
    assert G.falloff(0.5, 0.5) == 0.0
    assert 0 < G.falloff(0.25, 0.5, "smooth") < 1
    assert G.taper_scale(0, 1.0, 0.2) == 1.0
    assert G.taper_scale(1, 1.0, 0.2) == pytest.approx(0.2)
    assert G.taper_scale(0.5, 1.0, 0.2, "smooth") == pytest.approx(0.6)


def test_polyline_and_distribution():
    pts = [(0, 0, 0), (0, 0, 2), (0, 0, 4)]
    assert G.polyline_point_at(pts, 0.5) == pytest.approx((0, 0, 2))
    assert G.polyline_point_at(pts, 1.0) == pytest.approx((0, 0, 4))
    assert G.polyline_tangent(pts, 0.5) == pytest.approx((0, 0, 1))
    dist = G.distribute_along((0, 0, 0), (0, 0, 1), 3)
    assert dist == [(0.0, 0.0, 0.0), (0.0, 0.0, 0.5), (0.0, 0.0, 1.0)]


def test_principal_axis_finds_vertical_rod():
    pts = [(0.01 * (i % 3), 0.01 * (i % 2), i * 0.1) for i in range(50)]
    axis, spread = G.principal_axis(pts)
    assert abs(abs(axis[2]) - 1.0) < 0.05
    assert 0.5 < spread <= 1.0


def test_project_and_closest_point():
    assert G.project_to_plane((0, 0, 5), (0, 0, 0), (0, 0, 1)) == pytest.approx((0, 0, 0))
    t, p = G.closest_point_on_segment((1, 1, 0), (0, 0, 0), (2, 0, 0))
    assert t == pytest.approx(0.5)
    assert p == pytest.approx((1, 0, 0))
    assert G.distance_to_segment((0, 1, 0), (0, 0, 0), (0, 0, 5)) == pytest.approx(1.0)


def test_units():
    assert G.meters_to_studs(0.28) == pytest.approx(1.0)
    assert G.studs_to_meters(10) == pytest.approx(2.8)
    assert G.dims_to_studs((0.28, 0.56, 0.84)) == pytest.approx((1.0, 2.0, 3.0))

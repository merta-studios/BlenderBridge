# AUTO-GENERATED - do not edit.
# Source: blenderbridge/geometry.py
# Edit the source file and run: python tools/sync_shared.py
# (comments, so ``from __future__ import annotations`` stays the first statement)

"""Small, dependency free 3D math used on both sides of the bridge.

Blender ships ``mathutils``; the connector does not.  Everything here works on
plain tuples/lists so identical code can run in the add-on, in the connector and
in the test suite.  Matrices are row-major 4x4 nested lists, which converts to a
``mathutils.Matrix`` with ``Matrix(rows)``.

Units: Blender works in metres.  Roblox Studio works in studs, where one stud is
0.28 m (the classic Roblox scale).  Conversion helpers live at the bottom.
"""

from __future__ import annotations

import math
from typing import Iterable, List, Sequence, Tuple

Vec3 = Tuple[float, float, float]
Mat4 = List[List[float]]

EPSILON = 1e-9
STUD_IN_METERS = 0.28


# ---------------------------------------------------------------------------
# Vector helpers
# ---------------------------------------------------------------------------


def vec3(v: Sequence[float]) -> Vec3:
    if len(v) != 3:
        raise ValueError("expected a 3 component vector")
    return (float(v[0]), float(v[1]), float(v[2]))


def vadd(a: Sequence[float], b: Sequence[float]) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def vsub(a: Sequence[float], b: Sequence[float]) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def vmul(a: Sequence[float], s: float) -> Vec3:
    return (a[0] * s, a[1] * s, a[2] * s)


def vhadamard(a: Sequence[float], b: Sequence[float]) -> Vec3:
    return (a[0] * b[0], a[1] * b[1], a[2] * b[2])


def vdot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def vcross(a: Sequence[float], b: Sequence[float]) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def vlen(a: Sequence[float]) -> float:
    return math.sqrt(vdot(a, a))


def vnorm(a: Sequence[float]) -> Vec3:
    n = vlen(a)
    if n < EPSILON:
        return (0.0, 0.0, 0.0)
    return (a[0] / n, a[1] / n, a[2] / n)


def vlerp(a: Sequence[float], b: Sequence[float], t: float) -> Vec3:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def vneg(a: Sequence[float]) -> Vec3:
    return (-a[0], -a[1], -a[2])


def vround(a: Sequence[float], digits: int = 6) -> Vec3:
    return (round(a[0], digits), round(a[1], digits), round(a[2], digits))


def vrotate(v: Sequence[float], axis: Sequence[float], angle_rad: float) -> Vec3:
    """Rotate ``v`` around ``axis`` using Rodrigues' formula."""
    k = vnorm(axis)
    if vlen(k) < EPSILON or abs(angle_rad) < EPSILON:
        return vec3(v)
    c = math.cos(angle_rad)
    s = math.sin(angle_rad)
    return tuple(  # type: ignore[return-value]
        v[i] * c + vcross(k, v)[i] * s + k[i] * vdot(k, v) * (1 - c) for i in range(3)
    )


def axis_perpendicular(axis: Sequence[float]) -> Vec3:
    """Return an arbitrary unit vector perpendicular to ``axis``."""
    a = vnorm(axis)
    ref = (0.0, 0.0, 1.0) if abs(a[2]) < 0.9 else (1.0, 0.0, 0.0)
    return vnorm(vcross(a, ref))


# ---------------------------------------------------------------------------
# Matrices
# ---------------------------------------------------------------------------


def identity() -> Mat4:
    return [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


def euler_matrix(rot_x_deg: float, rot_y_deg: float, rot_z_deg: float, order: str = "XYZ") -> Mat4:
    """Rotation matrix for intrinsic Euler angles in degrees (Blender's default order)."""
    rx, ry, rz = (math.radians(a) for a in (rot_x_deg, rot_y_deg, rot_z_deg))
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    mx = [[1, 0, 0], [0, cx, -sx], [0, sx, cx]]
    my = [[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]
    mz = [[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]]
    mats = {"X": mx, "Y": my, "Z": mz}
    rot = [[1.0 if i == j else 0.0 for j in range(3)] for i in range(3)]
    for axis_name in order.upper():
        if axis_name not in mats:
            raise ValueError(f"invalid euler order component: {axis_name!r}")
        rot = _mat3_mul(mats[axis_name], rot)
    return [
        [rot[0][0], rot[0][1], rot[0][2], 0.0],
        [rot[1][0], rot[1][1], rot[1][2], 0.0],
        [rot[2][0], rot[2][1], rot[2][2], 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _mat3_mul(a: Sequence[Sequence[float]], b: Sequence[Sequence[float]]) -> List[List[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def compose_matrix(loc: Sequence[float], rot_deg: Sequence[float], scale: Sequence[float]) -> Mat4:
    """Compose a Blender style TRS matrix (rot in degrees, XYZ order)."""
    r = euler_matrix(rot_deg[0], rot_deg[1], rot_deg[2])
    s = scale
    for i in range(3):
        for j in range(3):
            r[i][j] *= s[j]
    r[0][3], r[1][3], r[2][3] = float(loc[0]), float(loc[1]), float(loc[2])
    return r


def mat_apply(m: Sequence[Sequence[float]], v: Sequence[float]) -> Vec3:
    x, y, z = float(v[0]), float(v[1]), float(v[2])
    return (
        m[0][0] * x + m[0][1] * y + m[0][2] * z + m[0][3],
        m[1][0] * x + m[1][1] * y + m[1][2] * z + m[1][3],
        m[2][0] * x + m[2][1] * y + m[2][2] * z + m[2][3],
    )


def mat_from_axes(x_axis: Sequence[float], y_axis: Sequence[float], z_axis: Sequence[float],
                  loc: Sequence[float]) -> Mat4:
    """Build a rotation matrix from three (already orthonormal) axes."""
    x, y, z = vnorm(x_axis), vnorm(y_axis), vnorm(z_axis)
    return [
        [x[0], y[0], z[0], float(loc[0])],
        [x[1], y[1], z[1], float(loc[1])],
        [x[2], y[2], z[2], float(loc[2])],
        [0.0, 0.0, 0.0, 1.0],
    ]


def mat_align_z_to(direction: Sequence[float], loc: Sequence[float],
                   up_hint: Sequence[float] = (0.0, 0.0, 1.0)) -> Mat4:
    """Rotation whose local +Z points along ``direction``.

    Used for "along the trunk" / "perpendicular to the surface" style relations.
    """
    z = vnorm(direction)
    if vlen(z) < EPSILON:
        z = (0.0, 0.0, 1.0)
    hint = vnorm(up_hint)
    if abs(vdot(z, hint)) > 0.999:
        hint = (1.0, 0.0, 0.0)
    x = vnorm(vcross(hint, z))
    y = vnorm(vcross(z, x))
    return mat_from_axes(x, y, z, loc)


def mat_look_along(forward: Sequence[float], loc: Sequence[float]) -> Mat4:
    """Rotation whose local -Y points along ``forward`` (Blender camera/lamp style)."""
    f = vnorm(forward)
    if vlen(f) < EPSILON:
        f = (0.0, 1.0, 0.0)
    up = (0.0, 0.0, 1.0)
    if abs(vdot(f, up)) > 0.999:
        up = (0.0, 1.0, 0.0)
    x = vnorm(vcross(up, vneg(f)))
    y = vneg(f)
    z = vnorm(vcross(x, y))
    return mat_from_axes(x, y, z, loc)


def mat_to_euler_deg(m: Sequence[Sequence[float]]) -> Vec3:
    """Extract XYZ Euler angles (degrees) from a rotation matrix."""
    sy = -m[2][0]
    sy = max(-1.0, min(1.0, sy))
    ry = math.asin(sy)
    if abs(math.cos(ry)) > 1e-6:
        rx = math.atan2(m[2][1], m[2][2])
        rz = math.atan2(m[1][0], m[0][0])
    else:  # gimbal lock
        rx = math.atan2(-m[1][2], m[1][1])
        rz = 0.0
    return (math.degrees(rx), math.degrees(ry), math.degrees(rz))


# ---------------------------------------------------------------------------
# Bounding boxes
# ---------------------------------------------------------------------------


def aabb(points: Iterable[Sequence[float]]) -> Tuple[Vec3, Vec3]:
    pts = list(points)
    if not pts:
        raise ValueError("aabb() needs at least one point")
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    for p in pts:
        for i in range(3):
            if p[i] < lo[i]:
                lo[i] = float(p[i])
            if p[i] > hi[i]:
                hi[i] = float(p[i])
    return (lo[0], lo[1], lo[2]), (hi[0], hi[1], hi[2])


def aabb_center(box: Tuple[Sequence[float], Sequence[float]]) -> Vec3:
    lo, hi = box
    return ((lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0, (lo[2] + hi[2]) / 2.0)


def aabb_dims(box: Tuple[Sequence[float], Sequence[float]]) -> Vec3:
    lo, hi = box
    return (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])


def aabb_corners(box: Tuple[Sequence[float], Sequence[float]]) -> List[Vec3]:
    lo, hi = box
    return [
        (x, y, z)
        for x in (lo[0], hi[0])
        for y in (lo[1], hi[1])
        for z in (lo[2], hi[2])
    ]


def aabb_union(boxes: Iterable[Tuple[Sequence[float], Sequence[float]]]) -> Tuple[Vec3, Vec3]:
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    found = False
    for box in boxes:
        found = True
        for i in range(3):
            lo[i] = min(lo[i], box[0][i])
            hi[i] = max(hi[i], box[1][i])
    if not found:
        raise ValueError("aabb_union() needs at least one box")
    return (lo[0], lo[1], lo[2]), (hi[0], hi[1], hi[2])


def aabb_volume(box: Tuple[Sequence[float], Sequence[float]]) -> float:
    d = aabb_dims(box)
    return max(0.0, d[0]) * max(0.0, d[1]) * max(0.0, d[2])


def aabb_anchor(box: Tuple[Sequence[float], Sequence[float]], anchor: str) -> Vec3:
    """Named anchor point on an axis aligned box.

    ``anchor`` is one of: center, bottom, top, left, right, front, back and the
    8 corner combinations such as ``top_left_front``.
    """
    lo, hi = box
    c = aabb_center(box)
    parts = anchor.lower().split("_") if anchor else []
    x, y, z = c
    for part in parts:
        if part in ("center", "centre", "middle"):
            continue
        if part in ("left", "right"):
            x = lo[0] if part == "left" else hi[0]
        elif part in ("front", "back"):
            y = lo[1] if part == "front" else hi[1]
        elif part in ("bottom", "base", "low"):
            z = lo[2]
        elif part in ("top", "tip", "high"):
            z = hi[2]
        else:
            raise ValueError(f"unknown anchor component: {part!r}")
    return (x, y, z)


# ---------------------------------------------------------------------------
# Falloff / deformation curves
# ---------------------------------------------------------------------------


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if value < lo else hi if value > hi else value


def falloff(distance: float, radius: float, profile: str = "smooth") -> float:
    """Weight in [0, 1] for a vertex ``distance`` away from the region core.

    ``radius`` is the width of the soft transition zone.  A distance of 0 gives
    1.0, ``>= radius`` gives 0.0.
    """
    if radius <= EPSILON:
        return 1.0 if distance <= EPSILON else 0.0
    t = clamp(1.0 - abs(distance) / radius)
    if profile == "linear":
        return t
    if profile == "smooth":
        return t * t * (3.0 - 2.0 * t)
    if profile == "sharp":
        return t ** 3
    if profile == "gaussian":
        return math.exp(-4.0 * (1.0 - t) ** 2) if t > 0 else 0.0
    raise ValueError(f"unknown falloff profile: {profile!r}")


def taper_scale(t: float, start: float, end: float, curve: str = "linear") -> float:
    """Scale factor along a normalised axis parameter ``t`` in [0, 1]."""
    t = clamp(t)
    if curve == "linear":
        k = t
    elif curve == "ease_in":
        k = t * t
    elif curve == "ease_out":
        k = 1.0 - (1.0 - t) ** 2
    elif curve == "smooth":
        k = t * t * (3.0 - 2.0 * t)
    else:
        raise ValueError(f"unknown taper curve: {curve!r}")
    return start + (end - start) * k


def bend_offset(height_fraction: float, total_height: float, angle_deg: float,
                radius_fraction: float = 1.0) -> Tuple[Vec3, float]:
    """Approximate bend of a vertical column.

    Returns ``(offset, rotation_rad)`` for a vertex at ``height_fraction`` of the
    column, where the whole column is bent by ``angle_deg`` towards +X.  A simple
    arc approximation keeps the result predictable and cheap for big meshes.
    """
    f = clamp(height_fraction * radius_fraction)
    angle = math.radians(angle_deg) * f * f
    if abs(angle) < EPSILON:
        return (0.0, 0.0, 0.0), 0.0
    r = total_height * max(0.15, 1.0 / max(abs(math.radians(angle_deg)), 1e-6))
    x = r * (1.0 - math.cos(angle))
    z = -r * math.sin(angle) * 0.0
    return (x, 0.0, z), angle


# ---------------------------------------------------------------------------
# Lines, planes, distribution
# ---------------------------------------------------------------------------


def closest_point_on_segment(p: Sequence[float], a: Sequence[float], b: Sequence[float]) -> Tuple[float, Vec3]:
    """Return ``(t, point)`` of the point on segment a->b closest to p."""
    ab = vsub(b, a)
    denom = vdot(ab, ab)
    if denom < EPSILON:
        return 0.0, vec3(a)
    t = clamp(vdot(vsub(p, a), ab) / denom)
    return t, vadd(a, vmul(ab, t))


def distance_to_segment(p: Sequence[float], a: Sequence[float], b: Sequence[float]) -> float:
    _, cp = closest_point_on_segment(p, a, b)
    return vlen(vsub(p, cp))


def polyline_point_at(points: Sequence[Sequence[float]], t: float) -> Vec3:
    """Point at normalised arc length ``t`` along a polyline."""
    if not points:
        raise ValueError("polyline_point_at() needs points")
    if len(points) == 1:
        return vec3(points[0])
    t = clamp(t)
    lengths = [vlen(vsub(points[i + 1], points[i])) for i in range(len(points) - 1)]
    total = sum(lengths)
    if total < EPSILON:
        return vec3(points[0])
    target = t * total
    acc = 0.0
    for i, seg in enumerate(lengths):
        if acc + seg >= target or i == len(lengths) - 1:
            local = 0.0 if seg < EPSILON else (target - acc) / seg
            return vlerp(points[i], points[i + 1], local)
        acc += seg
    return vec3(points[-1])


def polyline_tangent(points: Sequence[Sequence[float]], t: float) -> Vec3:
    """Direction of the polyline at normalised position ``t``."""
    if len(points) < 2:
        return (0.0, 0.0, 1.0)
    t = clamp(t)
    n = len(points) - 1
    idx = min(n - 1, int(t * n))
    return vnorm(vsub(points[idx + 1], points[idx]))


def project_to_plane(p: Sequence[float], plane_point: Sequence[float],
                     plane_normal: Sequence[float]) -> Vec3:
    n = vnorm(plane_normal)
    d = vdot(vsub(p, plane_point), n)
    return vsub(p, vmul(n, d))


def distribute_along(p0: Sequence[float], p1: Sequence[float], count: int,
                     include_ends: bool = True) -> List[Vec3]:
    """Evenly spaced positions on the segment p0->p1."""
    if count <= 0:
        return []
    if count == 1:
        return [vlerp(p0, p1, 0.5)]
    if include_ends:
        return [vlerp(p0, p1, i / (count - 1)) for i in range(count)]
    return [vlerp(p0, p1, (i + 1) / (count + 1)) for i in range(count)]


def principal_axis(points: Sequence[Sequence[float]]) -> Tuple[Vec3, float]:
    """Dominant direction of a point cloud via power iteration on covariance.

    Returns ``(axis, spread)``; ``spread`` is the normalised variance along the
    axis (0 = flat line of points with no thickness, 1 = fully isotropic).
    Good enough to answer "which way does this trunk run?".
    """
    pts = [vec3(p) for p in points]
    if len(pts) < 3:
        if len(pts) == 2:
            return vnorm(vsub(pts[1], pts[0])), 0.0
        return (0.0, 0.0, 1.0), 0.0
    center = vmul(_vsum(pts), 1.0 / len(pts))
    cov = [[0.0] * 3 for _ in range(3)]
    for p in pts:
        d = vsub(p, center)
        for i in range(3):
            for j in range(3):
                cov[i][j] += d[i] * d[j]
    for i in range(3):
        for j in range(3):
            cov[i][j] /= len(pts)
    axis = (0.0, 0.0, 1.0)
    for _ in range(32):
        nx = vnorm(tuple(sum(cov[i][j] * axis[j] for j in range(3)) for i in range(3)))
        if vlen(nx) < EPSILON or vlen(vsub(nx, axis)) < 1e-9:
            axis = nx if vlen(nx) > EPSILON else axis
            break
        axis = nx
    var_axis = sum(
        vdot(vsub(p, center), axis) ** 2 for p in pts
    ) / len(pts)
    trace = cov[0][0] + cov[1][1] + cov[2][2]
    spread = 0.0 if trace < EPSILON else clamp(var_axis / trace)
    return axis, spread


def _vsum(points: Iterable[Sequence[float]]) -> Vec3:
    x = y = z = 0.0
    for p in points:
        x += p[0]
        y += p[1]
        z += p[2]
    return (x, y, z)


def centroid(points: Sequence[Sequence[float]]) -> Vec3:
    if not points:
        raise ValueError("centroid() needs at least one point")
    return vmul(_vsum(points), 1.0 / len(points))


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def meters_to_studs(value: float) -> float:
    return value / STUD_IN_METERS


def studs_to_meters(value: float) -> float:
    return value * STUD_IN_METERS


def dims_to_studs(dims: Sequence[float]) -> Vec3:
    return tuple(meters_to_studs(d) for d in dims)  # type: ignore[return-value]

"""Named regions inside a single mesh, and pure geometry operations on them.

Why this module is separate
---------------------------
The interesting geometry logic is *not* allowed to depend on Blender.  Here we
work on plain lists of vertex positions, edges and normals, which means the
whole deformer toolkit can be unit-tested in CI without launching Blender.
The Blender side (``blenderbridge_addon/ops/mesh_ops.py``) only has to shuttle
data between ``bmesh`` and these functions.

A region is a named subset of the vertices of one mesh with a per-vertex
influence weight in [0, 1]:

* weight 1.0  -> the vertex belongs to the region (full effect)
* 0.5        -> soft transition zone (half effect)
* protected  -> weight is never changed by deformers, ever

Regions can be created from a manual selection in Blender, a box/sphere probe,
a material slot, a loose part, or by hand-editing the vertex group.  They are
persisted as a JSON document on the object plus one Blender vertex group per
region, so the user keeps working with normal Blender tools.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import geometry as G

REGION_PREFIX = "bb_rg_"
REGION_PROP = "bb_regions"
ANCHOR_PROP = "bb_anchors"
PATH_PROP = "bb_paths"
REGION_FORMAT = 1

MAX_REGION_VERTS = 500_000


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Region:
    name: str
    object_name: str
    vertices: List[int] = field(default_factory=list)
    weights: List[float] = field(default_factory=list)
    kind: str = "region"          # region | part | protected
    protected: bool = False
    source: str = "manual"
    note: str = ""
    created_at: str = ""
    fingerprint: str = ""
    vertex_count: int = 0
    poly_count: int = 0
    bounds: Optional[List[List[float]]] = None
    stale: Optional[bool] = None

    def __post_init__(self) -> None:
        if len(self.weights) == 0:
            self.weights = [1.0] * len(self.vertices)
        if len(self.weights) != len(self.vertices):
            raise ValueError("region weights must match vertex count")
        for w in self.weights:
            if not (0.0 <= w <= 1.0):
                raise ValueError("region weights must be within [0, 1]")

    @property
    def size(self) -> int:
        return len(self.vertices)


@dataclass
class Anchor:
    """A named point (optionally a direction) on an object."""

    name: str
    object_name: str
    position: G.Vec3
    direction: Optional[G.Vec3] = None
    source: str = "manual"
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "object": self.object_name,
            "position": list(self.position),
            "source": self.source,
        }
        if self.direction:
            out["direction"] = list(self.direction)
        if self.note:
            out["note"] = self.note
        return out


@dataclass
class DeformResult:
    positions: List[G.Vec3]
    moved: int
    max_displacement: float
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Fingerprints: detect that stored regions no longer match the mesh
# ---------------------------------------------------------------------------


def fingerprint(vertex_count: int, poly_count: int,
                edges: Optional[Sequence[Sequence[int]]] = None,
                sample: int = 512) -> str:
    """Topology fingerprint used to detect *stale* regions.

    It deliberately does **not** hash vertex coordinates: moving vertices is a
    normal, supported operation that keeps every index valid, so a region must
    survive it.  What genuinely invalidates the indices is a topology change -
    subdivide, extrude, join, separate, merge, delete - which changes the counts
    and/or the edge list.  Those are what this hash catches.
    """
    h = hashlib.sha256()
    h.update(f"v{int(vertex_count)};p{int(poly_count)};".encode("ascii"))
    if edges:
        step = max(1, len(edges) // sample)
        for i in range(0, len(edges), step):
            e = edges[i]
            h.update(f"{int(e[0])}-{int(e[1])};".encode("ascii"))
    return h.hexdigest()[:32]


def region_bounds(positions: Sequence[Sequence[float]], indices: Sequence[int]) -> Optional[List[List[float]]]:
    if not indices:
        return None
    lo, hi = G.aabb(positions[i] for i in indices if 0 <= i < len(positions))
    return [list(lo), list(hi)]


def region_state(region: Region, vertex_count: int, poly_count: int,
                 edges: Optional[Sequence[Sequence[int]]] = None) -> str:
    """Return ``ok``, ``stale`` or ``out_of_range`` for a stored region."""
    if any(i >= vertex_count or i < 0 for i in region.vertices):
        return "out_of_range"
    if region.fingerprint and fingerprint(vertex_count, poly_count, edges) != region.fingerprint:
        return "stale"
    return "ok"


# ---------------------------------------------------------------------------
# Region construction from probes
# ---------------------------------------------------------------------------


def region_from_box(positions: Sequence[Sequence[float]], lo: Sequence[float], hi: Sequence[float],
                    soft: float = 0.0, profile: str = "smooth") -> Tuple[List[int], List[float]]:
    """Vertices inside an axis aligned box; ``soft`` widens a falloff band."""
    box_lo, box_hi = G.aabb([lo, hi])
    idx: List[int] = []
    weights: List[float] = []
    for i, p in enumerate(positions):
        inside = all(box_lo[k] <= p[k] <= box_hi[k] for k in range(3))
        if inside:
            idx.append(i)
            weights.append(1.0)
        elif soft > 0.0:
            # distance to the box surface, negative inside
            d = math.sqrt(sum(
                (box_lo[k] - p[k]) ** 2 if p[k] < box_lo[k] else
                (p[k] - box_hi[k]) ** 2 if p[k] > box_hi[k] else 0.0
                for k in range(3)
            ))
            if d < soft:
                idx.append(i)
                weights.append(G.falloff(d, soft, profile))
    return idx, weights


def region_from_sphere(positions: Sequence[Sequence[float]], center: Sequence[float], radius: float,
                       soft: float = 0.0, profile: str = "smooth") -> Tuple[List[int], List[float]]:
    idx: List[int] = []
    weights: List[float] = []
    for i, p in enumerate(positions):
        d = G.vlen(G.vsub(p, center))
        if d <= radius:
            idx.append(i)
            weights.append(1.0)
        elif soft > 0.0 and d < radius + soft:
            idx.append(i)
            weights.append(G.falloff(d - radius, soft, profile))
    return idx, weights


def region_from_axis_band(positions: Sequence[Sequence[float]], axis_start: Sequence[float],
                          axis_end: Sequence[float], radius: float,
                          t_range: Tuple[float, float] = (0.0, 1.0),
                          soft: float = 0.0, profile: str = "smooth") -> Tuple[List[int], List[float]]:
    """Vertices within ``radius`` of a segment - the 'grab a limb' probe."""
    idx: List[int] = []
    weights: List[float] = []
    lo_t, hi_t = t_range
    for i, p in enumerate(positions):
        t, cp = G.closest_point_on_segment(p, axis_start, axis_end)
        if not (lo_t <= t <= hi_t):
            continue
        d = G.vlen(G.vsub(p, cp))
        if d <= radius:
            idx.append(i)
            weights.append(1.0)
        elif soft > 0.0 and d < radius + soft:
            idx.append(i)
            weights.append(G.falloff(d - radius, soft, profile))
    return idx, weights


def region_from_indices(positions: Sequence[Sequence[float]], indices: Iterable[int],
                        soft: float = 0.0, profile: str = "smooth") -> Tuple[List[int], List[float]]:
    """Grow a manual selection by ``soft`` metres for a smooth falloff band."""
    seeds = sorted({int(i) for i in indices if 0 <= int(i) < len(positions)})
    if not seeds or soft <= 0.0:
        return seeds, [1.0] * len(seeds)
    seed_points = [positions[i] for i in seeds]
    seed_set = set(seeds)
    weights = {i: 1.0 for i in seeds}
    for i, p in enumerate(positions):
        if i in seed_set:
            continue
        d = min(G.vlen(G.vsub(p, sp)) for sp in seed_points)
        if d < soft:
            weights[i] = G.falloff(d, soft, profile)
    idx = sorted(weights)
    return idx, [weights[i] for i in idx]


def region_by_height(positions: Sequence[Sequence[float]], axis: Sequence[float],
                     t_range: Tuple[float, float], soft: float = 0.0,
                     profile: str = "smooth") -> Tuple[List[int], List[float]]:
    """Slice a mesh by normalised position along an axis (e.g. the top third)."""
    a = G.vnorm(axis)
    if G.vlen(a) < G.EPSILON:
        return [], []
    proj = [G.vdot(p, a) for p in positions]
    lo, hi = min(proj), max(proj)
    span = hi - lo
    if span < G.EPSILON:
        return [], []
    idx: List[int] = []
    weights: List[float] = []
    for i, v in enumerate(proj):
        t = (v - lo) / span
        if t_range[0] <= t <= t_range[1]:
            idx.append(i)
            weights.append(1.0)
        elif soft > 0.0:
            # distance in parameter space -> metres, for a consistent look
            d = min(abs(t - t_range[0]), abs(t - t_range[1]))
            if d < soft:
                idx.append(i)
                weights.append(G.falloff(d, soft, profile))
    return idx, weights


# ---------------------------------------------------------------------------
# Deformers
# ---------------------------------------------------------------------------


def _apply(positions: Sequence[Sequence[float]], weights: Sequence[float],
           fn) -> DeformResult:
    out: List[G.Vec3] = []
    moved = 0
    max_d = 0.0
    for i, p in enumerate(positions):
        w = weights[i] if i < len(weights) else 0.0
        if w <= 0.0:
            out.append(G.vec3(p))
            continue
        np_ = fn(G.vec3(p), w, i)
        d = G.vlen(G.vsub(np_, p))
        if d > G.EPSILON:
            moved += 1
            max_d = max(max_d, d)
        out.append(np_)
    return DeformResult(out, moved, max_d)


def displace(positions, weights, direction: Sequence[float], amount: float) -> DeformResult:
    """Move vertices along ``direction`` scaled by their influence."""
    d = G.vnorm(direction)
    return _apply(positions, weights, lambda p, w, i: G.vadd(p, G.vmul(d, amount * w)))


def inflate(positions, weights, normals: Sequence[Sequence[float]], amount: float) -> DeformResult:
    """Push vertices along their normals (``amount`` < 0 shrinks).

    This is how "make the cheek a bit fuller" is implemented: the region is the
    cheek, the normal is the local surface direction, so volume grows outward
    instead of the mesh sliding sideways.
    """
    n = min(len(normals), len(positions))
    return _apply(
        positions,
        weights,
        lambda p, w, i: G.vadd(p, G.vmul(G.vnorm(normals[i]) if i < n else (0, 0, 1), amount * w)),
    )


def scale_about(positions, weights, center: Sequence[float], factor: Sequence[float]) -> DeformResult:
    """Scale vertices towards/away from ``center`` with a possibly non-uniform factor."""
    f = G.vec3(factor)

    def do(p, w, i):
        d = G.vsub(p, center)
        scaled = G.vhadamard(d, f)
        target = G.vadd(center, scaled)
        return G.vlerp(p, target, w)

    return _apply(positions, weights, do)


def taper(positions, weights, axis: Sequence[float], origin: Sequence[float],
          start_factor: float, end_factor: float, curve: str = "linear") -> DeformResult:
    """Scale a limb around its axis so that it gets thinner towards one end."""
    a = G.vnorm(axis)
    if G.vlen(a) < G.EPSILON:
        return DeformResult([G.vec3(p) for p in positions], 0, 0.0, ["taper: axis was degenerate"])

    ts = [G.vdot(p, a) for p in positions]
    lo, hi = min(ts), max(ts)
    span = (hi - lo) or 1.0
    affected = [i for i, w in enumerate(weights) if w > 0]
    if not affected:
        return DeformResult([G.vec3(p) for p in positions], 0, 0.0)

    def do2(p, w, i):
        t = (G.vdot(p, a) - lo) / span
        k = G.taper_scale(t, start_factor, end_factor, curve)
        # radial position around the axis line through origin
        proj = G.vdot(p, a)
        radial = G.vsub(p, G.vmul(a, proj))
        origin_radial = G.vsub(origin, G.vmul(a, G.vdot(origin, a)))
        new_radial = G.vadd(G.vmul(G.vsub(radial, origin_radial), k), origin_radial)
        target = G.vadd(G.vmul(a, proj), new_radial)
        return G.vlerp(p, target, w)

    return _apply(positions, weights, do2)


def _bend_centerline(u: float, span: float, angle: float, t_start: float,
                     curve: str, steps: int = 32) -> Tuple[G.Vec3, float]:
    """Centreline offset ``(x, z)`` and local tilt for a bend by ``angle`` rad.

    Each cross-section at height ``s`` is tilted by ``theta(s)``; the centreline
    is the integral of the tilted tangent, which keeps the rod's length intact
    instead of rotating it like a rigid stick.  Returns ``((x, z), theta)`` in the
    (a -> d) plane measured from the bend origin.
    """
    if span < G.EPSILON:
        return (0.0, 0.0), 0.0
    u = G.clamp(u / span) * span
    n = max(2, min(steps, int(u / max(span / steps, 1e-9)) + 2))
    du = u / n if n else 0.0
    x = z = 0.0
    theta_u = 0.0
    for i in range(n):
        s_mid = (i + 0.5) * du
        t = s_mid / span
        k = G.clamp((t - t_start) / max(1e-6, 1.0 - t_start))
        if curve == "smooth":
            k = k * k * (3.0 - 2.0 * k)
        theta = angle * k
        x += math.sin(theta) * du
        z += math.cos(theta) * du
        theta_u = theta
    k = G.clamp((u / span - t_start) / max(1e-6, 1.0 - t_start))
    if curve == "smooth":
        k = k * k * (3.0 - 2.0 * k)
    return (x, z), angle * k


def bend(positions, weights, axis: Sequence[float], origin: Sequence[float],
         angle_deg: float, direction: Sequence[float], t_range: Tuple[float, float] = (0.0, 1.0),
         curve: str = "smooth") -> DeformResult:
    """Bend a column along ``axis`` towards ``direction``.

    ``t_range`` defines where the bend starts along the axis (0 = base,
    1 = tip).  Cross-sections stay perpendicular to the bent centreline, so the
    trunk keeps its thickness and length.  Topology is untouched - only vertex
    positions change, which is exactly what makes the operation safe to undo and
    safe for stored regions.
    """
    a = G.vnorm(axis)
    d = G.vnorm(direction)
    if G.vlen(a) < G.EPSILON or G.vlen(d) < G.EPSILON:
        return DeformResult([G.vec3(p) for p in positions], 0, 0.0, ["bend: degenerate axis"])
    d = G.vnorm(G.vsub(d, G.vmul(a, G.vdot(a, d))))
    if G.vlen(d) < G.EPSILON:
        d = G.axis_perpendicular(a)

    ts = [G.vdot(p, a) for p in positions]
    lo, hi = min(ts), max(ts)
    span = (hi - lo) or 1.0
    angle = math.radians(angle_deg)
    t_start = G.clamp(t_range[0])
    b = G.vnorm(G.vcross(a, d))
    e2 = G.vnorm(G.vcross(a, b)) if G.vlen(G.vcross(a, b)) > 1e-9 else G.axis_perpendicular(a)
    cache: Dict[int, Tuple[G.Vec3, float]] = {}

    def do(p, w, i):
        rel = G.vsub(p, origin)
        u = G.vdot(rel, a)
        if u <= 0.0:
            return p
        key = int(u / max(span, 1e-9) * 512)
        if key in cache:
            (cx, cz), theta = cache[key]
        else:
            (cx, cz), theta = _bend_centerline(u, span, angle, t_start, curve)
            cache[key] = ((cx, cz), theta)
        if abs(theta) < 1e-9 and abs(cx) < 1e-12:
            return p
        radial = G.vsub(rel, G.vmul(a, u))
        alpha = G.vdot(radial, b)
        beta = G.vdot(radial, e2)
        a2 = G.vnorm(G.vadd(G.vmul(a, math.cos(theta)), G.vmul(d, math.sin(theta))))
        e2b = G.vnorm(G.vcross(a2, b))
        target = G.vadd(
            G.vadd(origin, G.vadd(G.vmul(a, cz), G.vmul(d, cx))),
            G.vadd(G.vmul(b, alpha), G.vmul(e2b, beta)),
        )
        return G.vlerp(p, target, w)

    return _apply(positions, weights, do)


def smooth(positions, weights, edges: Sequence[Sequence[int]], factor: float = 0.5,
           iterations: int = 1) -> DeformResult:
    """Laplacian smoothing restricted to the weighted region.

    Vertices with weight 0 are hard-pinned, so neighbouring features are not
    dragged along - that is the "smooth this transition but keep the silhouette"
    behaviour.
    """
    n = len(positions)
    adj: List[List[int]] = [[] for _ in range(n)]
    for e in edges:
        if len(e) != 2:
            continue
        a, b = int(e[0]), int(e[1])
        if 0 <= a < n and 0 <= b < n:
            adj[a].append(b)
            adj[b].append(a)
    cur = [G.vec3(p) for p in positions]
    for _ in range(max(1, iterations)):
        nxt = list(cur)
        for i in range(n):
            w = weights[i] if i < len(weights) else 0.0
            if w <= 0.0 or not adj[i]:
                continue
            avg = G.vmul(G._vsum(cur[j] for j in adj[i]), 1.0 / len(adj[i]))
            nxt[i] = G.vlerp(cur[i], avg, G.clamp(factor * w))
        cur = nxt
    moved = sum(1 for i in range(n) if G.vlen(G.vsub(cur[i], positions[i])) > G.EPSILON)
    max_d = max((G.vlen(G.vsub(cur[i], positions[i])) for i in range(n)), default=0.0)
    return DeformResult(cur, moved, max_d)


def relax_protected(weights: Sequence[float], protected: Iterable[int]) -> List[float]:
    """Force protected vertices to zero influence."""
    w = list(weights)
    for i in protected:
        if 0 <= i < len(w):
            w[i] = 0.0
    return w


def combine_weights(a: Sequence[float], b: Sequence[float], mode: str = "multiply") -> List[float]:
    """Combine two influence arrays (e.g. region AND height filter)."""
    n = max(len(a), len(b))
    out: List[float] = []
    for i in range(n):
        x = a[i] if i < len(a) else 0.0
        y = b[i] if i < len(b) else 0.0
        if mode == "multiply":
            out.append(x * y)
        elif mode == "max":
            out.append(max(x, y))
        elif mode == "min":
            out.append(min(x, y))
        else:
            raise ValueError(f"unknown combine mode: {mode!r}")
    return out


def weights_from_region(region: Region, vertex_count: int) -> List[float]:
    """Dense weight array of length ``vertex_count`` from a sparse region."""
    out = [0.0] * vertex_count
    for i, w in zip(region.vertices, region.weights):
        if 0 <= i < vertex_count:
            out[i] = w
    return out


# ---------------------------------------------------------------------------
# Serialisation (JSON friendly)
# ---------------------------------------------------------------------------


def region_to_dict(region: Region) -> Dict[str, Any]:
    return {
        "name": region.name,
        "object": region.object_name,
        "vertices": region.vertices,
        "weights": [round(w, 4) for w in region.weights],
        "kind": region.kind,
        "protected": region.protected,
        "source": region.source,
        "note": region.note,
        "created_at": region.created_at,
        "fingerprint": region.fingerprint,
        "vertex_count": region.vertex_count,
        "poly_count": region.poly_count,
        "bounds": region.bounds,
        "format": REGION_FORMAT,
    }


def region_from_dict(data: Dict[str, Any]) -> Region:
    if not isinstance(data, dict) or "name" not in data:
        raise ValueError("region document needs at least a name")
    return Region(
        name=str(data["name"]),
        object_name=str(data.get("object", "")),
        vertices=[int(i) for i in data.get("vertices", [])],
        weights=[float(w) for w in data.get("weights", [])],
        kind=str(data.get("kind", "region")),
        protected=bool(data.get("protected", False)),
        source=str(data.get("source", "manual")),
        note=str(data.get("note", "")),
        created_at=str(data.get("created_at", "")),
        fingerprint=str(data.get("fingerprint", "")),
        vertex_count=int(data.get("vertex_count", 0)),
        poly_count=int(data.get("poly_count", 0)),
        bounds=data.get("bounds"),
    )


def summarise_weights(weights: Sequence[float]) -> Dict[str, Any]:
    strong = sum(1 for w in weights if w >= 0.999)
    soft = sum(1 for w in weights if 0.0 < w < 0.999)
    return {
        "vertices": len(weights),
        "full": strong,
        "falloff": soft,
        "max_weight": round(max(weights), 4) if weights else 0.0,
        "sum_weight": round(sum(weights), 2),
    }

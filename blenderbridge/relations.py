"""Spatial relation solver.

Turns human intent such as

    "put the coconut on top of the palm, rotated along the trunk,
     sized 10 % of the palm's height"

into numbers that Blender can use, and - just as important - into an explicit
list of *assumptions* and *ambiguities* so the caller can confirm or correct.

Three inputs
------------
``spec``    what the user/agent asked for (see :func:`resolve_placement`)
``target``  measured facts about the reference object(s), produced by the add-on
``subject`` facts about the object that will be placed (its native size and
            where its origin sits relative to its bounding box)

The solver never guesses silently: every default it applies is appended to
``assumptions``; every genuinely ambiguous wording is appended to
``ambiguities`` with a default answer and the alternatives.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import geometry as G

UP = (0.0, 0.0, 1.0)

PLACEMENTS = (
    "at",
    "on",
    "above",
    "below",
    "beside",
    "left_of",
    "right_of",
    "in_front_of",
    "behind",
    "inside",
    "between",
    "on_surface",
)

ROTATION_MODES = ("none", "copy", "align_to_axis", "parallel_to_axis",
                  "perpendicular_to_normal", "face_target", "upright")

SIZE_MODES = ("native", "relative", "absolute", "fit")

DIMENSIONS = ("height", "width", "depth", "max", "volume")


class RelationError(ValueError):
    """Raised when a relation descriptor cannot be understood at all."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.details = details or {}


# ---------------------------------------------------------------------------
# Facts helpers
# ---------------------------------------------------------------------------


def _bbox(facts: Dict[str, Any]) -> Tuple[G.Vec3, G.Vec3]:
    bbox = facts.get("bbox")
    if not bbox or len(bbox) != 2:
        raise RelationError("target facts are missing a bounding box", {"facts_keys": sorted(facts)})
    return G.vec3(bbox[0]), G.vec3(bbox[1])


def _frame_axes(facts: Dict[str, Any]) -> Dict[str, G.Vec3]:
    """Object-local axes expressed in world space (fallback: world axes)."""
    default = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}
    rotation = facts.get("rotation_deg") or [0.0, 0.0, 0.0]
    if not facts.get("axes_world"):
        return default
    axes = facts["axes_world"]
    return {
        "x": G.vnorm(axes.get("x", default["x"])),
        "y": G.vnorm(axes.get("y", default["y"])),
        "z": G.vnorm(axes.get("z", default["z"])),
    }


def dimension_of(facts: Dict[str, Any], dimension: str) -> float:
    """Return one size measurement of an object in metres."""
    lo, hi = _bbox(facts)
    dims = G.aabb_dims((lo, hi))
    if facts.get("dimensions"):
        dims = G.vec3(facts["dimensions"])
    if dimension in ("height", "z"):
        return abs(dims[2])
    if dimension in ("width", "x"):
        return abs(dims[0])
    if dimension in ("depth", "y"):
        return abs(dims[1])
    if dimension in ("max", "largest"):
        return max(abs(dims[0]), abs(dims[1]), abs(dims[2]))
    if dimension in ("volume", "bbox_volume"):
        return G.aabb_volume((lo, hi))
    raise RelationError(f"unknown size dimension: {dimension!r}", {"allowed": list(DIMENSIONS)})


def resolve_point(facts: Dict[str, Any], ref: Dict[str, Any]) -> Tuple[G.Vec3, List[str], List[Dict[str, Any]]]:
    """Resolve a point reference (anchor / path position / explicit point)."""
    assumptions: List[str] = []
    ambiguities: List[Dict[str, Any]] = []

    if "point" in ref and ref["point"] is not None:
        return G.vec3(ref["point"]), assumptions, ambiguities

    lo, hi = _bbox(facts)
    name = (ref.get("anchor") or "").lower()

    # named anchors stored on the object win over generic bbox anchors
    anchors = facts.get("anchors") or {}
    if name and name in anchors:
        a = anchors[name]
        pos = a["position"] if isinstance(a, dict) else a
        return G.vec3(pos), assumptions, ambiguities

    paths = facts.get("paths") or {}
    if ref.get("path") and ref["path"] in paths:
        pts = [G.vec3(p) for p in paths[ref["path"]]]
        t = float(ref.get("t", 1.0))
        return G.polyline_point_at(pts, t), assumptions, ambiguities

    if name in ("", "center", "centre", "middle"):
        return G.aabb_center((lo, hi)), assumptions, ambiguities

    if name in ("top", "tip", "top_center", "head"):
        if "tip" in anchors:  # e.g. a named "trunk_tip" anchor
            a = anchors["tip"]
            return G.vec3(a["position"] if isinstance(a, dict) else a), assumptions, ambiguities
        return G.aabb_anchor((lo, hi), "top"), assumptions, ambiguities

    if name in ("base", "bottom", "foot", "bottom_center"):
        return G.aabb_anchor((lo, hi), "bottom"), assumptions, ambiguities

    if name in ("left", "right", "front", "back"):
        frame = (ref.get("frame") or "world").lower()
        if frame == "target":
            axes = _frame_axes(facts)
            center = G.aabb_center((lo, hi))
            extent = G.aabb_dims((lo, hi))
            vec = {"left": G.vneg(axes["x"]), "right": axes["x"],
                   "front": G.vneg(axes["y"]), "back": axes["y"],
                   "back2": axes["y"]}.get(name)
            if vec is None:
                return center, assumptions, ambiguities
            half = G.vdot(extent, G.vnorm(vec)) / 2.0
            return G.vadd(center, G.vmul(G.vnorm(vec), half)), assumptions, ambiguities
        return G.aabb_anchor((lo, hi), name), assumptions, ambiguities

    # corner style names: top_left_front ...
    try:
        return G.aabb_anchor((lo, hi), name), assumptions, ambiguities
    except ValueError:
        raise RelationError(f"unknown anchor: {name!r}", {"object": facts.get("name")})


def resolve_axis(facts: Dict[str, Any], ref: Optional[Dict[str, Any]] = None) -> G.Vec3:
    """Best-effort dominant direction of a target (trunk direction etc.)."""
    if ref and ref.get("axis"):
        return G.vnorm(ref["axis"])
    if facts.get("axis"):
        return G.vnorm(facts["axis"])
    if ref and ref.get("path"):
        paths = facts.get("paths") or {}
        if ref["path"] in paths:
            pts = [G.vec3(p) for p in paths[ref["path"]]]
            return G.polyline_tangent(pts, float(ref.get("t", 0.5)))
    lo, hi = _bbox(facts)
    dims = G.aabb_dims((lo, hi))
    dominant = max(range(3), key=lambda i: abs(dims[i]))
    return tuple(1.0 if i == dominant else 0.0 for i in range(3))  # type: ignore[return-value]


def resolve_normal(facts: Dict[str, Any]) -> G.Vec3:
    if facts.get("normal"):
        return G.vnorm(facts["normal"])
    if facts.get("axis"):
        return G.vnorm(facts["axis"])
    return UP


# ---------------------------------------------------------------------------
# Subject (the thing being placed)
# ---------------------------------------------------------------------------


def _subject_info(subject: Optional[Dict[str, Any]]) -> Tuple[G.Vec3, str]:
    """Return ``(dims, origin_mode)`` for the subject.

    ``origin_mode`` describes where the object's origin sits inside its own
    bounding box: ``center`` (Blender default for new primitives), ``base``
    (origin at the bottom centre - handy for plants and buildings) or ``min``
    (origin at the corner).
    """
    if not subject:
        return (1.0, 1.0, 1.0), "center"
    dims = G.vec3(subject.get("dimensions") or [1.0, 1.0, 1.0])
    origin = str(subject.get("origin", "center")).lower()
    if origin not in ("center", "base", "min", "top"):
        origin = "center"
    return (abs(dims[0]), abs(dims[1]), abs(dims[2])), origin


def _origin_offset(dims: G.Vec3, origin_mode: str, up_sign: float = 1.0) -> G.Vec3:
    """Offset from the object origin to its bounding-box centre."""
    if origin_mode == "base":
        return (0.0, 0.0, dims[2] / 2.0)
    if origin_mode == "top":
        return (0.0, 0.0, -dims[2] / 2.0)
    if origin_mode == "min":
        return (dims[0] / 2.0, dims[1] / 2.0, dims[2] / 2.0)
    return (0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# Main solver
# ---------------------------------------------------------------------------


def resolve_placement(spec: Dict[str, Any], targets: Dict[str, Dict[str, Any]],
                      subject: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Compute position, rotation and scale for one placement intent.

    See the module docstring and ``docs/protocol.md`` for the full schema.
    The function is pure: same input, same output, no Blender needed.
    """
    if not isinstance(spec, dict):
        raise RelationError("placement spec must be an object")

    placement = str(spec.get("placement", "at")).lower()
    if placement not in PLACEMENTS:
        raise RelationError(f"unknown placement: {placement!r}", {"allowed": list(PLACEMENTS)})

    assumptions: List[str] = []
    ambiguities: List[Dict[str, Any]] = []

    # --- resolve the reference(s) -----------------------------------------
    target_refs: List[Dict[str, Any]] = []
    if spec.get("targets"):
        target_refs = list(spec["targets"])
    elif spec.get("target"):
        target_refs = [spec["target"]]
    if not target_refs:
        raise RelationError("spec.target or spec.targets is required")

    resolved_targets: List[Dict[str, Any]] = []
    for ref in target_refs:
        obj = ref.get("object")
        facts = targets.get(obj) if obj else None
        if facts is None and len(targets) == 1 and not obj:
            facts = next(iter(targets.values()))
            obj = facts.get("name")
            assumptions.append(f"no target object given, used the only available object '{obj}'")
        if facts is None:
            raise RelationError(f"target object not found in scene facts: {obj!r}",
                                {"available": sorted(targets)})
        if ref.get("part") and facts.get("parts") and ref["part"] in facts["parts"]:
            part = dict(facts["parts"][ref["part"]])
            part.setdefault("anchors", {})
            part.setdefault("paths", {})
            part.setdefault("name", f"{obj}:{ref['part']}")
            part.setdefault("rotation_deg", facts.get("rotation_deg"))
            part.setdefault("axes_world", facts.get("axes_world"))
            part = {**facts, **part}
            facts_for_ref = part
        else:
            if ref.get("part"):
                ambiguities.append({
                    "topic": "part",
                    "question": f"'{ref['part']}' is not a known part of '{obj}'.",
                    "default": "use the whole object",
                    "options": sorted((facts.get("parts") or {}).keys()),
                })
            facts_for_ref = facts
        point, a, amb = resolve_point(facts_for_ref, ref)
        assumptions.extend(a)
        ambiguities.extend(amb)
        resolved_targets.append({"ref": ref, "facts": facts_for_ref, "point": point})

    primary = resolved_targets[0]
    tfacts = primary["facts"]
    tpoint = primary["point"]
    tbox = _bbox(tfacts)
    tcenter = G.aabb_center(tbox)
    tdims = G.aabb_dims(tbox)
    axes = _frame_axes(tfacts)

    subj_dims, origin_mode = _subject_info(subject)
    gap = float(spec.get("gap", 0.0) or 0.0)

    # --- sizing -----------------------------------------------------------
    scale, size_notes, size_amb = _resolve_scale(spec.get("size"), tfacts, subj_dims, origin_mode)
    assumptions.extend(size_notes)
    ambiguities.extend(size_amb)
    scaled_dims = (subj_dims[0] * scale[0], subj_dims[1] * scale[1], subj_dims[2] * scale[2])

    # --- position ---------------------------------------------------------
    up = G.vnorm(spec.get("up") or UP)
    position_center = _resolve_center(
        placement, spec, resolved_targets, tfacts, tpoint, tcenter, tdims,
        scaled_dims, axes, up, gap, assumptions, ambiguities,
    )

    sink = float(spec.get("sink", 0.0) or 0.0)
    if sink:
        position_center = G.vadd(position_center, G.vmul(up, -abs(sink) * scaled_dims[2]))
        assumptions.append(f"'sink' moved the object {abs(sink) * 100:.0f} % of its height into the surface")

    # convert bbox centre -> object origin
    offset = _origin_offset(scaled_dims, origin_mode)
    rotation_deg = _resolve_rotation(spec.get("rotation"), tfacts, primary["ref"], axes,
                                     subj_dims, position_center)
    rot_matrix = G.euler_matrix(*rotation_deg)
    offset_world = G.mat_apply(rot_matrix, offset)
    origin_pos = G.vsub(position_center, offset_world)

    offset_vec = spec.get("offset")
    if offset_vec:
        origin_pos = G.vadd(origin_pos, G.vec3(offset_vec))
        assumptions.append("applied an explicit offset to the computed position")

    persistence = _persistence_plan(spec.get("persist"), tfacts, primary["ref"], placement)

    return {
        "position": [round(c, 6) for c in origin_pos],
        "center": [round(c, 6) for c in position_center],
        "rotation_deg": [round(c, 4) for c in rotation_deg],
        "scale": [round(c, 6) for c in scale],
        "size_m": [round(c, 6) for c in scaled_dims],
        "target_point": [round(c, 6) for c in tpoint],
        "frame_axes": {k: [round(c, 6) for c in v] for k, v in axes.items()},
        "subject": {"dimensions": list(subj_dims), "origin": origin_mode},
        "persistence": persistence,
        "assumptions": assumptions,
        "ambiguities": ambiguities,
        "needs_confirmation": bool(ambiguities) and not bool(spec.get("accept_ambiguities")),
    }


def _resolve_scale(size_spec: Optional[Dict[str, Any]], tfacts: Dict[str, Any],
                   subj_dims: G.Vec3, origin_mode: str) -> Tuple[G.Vec3, List[str], List[Dict[str, Any]]]:
    assumptions: List[str] = []
    ambiguities: List[Dict[str, Any]] = []
    if not size_spec:
        return (1.0, 1.0, 1.0), assumptions, ambiguities

    mode = str(size_spec.get("mode", "native")).lower()
    if mode not in SIZE_MODES:
        raise RelationError(f"unknown size mode: {mode!r}", {"allowed": list(SIZE_MODES)})

    if mode == "native":
        return (1.0, 1.0, 1.0), assumptions, ambiguities

    if mode == "absolute":
        wanted = G.vec3(size_spec.get("size") or [1.0, 1.0, 1.0])
        scale = tuple(wanted[i] / subj_dims[i] if subj_dims[i] > G.EPSILON else 1.0 for i in range(3))
        return scale, ["scaled to the requested absolute size in metres"], ambiguities

    if mode == "relative":
        fraction = float(size_spec.get("fraction", 0.1))
        dimension = size_spec.get("dimension")
        tdims = G.aabb_dims(_bbox(tfacts))
        if not dimension:
            dimension = "height"
            ambiguities.append({
                "topic": "size_dimension",
                "question": "10 % of *what*? Height, width, depth or volume of the target?",
                "default": f"height ({tdims[2]:.3f} m)",
                "options": {
                    "height": round(tdims[2], 4),
                    "width": round(tdims[0], 4),
                    "depth": round(tdims[1], 4),
                    "volume": round(G.aabb_volume(_bbox(tfacts)), 4),
                },
            })
        if dimension in ("volume", "bbox_volume"):
            tv = G.aabb_volume(_bbox(tfacts))
            sv = max(G.aabb_volume(((0, 0, 0), subj_dims)), G.EPSILON)
            factor = (fraction * tv / sv) ** (1.0 / 3.0)
            assumptions.append(f"matched {fraction * 100:.1f} % of the target's bounding-box volume")
            return (factor, factor, factor), assumptions, ambiguities
        target_len = dimension_of(tfacts, dimension)
        wanted = fraction * target_len
        factor = wanted / subj_dims[2] if subj_dims[2] > G.EPSILON else 1.0
        axis_map = {"height": 2, "width": 0, "depth": 1}
        idx = axis_map.get(dimension, 2)
        factor = wanted / max(subj_dims[idx], G.EPSILON)
        assumptions.append(
            f"scaled so the subject's {dimension} equals {fraction * 100:.1f} % of the target "
            f"({wanted:.3f} m), uniform scale x{factor:.3f}"
        )
        return (factor, factor, factor), assumptions, ambiguities

    if mode == "fit":
        padding = float(size_spec.get("padding", 0.0) or 0.0)
        axes_fit = str(size_spec.get("axes", "xyz"))
        tdims = G.aabb_dims(_bbox(tfacts))
        scale = [1.0, 1.0, 1.0]
        factors = []
        for i, axis_name in enumerate("xyz"):
            avail = max(tdims[i] - 2 * padding, G.EPSILON)
            f = avail / max(subj_dims[i], G.EPSILON)
            factors.append(f)
            if axis_name in axes_fit:
                scale[i] = f
        if "uniform" in axes_fit:
            f = min(factors)
            scale = [f, f, f]
            assumptions.append("fit uniformly to stay inside the target box (kept proportions)")
        else:
            assumptions.append("stretched to fill the target box along the requested axes")
        return tuple(scale), assumptions, ambiguities

    raise RelationError(f"unknown size mode: {mode!r}")


def _resolve_center(placement: str, spec: Dict[str, Any], resolved_targets: List[Dict[str, Any]],
                    tfacts: Dict[str, Any], tpoint: G.Vec3, tcenter: G.Vec3, tdims: G.Vec3,
                    subj_dims: G.Vec3, axes: Dict[str, G.Vec3], up: G.Vec3, gap: float,
                    assumptions: List[str], ambiguities: List[Dict[str, Any]]) -> G.Vec3:
    """Where the subject's bounding-box centre belongs."""
    half_up = subj_dims[2] / 2.0

    if placement == "at":
        return tpoint

    if placement == "inside":
        return tcenter

    if placement == "on":
        return G.vadd(tpoint, G.vmul(up, half_up + gap))

    if placement == "above":
        top = G.aabb_anchor(_bbox(tfacts), "top")
        # keep the horizontal position of the anchor point, go straight up from the box top
        horizontal = G.vsub(tpoint, G.vmul(up, G.vdot(G.vsub(tpoint, top), up)))
        return G.vadd(horizontal, G.vmul(up, G.vdot(G.vsub(top, tcenter), up) + half_up + gap))

    if placement == "below":
        bottom = G.aabb_anchor(_bbox(tfacts), "bottom")
        if up == UP:
            return (tpoint[0], tpoint[1], bottom[2] - half_up - gap)
        return G.vsub(tpoint, G.vmul(up, half_up + gap))

    frame = str(spec.get("frame", "world")).lower()
    if frame not in ("world", "target"):
        raise RelationError(f"unknown frame: {frame!r}", {"allowed": ["world", "target"]})

    if placement in ("left_of", "right_of", "beside", "in_front_of", "behind"):
        if frame == "world":
            base_axes = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0)}
            if spec.get("frame") is None:
                ambiguities.append({
                    "topic": "frame",
                    "question": "Left/right/front from the world view or along the object's own axes?",
                    "default": "world view (Blender front view: -X is left, -Y is towards the viewer)",
                    "options": ["world", "target"],
                })
        else:
            base_axes = axes

        direction_map = {
            "left_of": G.vneg(base_axes["x"]),
            "right_of": base_axes["x"],
            "in_front_of": G.vneg(base_axes["y"]),
            "behind": base_axes["y"],
        }
        if placement == "beside":
            side = str(spec.get("side", "right")).lower()
            direction = G.vneg(base_axes["x"]) if side.startswith("l") else base_axes["x"]
            assumptions.append(f"'beside' interpreted as {side} of the target")
        else:
            direction = direction_map[placement]

        extent_target = _extent_along(tdims, direction)
        extent_subject = _extent_along(subj_dims, direction)
        distance = extent_target / 2.0 + extent_subject / 2.0 + gap
        if placement in ("left_of", "right_of", "beside"):
            base = G.vec3((tcenter[0], tcenter[1], tpoint[2]))
        else:
            base = G.vec3((tpoint[0], tpoint[1], tcenter[2]))
        return G.vadd(base, G.vmul(G.vnorm(direction), distance))

    if placement == "between":
        if len(resolved_targets) < 2:
            raise RelationError("'between' needs exactly two targets", {"got": len(resolved_targets)})
        p0 = resolved_targets[0]["point"]
        p1 = resolved_targets[1]["point"]
        mid = G.vlerp(p0, p1, 0.5)
        axis = G.vnorm(G.vsub(p1, p0))
        d0 = _extent_along(_bbox_dims(resolved_targets[0]["facts"]), axis) / 2.0
        d1 = _extent_along(_bbox_dims(resolved_targets[1]["facts"]), axis) / 2.0
        span_between = max(0.0, G.vlen(G.vsub(p1, p0)) - d0 - d1)
        subj_extent = _extent_along(subj_dims, axis)
        if subj_extent > span_between > G.EPSILON:
            assumptions.append(
                f"subject ({subj_extent:.3f} m) is wider than the gap between the targets "
                f"({span_between:.3f} m); it will overlap both sides"
            )
        assumptions.append("'between' placed the object at the midpoint of the two anchor points")
        return mid

    if placement == "on_surface":
        normal = resolve_normal(tfacts)
        # keep the requested point on the surface plane, then lift by half height
        plane_pt = G.project_to_plane(tpoint, tcenter, normal)
        lift = _extent_along(subj_dims, normal) / 2.0 + gap
        return G.vadd(plane_pt, G.vmul(G.vnorm(normal), lift))

    raise RelationError(f"unhandled placement: {placement!r}")


def abs_g(v: Sequence[float]) -> G.Vec3:
    return (abs(v[0]), abs(v[1]), abs(v[2]))


def _bbox_dims(facts: Dict[str, Any]) -> G.Vec3:
    return G.aabb_dims(_bbox(facts))


def _extent_along(dims: Sequence[float], direction: Sequence[float]) -> float:
    """Size of a box measured along an arbitrary direction."""
    d = G.vnorm(direction)
    return abs(d[0]) * dims[0] + abs(d[1]) * dims[1] + abs(d[2]) * dims[2]


def _resolve_rotation(rot_spec: Optional[Dict[str, Any]], tfacts: Dict[str, Any],
                      tref: Dict[str, Any], axes: Dict[str, G.Vec3],
                      subj_dims: G.Vec3, subj_center: G.Vec3) -> G.Vec3:
    if not rot_spec:
        return (0.0, 0.0, 0.0)
    if isinstance(rot_spec, str):
        rot_spec = {"mode": rot_spec}

    mode = str(rot_spec.get("mode", "none")).lower()
    if mode not in ROTATION_MODES:
        raise RelationError(f"unknown rotation mode: {mode!r}", {"allowed": list(ROTATION_MODES)})

    if mode in ("none", "upright"):
        return (0.0, 0.0, 0.0)

    if mode == "copy":
        raw = tfacts.get("rotation_deg") or rot_spec.get("value") or [0.0, 0.0, 0.0]
        return G.vec3(raw)

    if mode in ("align_to_axis", "parallel_to_axis"):
        axis = resolve_axis(tfacts, tref)
        hint = (0.0, 0.0, 1.0) if abs(axis[2]) < 0.98 else (0.0, 1.0, 0.0)
        m = G.mat_align_z_to(axis, (0.0, 0.0, 0.0), up_hint=hint)
        return G.mat_to_euler_deg(m)

    if mode == "perpendicular_to_normal":
        normal = resolve_normal(tfacts)
        hint = (0.0, 0.0, 1.0) if abs(normal[2]) < 0.98 else (0.0, 1.0, 0.0)
        m = G.mat_align_z_to(normal, (0.0, 0.0, 0.0), up_hint=hint)
        return G.mat_to_euler_deg(m)

    if mode == "face_target":
        # point the subject's local -Y at the target (lights, cameras, signs)
        goal = G.vec3(rot_spec.get("towards_point") or G.aabb_center(_bbox(tfacts)))
        direction = G.vsub(goal, subj_center)
        if G.vlen(direction) < G.EPSILON:
            direction = G.vec3(rot_spec.get("fallback_direction") or (0.0, 1.0, 0.0))
        return G.mat_to_euler_deg(G.mat_look_along(direction, (0.0, 0.0, 0.0)))

    return (0.0, 0.0, 0.0)


def _persistence_plan(persist: Any, tfacts: Dict[str, Any], tref: Dict[str, Any],
                      placement: str) -> Optional[Dict[str, Any]]:
    """Describe how to *keep* a relation when the target moves (opt-in)."""
    if not persist:
        return None
    if isinstance(persist, str):
        persist = {"mode": persist}
    mode = str(persist.get("mode", "parent")).lower()
    target_name = tfacts.get("name") or tref.get("object")
    if mode == "none":
        return None
    if mode == "parent":
        return {
            "kind": "parent",
            "target": target_name,
            "parent_type": "OBJECT",
            "keep_transform": True,
            "note": "subject follows the target's transform; move the target to move everything",
        }
    if mode == "vertex_parent":
        if not persist.get("vertex_index"):
            raise RelationError("vertex_parent needs vertex_index (from probe.nearest_vertex)")
        return {
            "kind": "parent",
            "target": target_name,
            "parent_type": "VERTEX",
            "vertex_index": int(persist["vertex_index"]),
            "note": "subject sticks to one vertex of a deforming mesh (blend shapes, armatures)",
        }
    if mode == "constraint":
        return {
            "kind": "constraint",
            "target": target_name,
            "constraints": persist.get("constraints") or [
                {"type": "COPY_LOCATION", "influence": 1.0},
                {"type": "COPY_ROTATION", "influence": 1.0},
            ],
            "note": "constraints keep position/rotation in sync without changing the hierarchy",
        }
    if mode == "surface":
        return {
            "kind": "surface",
            "target": target_name,
            "shrinkwrap": {"wrap_method": "NEAREST_SURFACEPOINT", "offset": float(persist.get("offset", 0.0))},
            "note": "Shrinkwrap keeps the subject on the target surface after the target is reshaped",
        }
    raise RelationError(f"unknown persistence mode: {mode!r}",
                        {"allowed": ["none", "parent", "vertex_parent", "constraint", "surface"]})


# ---------------------------------------------------------------------------
# Distribution ("evenly along an edge")
# ---------------------------------------------------------------------------


def distribute(spec: Dict[str, Any], targets: Dict[str, Dict[str, Any]],
               subject: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Place ``count`` copies evenly along a path, an axis or between two points."""
    count = int(spec.get("count", 3))
    if count <= 0:
        raise RelationError("count must be >= 1")
    if count > 500:
        raise RelationError("count is limited to 500 copies per call")

    ref = spec.get("target") or {}
    obj = ref.get("object")
    facts = targets.get(obj) if obj else None
    if facts is None and len(targets) == 1:
        facts = next(iter(targets.values()))
    if facts is None:
        raise RelationError(f"target object not found: {obj!r}", {"available": sorted(targets)})

    assumptions: List[str] = []
    if spec.get("between"):
        p0 = G.vec3(spec["between"][0])
        p1 = G.vec3(spec["between"][1])
        assumptions.append("distributed between the two given points")
    elif ref.get("path"):
        paths = facts.get("paths") or {}
        if ref["path"] not in paths:
            raise RelationError(f"path not found on target: {ref['path']!r}",
                                {"available": sorted(paths)})
        pts = [G.vec3(p) for p in paths[ref["path"]]]
        points = [G.polyline_point_at(pts, i / max(1, count - 1)) for i in range(count)]
        return {
            "points": [[round(c, 6) for c in p] for p in points],
            "tangents": [[round(c, 6) for c in G.polyline_tangent(pts, i / max(1, count - 1))]
                         for i in range(count)],
            "assumptions": assumptions + [f"distributed {count} copies along path '{ref['path']}'"],
            "ambiguities": [],
        }
    else:
        p0, _, _ = resolve_point(facts, {**ref, "anchor": spec.get("from", "base")})
        p1, _, _ = resolve_point(facts, {**ref, "anchor": spec.get("to", "top")})
        assumptions.append(f"distributed from '{spec.get('from', 'base')}' to '{spec.get('to', 'top')}'")

    include_ends = bool(spec.get("include_ends", True))
    points = G.distribute_along(p0, p1, count, include_ends=include_ends)
    tangent = G.vnorm(G.vsub(p1, p0))
    rotation = None
    if spec.get("rotation") and str(spec.get("rotation")).lower() in ("align", "align_to_axis", "parallel"):
        rotation = G.mat_to_euler_deg(G.mat_align_z_to(tangent, (0, 0, 0)))
    out: Dict[str, Any] = {
        "points": [[round(c, 6) for c in p] for p in points],
        "tangent": [round(c, 6) for c in tangent],
        "assumptions": assumptions,
        "ambiguities": [],
    }
    if rotation:
        out["rotation_deg"] = [round(c, 4) for c in rotation]
    return out

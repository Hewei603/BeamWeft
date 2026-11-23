"""Bound an instrument width when its longitudinal position is known only within a closed interval."""
import math

from pydantic import Field

from .observation import host_route
from .optics import StrictInput


class DetectorPositionBound(StrictInput):
    plus_minus_mm: float = Field(ge=0, description="Documented hard bound on axial detector location in mm, about each stated coordinate.")
    source: str = Field(min_length=1, max_length=500, pattern=r"\S",
                        description="Basis for this absolute position bound in the stated reference frame.")


def _travel(layout, anchors, beam_uid, target_uid, depth, frame):
    """Distances that stay in the registered segment or uninterrupted modeled uniform medium."""
    index, element, _ = anchors[target_uid]
    before, after = depth, element.length_mm - depth
    if frame == "element_depth":
        return before, after
    elements = layout.system.elements
    if beam_uid == "primary":
        indices = {e.uid: e.n for e in elements if e.kind == "space"}
    else:
        path = next(p for p in layout.incident_paths if p.uid == beam_uid)
        route, _ = host_route(path, anchors, layout.system.wavelength_nm)
        indices = {e.uid: n for e, _, n in route if e.kind == "space"}
    n = indices.get(target_uid)
    if n is not None:
        for step in (-1, 1):
            distance = 0.
            i = index + step
            while 0 <= i < len(elements) and indices.get(elements[i].uid) == n:
                # The opening belongs to the entrance of the downstream element in both directions.
                if elements[max(i, i-step)].clear_aperture:
                    break
                distance += elements[i].length_mm
                i += step
            if step < 0:
                before += distance
            else:
                after += distance
    return before, after


def apply_position_bound(layout, anchors, section, beam, axis, frame, bound, width, measured_interval=None):
    """Exact extrema of |q + dz| in homogeneous propagation, including a waist inside the interval."""
    depth = section["depth_mm"]
    delta = bound.plus_minus_mm
    if delta == 0:
        result = {"position_width_range": [width, width]}
        if measured_interval is not None:
            result["compatible_offsets_mm"] = [[0., 0.]] if clearance([width, width], measured_interval) == 0 else []
        return result
    before, after = _travel(layout, anchors, beam["uid"], section["target_uid"], depth, frame)
    if delta > min(before, after):
        return {"position_issue": "定位范围超出了可连续取样的均匀段或所选元件。请核对可达位置与界面。"}
    q = beam["field_axes"].get(axis, {}).get("q_mm")
    if not q or not all(math.isfinite(v) for v in q) or q[1] <= 0:
        return {"position_issue": "缺少该方向在介质内的传播参数，无法求定位范围内的宽度。"}
    x, rayleigh = q
    center_norm = math.hypot(x, rayleigh)
    nearest = min(delta, max(-delta, -x))
    farthest = delta if x >= 0 else -delta

    def at(offset):
        return width * (math.hypot(x + offset, rayleigh) / center_norm)

    limits = [at(nearest), at(farthest)]
    if not all(math.isfinite(w) and w > 0 for w in limits):
        return {"position_issue": "定位范围内的光斑超出数值范围，请核对输入参数和单位。"}
    result = {"position_width_range": limits}
    if measured_interval is not None:
        result["compatible_offsets_mm"] = _compatible_offsets(at, delta, nearest, measured_interval)
    return result


def clearance(first, second):
    """Positive separation of two closed intervals. Insignificant roundoff at contact counts as overlap."""
    left, right = (first, second) if first[0] <= second[0] else (second, first)
    gap = right[0] - left[1]
    return gap if gap > 32 * max(math.ulp(left[1]), math.ulp(right[0])) else 0.0


def _compatible_offsets(width_at, delta, nearest, measured):
    """Invert each monotone side separately; a reading above the waist can have two disjoint solutions."""
    def crossing(start, end, low, high, width):
        # start is the narrower end of this branch, regardless of its z.
        if width <= low:
            return start
        if width >= high:
            return end
        for _ in range(64):
            middle = (start + end) / 2
            if middle in (start, end):
                break
            if width_at(middle) < width:
                start = middle
            else:
                end = middle
        return (start + end) / 2

    low = width_at(nearest)
    ranges = []
    for end in (-delta, delta):
        high = width_at(end)
        if clearance([low, high], measured):
            continue
        if low == high:
            pair = sorted((nearest, end))
        else:
            # Keep closed endpoints when the two detector axes invert the
            # same physical width boundary with slightly different rounding.
            widths = [measured[0] - 32 * math.ulp(measured[0]), measured[1] + 32 * math.ulp(measured[1])]
            pair = sorted(crossing(nearest, end, low, high, value) for value in widths)
        if ranges and ranges[-1][1] >= pair[0]:
            ranges[-1][1] = max(ranges[-1][1], pair[1])
        else:
            ranges.append(pair)
    return ranges


def common_offsets(first, second):
    combined = []
    for a in first:
        for b in second:
            low, high = max(a[0], b[0]), min(a[1], b[1])
            if low <= high:
                combined.append([low, high])
            elif clearance(a, b) == 0:
                contact = (low + high) / 2
                combined.append([contact, contact])
    return combined

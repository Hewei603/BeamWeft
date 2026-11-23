"""Absorption-weighted Gaussian signal density sampled by incoherent geometric pump rays."""

import math

import numpy as np
from numpy.polynomial.legendre import leggauss


_RULES = tuple(((nodes + 1) / 2, weights / 2) for nodes, weights in (leggauss(8), leggauss(16)))
_RTOL = 1e-8
_RANGE_ERROR = "光线与模式在该段内的重叠超出数值范围或未能分辨。请核对长度、参考面、束心和束宽。"


def _width(axis, length, t):
    width, qx, qy, _, _ = axis
    return width * (np.hypot(qx + length * t, qy) / math.hypot(qx, qy))


def _partitions(length, depth, axes, delta, travel):
    """Bracket focus, crossings and thin deposition layers before sampling any integrand."""
    common = {0., 1.}
    for d in (1, 8, 32, 128):
        if d < depth:
            common.add(d / depth)
    for _, qx, qy, _, _ in axes:
        for widths in (-8, -1, 0, 1, 8):
            where = (-qx + widths * qy) / length
            if 0 < where < 1:
                common.add(where)
    count = len(delta)
    points = [np.full(count, t) for t in sorted(common)]
    for k, axis in enumerate(axes):
        moving = travel[:, k] != 0
        cross = np.divide(-delta[:, k], travel[:, k], out=np.zeros(count), where=moving)
        center = np.clip(cross, 0, 1)
        width = _width(axis, length, center)
        span = np.divide(width, np.abs(travel[:, k]), out=np.zeros(count), where=moving)
        for widths in (-8, -1, 0, 1, 8):
            points.append(np.clip(cross + widths * span, 0, 1))
    return np.sort(np.column_stack(points), axis=1)


def _interval(a, b, rows, length, depth, axes, delta, travel):
    """Two quadrature estimates and an envelope from affine rays and Gaussian width extrema."""
    dt = b - a
    tau = depth * dt
    deposition = -np.expm1(-tau)
    # Work in log scale so an optically thick layer can have a finite integral even if
    # its peak per unit axial length would overflow. For tiny tau use the analytic limit.
    small = tau < 1e-8
    log_mass = -depth * a + np.log(dt) + math.log(depth / -math.expm1(-depth))
    log_mass += np.log(np.divide(deposition, tau, out=np.ones_like(tau), where=~small))
    log_mass[small] -= tau[small] / 2
    log_scale = math.log(2 / math.pi) + log_mass
    values = []
    for nodes, weights in _RULES:
        cdf = -np.log1p(-deposition[:, None] * nodes)
        fraction = np.divide(cdf, tau[:, None], where=~small[:, None], out=np.empty_like(cdf))
        fraction[small] = nodes * (1 - tau[small, None] * (1 - nodes) / 2)
        t = a[:, None] + dt[:, None] * fraction
        log_value = np.broadcast_to(log_scale[:, None], t.shape).copy()
        for k, axis in enumerate(axes):
            w = _width(axis, length, t)
            distance = (delta[rows, k, None] + travel[rows, k, None] * t) / w
            log_value -= np.log(w) + 2 * np.square(distance)
        values.append(np.exp(log_value) @ weights)

    # A narrow bright interval may fall between every quadrature node. Bound its
    # possible contribution using the nearest point to the ray center and extrema
    # of w on [a,b]. Refine an interval whose bound dwarfs both sampled integrals.
    log_bound = log_scale.copy()
    for k, axis in enumerate(axes):
        waist = np.clip(-axis[1] / length, a, b)
        wmin = _width(axis, length, waist)
        wmax = np.maximum(_width(axis, length, a), _width(axis, length, b))
        begin, end = delta[rows, k] + travel[rows, k] * a, delta[rows, k] + travel[rows, k] * b
        nearest = np.where(np.signbit(begin) == np.signbit(end), np.minimum(abs(begin), abs(end)), 0.)
        log_bound -= np.log(wmin) + 2 * np.square(nearest / wmax)
    return values[1], np.abs(values[1] - values[0]), np.exp(log_bound)


def _integrate_batch(length, depth, axes, rays, n):
    count = len(rays)
    delta = rays[:, :2] - [axis[3] for axis in axes]
    travel = length * (rays[:, 2:4] / n - [axis[4] for axis in axes])
    points = _partitions(length, depth, axes, delta, travel)
    rows, col = np.nonzero(np.diff(points, axis=1) > 0)
    a, b = points[rows, col], points[rows, col + 1]
    total, uncertainty = np.zeros(count), np.zeros(count)
    for _ in range(40):
        high, error, upper = _interval(a, b, rows, length, depth, axes, delta, travel)
        if not (np.all(np.isfinite(high)) and np.all(np.isfinite(error)) and not np.any(np.isnan(upper))):
            raise ValueError(_RANGE_ERROR)
        scale = total + np.bincount(rows, weights=high, minlength=count)
        # Negligible tails can be accepted with their entire positive integral bound.
        # Apportion this budget over the outstanding intervals of each ray.
        tiny = upper <= (_RTOL * .05 * scale / np.maximum(1, np.bincount(rows, minlength=count)))[rows]
        suspect = upper > 32 * high
        error = np.where(suspect, upper, error)
        refine = ~tiny & (suspect | (error > _RTOL * .25 * high))
        done = ~refine
        total += np.bincount(rows[done], weights=high[done], minlength=count)
        uncertainty += np.bincount(rows[done], weights=error[done], minlength=count)
        if not np.any(refine):
            return total, uncertainty
        a, b, rows = a[refine], b[refine], rows[refine]
        mid = a + (b - a) / 2
        if np.any((mid <= a) | (mid >= b)) or len(mid) > 32768:
            break
        a, b, rows = np.concatenate((a, mid)), np.concatenate((mid, b)), np.tile(rows, 2)
    raise ValueError(_RANGE_ERROR)


def weighted_ray_area(length, optical_depth, signal_axes, rays, medium_n, replicate_split=None):
    """Return the inverse mean signal intensity at the absorbed pump samples, in mm².

    Each signal axis supplies entrance 1/e² radius, Re(q), Im(q), center and slope.
    Ray columns are entrance T/S coordinates in mm, reduced T/S slopes and weight
    relative to the initial pump power. Optical depth is axial Beer intensity depth.
    """
    axes = np.asarray(signal_axes, dtype=float)
    population = np.asarray(rays, dtype=float)
    if not population.size:
        raise ValueError("该段入口没有正权重光线，无法归一化吸收泵浦分布。")
    if (axes.shape != (2, 5) or not np.all(np.isfinite(axes)) or np.any(axes[:, [0, 2]] <= 0)
            or population.ndim != 2 or population.shape[1] != 5 or not np.all(np.isfinite(population))
            or np.any(population[:, 4] < 0) or not all(math.isfinite(v) and v > 0 for v in (length, optical_depth, medium_n))):
        raise ValueError("体段长度、光学厚度、介质折射率、模式状态或光线权重无效。")
    if replicate_split is not None and (type(replicate_split) is not int or not 0 <= replicate_split <= len(population)):
        raise ValueError("独立光线组的分界不在已登记的光线序列内。")
    weights = population[:, 4]
    mass = math.fsum(weights)
    if mass == 0:
        raise ValueError("该段入口没有正权重光线，无法归一化吸收泵浦分布。")
    live = np.flatnonzero(weights > 0)
    density, errors = np.zeros(len(weights)), np.zeros(len(weights))
    with np.errstate(over="ignore", under="ignore", divide="ignore", invalid="ignore"):
        for start in range(0, len(live), 128):
            ids = live[start:start + 128]
            density[ids], errors[ids] = _integrate_batch(length, optical_depth, axes, population[ids], medium_n)
    normalized = weights / mass
    mean, error = math.fsum(density * normalized), math.fsum(errors * normalized)
    if not math.isfinite(mean) or mean <= 0 or not math.isfinite(1 / mean) or not math.isfinite(error) or error > _RTOL * mean:
        raise ValueError(_RANGE_ERROR)
    result = {"area_mm2": 1 / mean, "mean_signal_density_per_mm2": mean,
              "relative_quadrature_error": error / mean, "positive_ray_count": len(live), "entrance_weight": mass}
    if replicate_split is not None:
        groups = (slice(None, replicate_split), slice(replicate_split, None))
        masses = [math.fsum(weights[s]) for s in groups]
        result["replicate_entrance_weights"] = masses
        if min(masses) > 0:
            means = [math.fsum(density[s] * (weights[s] / m)) for s, m in zip(groups, masses)]
            result["replicate_density_difference_per_mm2"] = abs(means[0] - means[1])
    return result

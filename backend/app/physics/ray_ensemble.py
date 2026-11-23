"""Geometric, weighted ray populations and conservative support boxes in (x, y, p_x, p_y)."""

import numpy as np
from .apertures import aperture_mask


def accepted_by_na(p_t, p_s, n, numerical_aperture):
    # Compare n*sin(theta) directly; this remains finite when NA == n accepts the forward hemisphere.
    slope = np.hypot(p_t, p_s) / n
    return n * (slope / np.hypot(1.0, slope)) <= numerical_aperture


def na_support(box, n, numerical_aperture):
    angular = box[2:]
    nearest = np.maximum(0.0, np.maximum(angular[:, 0], -angular[:, 1]))
    furthest = abs(angular).max(axis=1)
    if accepted_by_na(*furthest, n, numerical_aperture):
        return "all"
    if not accepted_by_na(*nearest, n, numerical_aperture):
        return "none"
    return "unresolved"


def projected_box(initial, matrices):
    bounds = np.empty_like(initial)
    for transverse, mat in enumerate(matrices):
        inputs = initial[[transverse, transverse + 2], :]
        for offset, row in zip((0, 2), mat):
            terms = row[:, None] * inputs
            bounds[transverse + offset] = terms.min(axis=1).sum(), terms.max(axis=1).sum()
    return bounds


def clip_rays(rays, split, aperture):
    keep = aperture_mask(aperture, rays[:, 0], rays[:, 1])
    if split is not None:
        split = int(np.count_nonzero(keep[:split]))
    return rays[keep], split


def transfer_rays(rays, matrices):
    # Both outputs of a 2x2 transfer must see the same entrance coordinates.
    for axis, matrix in enumerate(matrices):
        pos, direction = rays[:, axis].copy(), rays[:, axis + 2].copy()
        rays[:, axis] = matrix[0, 0] * pos + matrix[0, 1] * direction
        rays[:, axis + 2] = matrix[1, 0] * pos + matrix[1, 1] * direction


def spot_statistics(rays):
    weights = rays[:, 4]
    mass = float(weights.sum())
    if mass == 0:
        return {}
    fractions = weights / mass
    mean = fractions @ rays[:, :2]
    variance = fractions @ ((rays[:, :2] - mean) ** 2)
    return {
        "center_t_mm": float(mean[0]),
        "center_s_mm": float(mean[1]),
        "diameter_4sigma_t_mm": float(4 * np.sqrt(variance[0])),
        "diameter_4sigma_s_mm": float(4 * np.sqrt(variance[1])),
    }


def minimum_spot_in_drift(rays, length, index):
    """Minimize the centered mean squared radius of this surviving population in a uniform drift."""
    if not len(rays) or length <= 0:
        return None
    weights = rays[:, 4]
    mass = weights.sum()
    if mass <= 0:
        return None
    frac = weights / mass
    centered = rays[:, :4] - np.einsum("i,ij->j", frac, rays[:, :4])
    coordinates, slopes = centered[:, :2], centered[:, 2:] / index
    if not np.any(rays[:, 2:] != rays[0, 2:]):
        return {
            "unique": False,
            "rms_radius_mm": float(np.sqrt(np.einsum("i,ij,ij->", frac, coordinates, coordinates))),
        }
    variance = np.einsum("i,ij,ij->", frac, slopes, slopes)
    if not variance:
        return None
    cross = np.einsum("i,ij,ij->", frac, coordinates, slopes)
    depth = float(np.clip(-cross / variance, 0.0, length))
    spot = coordinates + slopes * depth
    return {
        "unique": True,
        "depth_mm": depth,
        "rms_radius_mm": float(np.sqrt(np.einsum("i,ij,ij->", frac, spot, spot))),
    }


def detector_histogram(rays, x_edges, y_edges):
    # histogram2d rows are its first coordinate; the display expects rows ordered in S (y).
    return np.histogram2d(rays[:, 1], rays[:, 0], bins=(y_edges, x_edges), weights=rays[:, 4])[0]


def split_diagnostics(rays, split, x_edges, y_edges):
    if split is None:
        return {}
    first = rays[:split]
    second = rays[split:]
    gap = 2 * abs(first[:, 4].sum() - second[:, 4].sum())
    first_grid = detector_histogram(first, x_edges, y_edges)
    second_grid = detector_histogram(second, x_edges, y_edges)
    l1 = 2 * abs(first_grid - second_grid).sum()
    return {"replicate_throughput_difference": float(gap), "replicate_map_l1": float(l1)}

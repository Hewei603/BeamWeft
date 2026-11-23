"""Hard entrance stops in the transverse T/S plane, shared by scalar fields and rays."""

import math

import numpy as np


def _coordinates(aperture, x, y):
    angle = aperture.angle_deg % 360
    if angle % 90 == 0:
        c, s = ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))[int(angle // 90)]
    else:
        theta = math.radians(angle)
        c, s = math.cos(theta), math.sin(theta)
    dx, dy = x - aperture.center_t_mm, y - aperture.center_s_mm
    return (
        (c * dx + s * dy) / (aperture.width_mm / 2),
        (-s * dx + c * dy) / ((aperture.height_mm or aperture.width_mm) / 2),
    )


def aperture_mask(aperture, x, y):
    u, v = _coordinates(aperture, x, y)
    return u * u + v * v <= 1 if aperture.shape == "ellipse" else (np.abs(u) <= 1) & (np.abs(v) <= 1)


def aperture_support(aperture, box):
    x = np.repeat(box[0], 2)
    y = np.tile(box[1], 2)
    u, v = _coordinates(aperture, x, y)
    # All four vertices are sufficient for containment in a convex stop. Disjoint projections
    # provide a conservative rejection certificate; overlapping projections can remain unresolved.
    extent = np.maximum(np.abs(u), np.abs(v)) if aperture.shape == "rectangle" else np.hypot(u, v)
    tol = 32 * np.finfo(float).eps
    if extent.max() < 1 - tol or (
        aperture.shape == "rectangle" and aperture.angle_deg % 90 == 0 and extent.max() <= 1
    ):
        return "all"
    nearest_u = max(0.0, float(u.min()), float(-u.max()))
    nearest_v = max(0.0, float(v.min()), float(-v.max()))
    separation = (
        max(nearest_u, nearest_v) if aperture.shape == "rectangle" else math.hypot(nearest_u, nearest_v)
    )
    if separation > 1 + tol:
        return "none"
    return "unresolved"

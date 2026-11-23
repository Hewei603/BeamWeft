"""Integrate a piecewise-constant referenced map over whole, registered detector bins in a plane."""

import math

import numpy as np
from scipy.sparse import csr_matrix
from .transverse_frames import transverse_matrix


def aggregate_cells(values, max_side):
    """Sum consecutive rectangular cells, retaining the final, possibly shorter, bins."""
    h, w = values.shape
    xs = np.arange(0, w, math.ceil(w/max_side))
    ys = np.arange(0, h, math.ceil(h/max_side))
    totals = np.add.reduceat(np.add.reduceat(values, ys, axis=0), xs, axis=1)
    return np.append(xs, w), np.append(ys, h), totals


def _overlaps(starts, ends, edges):
    low = np.minimum(starts, ends)[:, None]
    high = np.maximum(starts, ends)[:, None]
    return csr_matrix(np.maximum(0.0, np.minimum(high, edges[1:]) - np.maximum(low, edges[:-1])))


def _inside(low, high, edges):
    # Allow a rounding error at a shared boundary, bounded by a fraction of the narrowest bin.
    magnitude = max(abs(edges[0]), abs(edges[-1]), edges[-1] - edges[0])
    tolerance = min(16 * np.spacing(magnitude), np.diff(edges).min() * 1e-8)
    return (low >= edges[0] - tolerance) & (high <= edges[-1] + tolerance)


def _clip_areas(quads, lower, upper):
    """Batched Sutherland–Hodgman intersection of quadrilaterals with rectangular bins."""
    # Translate each polygon to its target cell to avoid cancelling large laboratory coordinates in the area.
    vertices = quads - lower[:, None, :]
    sizes = upper - lower
    counts = np.full(len(quads), 4)
    rows = np.arange(len(quads))[:, None]
    for axis, sign in ((0, 1), (0, -1), (1, 1), (1, -1)):
        index = np.arange(vertices.shape[1])
        active = index < counts[:, None]
        previous_index = np.where(index == 0, np.maximum(0, counts[:, None] - 1), index - 1)
        previous = vertices[rows, previous_index]
        limit = 0.0 if sign > 0 else sizes[:, axis, None]
        delta = sign * (vertices[:, :, axis] - limit)
        old_delta = sign * (previous[:, :, axis] - limit)
        keep = active & (delta >= 0)
        crosses = active & ((old_delta >= 0) != (delta >= 0))
        # A vertex on the boundary already belongs to the polygon. Insert each crossing only once.
        crosses &= (old_delta != 0) & (delta != 0)
        length = keep.astype(int) + crosses.astype(int)
        positions = np.cumsum(length, axis=1) - length
        counts = length.sum(axis=1)
        if not counts.any():
            return np.zeros(len(quads))
        clipped = np.zeros((len(quads), int(counts.max()), 2))
        r, j = np.nonzero(crosses)
        ratio = old_delta[r, j] / (old_delta[r, j] - delta[r, j])
        clipped[r, positions[r, j]] = previous[r, j] + ratio[:, None] * (vertices[r, j] - previous[r, j])
        r, j = np.nonzero(keep)
        clipped[r, positions[r, j] + crosses[r, j]] = vertices[r, j]
        vertices = clipped
    index = np.arange(vertices.shape[1])
    following = vertices[rows, np.where(index + 1 < counts[:, None], index + 1, 0)]
    cross = vertices[:, :, 0] * following[:, :, 1] - vertices[:, :, 1] * following[:, :, 0]
    return 0.5 * np.abs(np.where(index < counts[:, None], cross, 0).sum(axis=1))


def _rotated_integral(quads, x_edges, y_edges, density):
    low, high = quads.min(axis=1), quads.max(axis=1)
    bounds = []
    for axis, edges in enumerate((x_edges, y_edges)):
        first = np.searchsorted(edges[1:], low[:, axis], side="right")
        stop = np.searchsorted(edges[:-1], high[:, axis], side="left")
        bounds.append((first, np.maximum(0, stop - first)))
    (ix, nx), (iy, ny) = bounds
    counts = nx * ny
    ends = np.cumsum(counts, dtype=np.int64)
    starts = ends - counts
    if ends[-1] > 12_000_000:
        raise ValueError(
            "旋转且长宽比悬殊的单元需要检查超过 1200 万个格对。请在采集或计算中划定更小的测量区域，或按目标分辨率合并网格。"
        )
    integrals = np.zeros(len(quads))
    for start in range(0, int(ends[-1]), 4096):
        indices = np.arange(start, min(start + 4096, ends[-1]))
        owner = np.searchsorted(ends, indices, side="right")
        offset = indices - starts[owner]
        x = ix[owner] + offset % nx[owner]
        y = iy[owner] + offset // nx[owner]
        lower = np.column_stack((x_edges[x], y_edges[y]))
        upper = np.column_stack((x_edges[x + 1], y_edges[y + 1]))
        area = _clip_areas(quads[owner], lower, upper)
        np.add.at(integrals, owner, density[y, x] * area)
    return integrals


def detector_integrals(ref_x, ref_y, ref_values, obs_x, obs_y, offset_x, offset_y, angle_deg, flip_y):
    """Map (x,y) to offset + R(angle) (x, ±y). Return integrated signal and whole-cell support."""
    matrix = transverse_matrix(angle_deg, flip_y)
    c, s = matrix[:, 0]
    ref_density = ref_values / np.outer(np.diff(ref_y), np.diff(ref_x))
    x, y = np.meshgrid(obs_x, obs_y)
    corners = np.stack((x, y), axis=-1) @ matrix.T + [offset_x, offset_y]
    quads = np.stack((corners[:-1, :-1], corners[:-1, 1:], corners[1:, 1:], corners[1:, :-1]), axis=2)
    low, high = quads.min(axis=2), quads.max(axis=2)
    valid = _inside(low[:, :, 0], high[:, :, 0], ref_x) & _inside(low[:, :, 1], high[:, :, 1], ref_y)
    if not valid.any():
        raise ValueError("没有完整观测单元落在参考图内。请检查两图的原点、像素物方尺度、镜像、转角和视场。")
    if c == 0 or s == 0:
        # Separable overlap matrices, kept sparse for high-resolution detectors. Handle swapped axes explicitly.
        a = corners[0, :, 0 if s == 0 else 1]
        b = corners[:, 0, 1 if s == 0 else 0]
        horizontal = _overlaps(a[:-1], a[1:], ref_x if s == 0 else ref_y)
        vertical = _overlaps(b[:-1], b[1:], ref_y if s == 0 else ref_x)
        intermediate = vertical @ (ref_density if s == 0 else ref_density.T)
        integrals = (horizontal @ intermediate.T).T
    else:
        integrals = np.zeros(valid.shape)
        integrals[valid] = _rotated_integral(quads[valid], ref_x, ref_y, ref_density)
    return integrals, valid

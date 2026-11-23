"""Portable, checked full-resolution grids for scientific study results."""

import hashlib
import json

import numpy as np
from pydantic import Field, model_validator

from ..physics.raster_overlap import aggregate_cells
from ..study_api import StrictInput


class InspectNativeGrid(StrictInput):
    run_id: int = Field(gt=0, title="已保存的计算编号")
    window: tuple[int, int, int, int] | None = Field(
        default=None,
        description="Optional zero-based [left column, top row, width, height] in the native array. Omit for the whole grid.",
    )


class GridViewport(StrictInput):
    x_min_mm: float | None = None
    x_max_mm: float | None = None
    y_min_mm: float | None = None
    y_max_mm: float | None = None
    max_side: int = Field(default=300, ge=1, le=512)

    @model_validator(mode="after")
    def ordered_bounds(self):
        for lo, hi in ((self.x_min_mm, self.x_max_mm), (self.y_min_mm, self.y_max_mm)):
            if (lo is None) != (hi is None) or lo is not None and lo >= hi:
                raise ValueError("每个轴的视区应同时提供下界和上界，且上界大于下界。")
        return self


def native_grid_access(case_id, run_id):
    return {
        "action": "inspect_native_grid",
        "run_id": run_id,
        "download_path": f"/api/cases/{case_id}/studies/runs/{run_id}/native-grid",
    }


def grid_digest(x_edges_mm, y_edges_mm, cell_integrals, signal_unit):
    """Identify coordinates, cell-integrated values and units independently of machine byte order."""
    arrays = tuple(np.asarray(a) for a in (x_edges_mm, y_edges_mm, cell_integrals))
    if any(a.dtype.kind not in "buif" for a in arrays):
        raise ValueError("网格的物理坐标与各单元积分须为实数。")
    x, y, cells = (np.ascontiguousarray(a, dtype="<f8") for a in arrays)
    if x.ndim != 1 or y.ndim != 1 or min(x.size, y.size) < 2 or cells.shape != (y.size - 1, x.size - 1):
        raise ValueError("全分辨率网格的行列数须与物理边界对应。")
    if not np.all(x[1:] > x[:-1]) or not np.all(y[1:] > y[:-1]) or not all(np.isfinite(a).all() for a in (x, y, cells)):
        raise ValueError("全分辨率网格须为有限数值，坐标边界须严格递增。")
    if not isinstance(signal_unit, str) or not signal_unit.strip():
        raise ValueError("请注明网格中每个单元的积分信号单位。")
    info = json.dumps(["grid-v1", list(cells.shape), signal_unit], ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(info.encode("utf-8") + b"\0")
    for arr in (x, y, cells):
        digest.update(memoryview(arr).cast("B"))
    return digest.hexdigest()


def write_grid_file(stream, grid, digest, provenance):
    np.savez(
        stream,
        x_edges_mm=np.asarray(grid.x_edges_mm, dtype="<f8"),
        y_edges_mm=np.asarray(grid.y_edges_mm, dtype="<f8"),
        cell_integrals=np.asarray(grid.cell_integrals, dtype="<f8"),
        signal_unit=grid.signal_unit,
        grid_sha256=digest,
        provenance_json=json.dumps(provenance, ensure_ascii=False, allow_nan=False),
    )
    stream.seek(0)


def grid_summary(grid, window):
    x, y, cells = np.asarray(grid.x_edges_mm), np.asarray(grid.y_edges_mm), np.asarray(grid.cell_integrals)
    rows, cols = cells.shape
    left, top, width, height = window or (0, 0, cols, rows)
    if min(left, top) < 0 or min(width, height) < 1 or left + width > cols or top + height > rows:
        raise ValueError(f"所选窗口超出原生 {cols} 列 × {rows} 行。请按左列、上行、宽、高填写。")
    selected = cells[top : top + height, left : left + width]
    with np.errstate(over="ignore", invalid="ignore"):
        integral = float(np.sum(selected, dtype=np.float64))
    if not np.isfinite(integral):
        raise ValueError("所选区域的总积分超出数值范围，请缩小区域或核对标定。")
    return {
        "grid_shape_yx": [rows, cols],
        "window": [left, top, width, height],
        "x_range_mm": [float(x[left]), float(x[left + width])],
        "y_range_mm": [float(y[top]), float(y[top + height])],
        "integral": integral,
        "min_cell": float(selected.min()),
        "max_cell": float(selected.max()),
        "signal_unit": grid.signal_unit,
        **({"cell_integrals": selected.tolist()} if selected.size <= 256 else {}),
    }


def _axis_window(edges, low, high):
    if low is None:
        return 0, len(edges) - 1
    start = max(0, int(np.searchsorted(edges, low, side="right")) - 1)
    end = min(len(edges) - 1, int(np.searchsorted(edges, high, side="left")))
    return start, end


def _density_limits(cells, x, y):
    dx, dy = np.diff(x), np.diff(y)
    # Limit the temporary memory used to measure the common colour scale on a large detector.
    rows_per_block = max(1, 262144 // len(dx))
    low, high = np.inf, -np.inf
    for i in range(0, len(dy), rows_per_block):
        section = slice(i, i + rows_per_block)
        density = cells[section] / (dy[section, None] * dx)
        low, high = min(low, density.min()), max(high, density.max())
    return [float(low), float(high)]


def grid_view(grid, options):
    x, y, cells = np.asarray(grid.x_edges_mm), np.asarray(grid.y_edges_mm), np.asarray(grid.cell_integrals)
    left, right = _axis_window(x, options.x_min_mm, options.x_max_mm)
    top, bottom = _axis_window(y, options.y_min_mm, options.y_max_mm)
    info = {
        "original_shape_yx": list(cells.shape),
        "signal_unit": grid.signal_unit,
        "x_range_mm": [float(x[0]), float(x[-1])],
        "y_range_mm": [float(y[0]), float(y[-1])],
    }
    if right <= left or bottom <= top:
        return {
            **info,
            "status": "outside_grid",
            "native_window": None,
            "x_edges_mm": [],
            "y_edges_mm": [],
            "density": [],
            "message": "所选视区在计算网格之外。可移回已计算范围，或返回全图。",
        }
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            col, row, grouped = aggregate_cells(cells[top:bottom, left:right], options.max_side)
            x_edges, y_edges = x[left + col], y[top + row]
            density = grouped / np.outer(np.diff(y_edges), np.diff(x_edges))
            scale = _density_limits(cells, x, y)
    except FloatingPointError as exc:
        raise ValueError("换算单元面积密度时超过数值范围，请核对坐标单位和积分信号标度。") from exc
    return {
        **info,
        "status": "completed",
        "native_window": [left, top, right - left, bottom - top],
        "density_range": scale,
        "x_edges_mm": x_edges.tolist(),
        "y_edges_mm": y_edges.tolist(),
        "density": density.tolist(),
    }

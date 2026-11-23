"""Monochromatic scalar Fresnel propagation on a padded, cell-centred Cartesian grid (mm)."""

from dataclasses import dataclass
import math

import numpy as np
from scipy.fft import fft2, ifft2, fftfreq

from .apertures import aperture_mask


def fresnel_transfer(freq2, wavelength_mm, reduced_distance_mm):
    return np.exp(-1j * np.pi * wavelength_mm * reduced_distance_mm * freq2)


def edge_fraction(density):
    margin = max(1, len(density)//16)
    outer = density[:margin].sum() + density[-margin:].sum()
    outer += density[margin:-margin, :margin].sum() + density[margin:-margin, -margin:].sum()
    return float(outer) / max(float(density.sum()), 1e-300)


@dataclass(frozen=True)
class GaussianAxis:
    radius: float
    curvature: float | None = None
    center: float = 0
    slope: float = 0


class FieldGrid:
    def __init__(self, width, cells, wavelength, tangential, sagittal, offset):
        self.dx = width / cells
        size = 2 * cells
        centers = np.arange(size) + 0.5 - size / 2
        self.x = (centers + offset[0]) * self.dx
        self.y = (centers + offset[1]) * self.dx
        self.frequency = freq = fftfreq(size, self.dx)
        self.freq2 = freq[None, :] ** 2 + freq[:, None] ** 2
        self.freq_max = np.maximum(np.abs(freq[None, :]), np.abs(freq[:, None]))
        self.wavelength = wavelength
        self.edge_peak = self.wrap_risk = self.phase_risk = 0.0
        self.min_feature_cells = math.inf
        self.rows = []
        waves = []
        for axis, coord in ((tangential, self.x), (sagittal, self.y)):
            u = coord - axis.center
            curvature = 1 / axis.curvature if axis.curvature is not None else 0.0
            phase = 2 * np.pi / wavelength * (axis.slope * u + 0.5 * curvature * u * u)
            waves.append(np.exp(-((u / axis.radius) ** 2) + 1j * phase))
            # At three input radii the Gaussian intensity is already below 2e-8 of peak.
            if abs(axis.slope) + abs(curvature) * 3 * axis.radius >= min(wavelength / (2 * self.dx), 0.15):
                self.phase_risk = 1.0
            self.min_feature_cells = min(self.min_feature_cells, 2 * axis.radius / self.dx)
        self.field = (
            math.sqrt(2 / (math.pi * tangential.radius * sagittal.radius)) * waves[1][:, None] * waves[0][None, :]
        )
        self.source_fraction = self.power()
        self.check_edges()

    def intensity(self):
        return np.abs(self.field) ** 2

    def power(self):
        return float(np.sum(self.intensity()) * self.dx**2)

    def check_edges(self):
        self.edge_peak = max(self.edge_peak, edge_fraction(self.intensity()))

    def aperture(self, aperture, name, uid, z):
        mask = aperture_mask(aperture, self.x[None, :], self.y[:, None])
        width = min(aperture.width_mm, aperture.height_mm or aperture.width_mm)
        self.min_feature_cells = min(self.min_feature_cells, width / self.dx)
        before = self.power()
        self.field *= mask
        after = self.power()
        self.rows.append(
            {"element": name, "uid": uid, "z_mm": z, "before": before, "after": after, "blocked": before - after}
        )

    def phase_surface(self, c_t, c_s):
        if c_t == c_s == 0:
            return
        x = self.x[None, :]
        y = self.y[:, None]
        # Estimate the occupied bandwidth after the phase kick, including the incident wavefront.
        # Test the kick itself too: the FFT of an already aliased lens cannot diagnose its own alias.
        density = self.intensity()
        excessive = np.maximum(np.abs(x * c_t), np.abs(y * c_s)) >= min(self.wavelength / (2 * self.dx), 0.15)
        ratio = float(density[excessive].sum()) / max(float(density.sum()), 1e-300)
        self.phase_risk = max(self.phase_risk, ratio)
        self.field *= np.exp(1j * np.pi / self.wavelength * (c_t * x * x + c_s * y * y))

    def propagate(self, length, n):
        if not length:
            return
        reduced_distance = length / n
        spectrum = fft2(self.field)
        energy = np.abs(spectrum) ** 2
        total = max(float(energy.sum()), 1e-300)
        # Frequencies translating by more than half the padded period undersample the Fresnel transfer phase.
        alias = self.wavelength * reduced_distance * self.freq_max >= len(self.x) * self.dx / 2
        self.wrap_risk = max(self.wrap_risk, float(energy[alias].sum()) / total)
        high_angle = self.wavelength * np.sqrt(self.freq2) / n >= 0.15
        self.phase_risk = max(self.phase_risk, float(energy[high_angle].sum()) / total)
        transfer = fresnel_transfer(self.freq2, self.wavelength, reduced_distance)
        self.field = ifft2(spectrum * transfer)
        self.check_edges()


def block_means(grid, factor):
    h, w = grid.shape
    return grid.reshape(h // factor, factor, w // factor, factor).mean(axis=(1, 3))


def display_raster(density, width, cells, max_bins=128):
    k = (len(density)-cells)//2
    window = density[k:k+cells,k:k+cells]
    stride = max(1, cells//max_bins)
    edges = np.linspace(-width/2,width/2,cells//stride+1)
    return {"edges_mm":edges.tolist(),"density":block_means(window,stride).tolist()}, float(window.sum()*(width/cells)**2)


@dataclass
class FieldEstimate:
    density: np.ndarray
    rows: list
    dx: float
    source_fraction: float
    min_feature_cells: float
    edge_peak: float
    wrap_risk: float
    phase_risk: float


def quadrature_offsets(finite_pupil):
    return ((-0.25, -0.25), (0.25, -0.25), (-0.25, 0.25), (0.25, 0.25)) if finite_pupil else ((0.0, 0.0),)


def integrate_cells(solve, finite_pupil):
    """Quadrature over grid origin. Propagate the entire coherent field on each lattice."""
    offsets = quadrature_offsets(finite_pupil)
    estimate = None
    for offset in offsets:
        grid = solve(offset)
        density = grid.intensity() / len(offsets)
        if estimate is None:
            rows = [{**row, "before": 0.0, "after": 0.0, "blocked": 0.0} for row in grid.rows]
            estimate = FieldEstimate(density, rows, grid.dx, 0.0, grid.min_feature_cells, 0.0, 0.0, 0.0)
        else:
            estimate.density += density
        estimate.source_fraction += grid.source_fraction / len(offsets)
        estimate.edge_peak = max(estimate.edge_peak, grid.edge_peak)
        estimate.wrap_risk = max(estimate.wrap_risk, grid.wrap_risk)
        estimate.phase_risk = max(estimate.phase_risk, grid.phase_risk)
        for row, local in zip(estimate.rows, grid.rows):
            for key in ("before", "after", "blocked"):
                row[key] += local[key] / len(offsets)
        del grid
    return estimate


def field_result(fine, coarse, width, cells):
    density = fine.density
    coarse_density = coarse.density
    total = float(density.sum() * fine.dx**2)
    if not math.isfinite(total) or not np.isfinite(coarse_density).all():
        raise ValueError("场计算超出数值范围，请核对波长、曲率、窗口和长度的单位。")
    relative_shape_error = float(np.abs(block_means(density, 2) - coarse_density).sum()) / max(
        float(coarse_density.sum()), 1e-300
    )
    peak_power_error = max((abs(a["after"] - b["after"]) for a, b in zip(fine.rows, coarse.rows)), default=0.0)
    warnings = []
    if abs(1 - fine.source_fraction) > 0.001 or abs(1 - coarse.source_fraction) > 0.005:
        warnings.append("源场积分偏离单位入射功率。请扩大视场以容纳偏心或宽光束，并为窄束增加采样。")
    if fine.min_feature_cells < 12:
        warnings.append("孔径或入射束的最窄全宽不足 12 个采样间距。请增加采样或缩小视场。")
    if fine.edge_peak > 0.003 or fine.wrap_risk > 0.01:
        warnings.append("场到达填充边界或有显著频谱超过当前传播距离的采样范围。请同时扩大视场并提高采样。")
    if fine.phase_risk > 0.005:
        warnings.append("局部角度超过近轴范围或相位变化过快。请核对光源与元件，并提高采样作对照。")
    if relative_shape_error > 0.05 or peak_power_error > 0.01:
        warnings.append("两种网格的强度分布或孔面通过功率尚未收敛，请提高采样并检查视场。")
    if total < 1e-12:
        warnings.append("通过功率太低，当前网格不能可靠解析残余光场。请核对光束与孔径位置。")
    raster,window_power = display_raster(density,width,cells)
    return {
        "status": "partial" if warnings else "completed",
        "warnings": warnings,
        "throughput": total,
        "window_power": window_power,
        "source_fraction": fine.source_fraction,
        "shape_change": relative_shape_error,
        "pupil_change": peak_power_error,
        "edge_fraction": fine.edge_peak,
        "alias_fraction": fine.wrap_risk,
        "phase_risk": fine.phase_risk,
        "cell_mm": fine.dx,
        "numerics": {
            "method": "scalar-paraxial-fresnel-transfer-fft",
            "window_mm": width,
            "padded_width_mm": len(density) * fine.dx,
            "fine_cells_per_padded_side": len(density),
            "coarse_cells_per_padded_side": len(coarse_density),
            "fine_spacing_mm": fine.dx,
            "coarse_spacing_mm": coarse.dx,
            "quadrature_offsets_ts_cells": [list(pair) for pair in quadrature_offsets(bool(fine.rows))],
            "quadrature_weighting": "equal-weight-intensities-after-coherent-propagation",
            "normalization": "unit-power-at-original-unbounded-gaussian-launch",
            "phase_convention": "transverse-envelope-without-longitudinal-carrier",
        },
        "rows": fine.rows,
        "grid": raster,
    }

"""Back focal plane of a positive ideal thin lens, from a sampled scalar pupil field."""
from dataclasses import dataclass
import math

import numpy as np
from scipy.fft import fft2, fftshift


# Limit one full-precision transform, including zero padding, before allocating its buffers.
MAX_FFT_CELLS = 8_388_608


@dataclass
class FocalPlane:
    x_edges_mm: np.ndarray
    y_edges_mm: np.ndarray
    cell_integrals: np.ndarray
    metrics: dict
    warnings: list[str]
    signal_unit: str


def _axis(count, step):
    # fftshift puts DC at count//2 for both parities. Edges surround actual FFT samples.
    return (np.arange(count + 1, dtype=float) - count//2 - .5) * step


def focal_plane(field, *, dx_mm, dy_mm, wavelength_mm, focal_mm, padding=1, pupil_power_w=None):
    """Rows and columns must already increase along physical +y/+x; wavelength is in the medium."""
    if field.ndim != 2 or min(field.shape) < 2:
        raise ValueError("入瞳至少需要 2 行、2 列，请核对采样的两个横向维度。")
    if not all(math.isfinite(v) and v > 0 for v in (dx_mm, dy_mm, wavelength_mm, focal_mm)):
        raise ValueError("物理像素间距、介质波长和焦距须为正的有限值。")
    h, w = field.shape
    ny, nx = h*padding, w*padding
    if nx*ny > MAX_FFT_CELLS:
        raise ValueError(f"补零后网格 {ny} × {nx} 超过本次 {MAX_FFT_CELLS} 个 FFT 采样的容量。"
                         "请降低补零倍数，或提供物理范围已明确的较小原件。")
    sample_area = dx_mm * dy_mm
    sx, sy = wavelength_mm*focal_mm/(nx*dx_mm), wavelength_mm*focal_mm/(ny*dy_mm)
    if not all(math.isfinite(v) and v > 0 for v in (sample_area, sx, sy, sx*sy)):
        raise ValueError("输入比例超出可表示的面积或焦面坐标范围，请核对物理单位。")
    x_edges, y_edges = _axis(nx,sx), _axis(ny,sy)
    if not np.isfinite(x_edges).all() or not np.isfinite(y_edges).all():
        raise ValueError("焦面物理坐标超出数值范围，请核对波长、焦距与间距。")
    flattened = field.ravel()
    original_power = float(np.vdot(flattened, flattened).real * sample_area)
    del flattened
    if not math.isfinite(original_power):
        raise ValueError("入瞳场积分超出数值范围，请核对输入幅度。")
    if original_power == 0 and np.any(field):
        raise ValueError("非零入瞳场的功率积分低于可表示范围，请统一调整输入场的幅度单位。")
    gain = 1.
    if pupil_power_w is not None:
        if original_power <= 0:
            raise ValueError("零场无法归一到给定的入瞳功率，请核对振幅及功率标定。")
        gain = pupil_power_w/original_power
    expected = original_power if pupil_power_w is None else pupil_power_w
    # Orthonormal DFT preserves the sum of squared amplitudes. The physical input area
    # makes each output bin equivalent to |FFT(E) dx dy / (lambda f)|² dX dY.
    spectrum = fft2(field, s=(ny,nx), norm="ortho")
    cells = np.abs(spectrum)
    del spectrum
    np.square(cells, out=cells)
    cells *= sample_area*gain
    cells = fftshift(cells)
    total = float(np.sum(cells))
    if not math.isfinite(total) or (original_power > 0 and total <= 0):
        raise ValueError("焦面积分超出可表示范围，请核对场标度与入瞳功率。")
    dc = float(cells[ny//2,nx//2])
    metrics = {"input_shape":[h,w], "fft_shape":[ny,nx], "fourier_pitch_x_mm":sx, "fourier_pitch_y_mm":sy,
               "dc_row_column_zero_based":[ny//2,nx//2], "input_integral":expected, "output_integral":total,
               "dc_integral":dc, "dc_fraction":dc/total if total else None,
               "parseval_relative_error":abs(total-expected)/expected if expected else None}
    warnings = []
    if total:
        row, col = np.unravel_index(np.argmax(cells), cells.shape)
        peak = float(cells[row,col])
        metrics.update(peak_x_mm=float((col-nx//2)*sx), peak_y_mm=float((row-ny//2)*sy),
                       peak_bin_count=int(np.count_nonzero(cells >= peak*(1-1e-12))))
        # Quantify energy in bins whose physical slope challenges the paraxial model.
        slopes_x_sq = ((np.arange(nx)-nx//2) * (wavelength_mm/(nx*dx_mm)))**2
        slopes_y_sq = ((np.arange(ny)-ny//2) * (wavelength_mm/(ny*dy_mm)))**2
        far = slopes_y_sq[:,None] + slopes_x_sq[None,:] >= .15**2
        fraction = float(np.sum(cells, where=far)/total)
        metrics["high_angle_fraction"] = fraction
        if fraction > .001:
            warnings.append(f"{fraction:.2%} 的离散积分落在横向方向余弦 ≥ 0.15 的区域，"
                            "这些位置的近轴焦距映射应与高数值孔径模型核对。")
    else:
        warnings.append("入瞳采样场为零，焦面没有可以定位的光斑。")
    return FocalPlane(x_edges, y_edges, cells, metrics, warnings, "W" if pupil_power_w is not None else "相对场强积分单位")

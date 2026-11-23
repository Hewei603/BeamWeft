"""Registered focal diffraction of archived sampled fields. Arrays use row, column order."""
from typing import Annotated, Literal

import numpy as np
from pydantic import Field, model_validator

from ..physics.focal_plane import MAX_FFT_CELLS, focal_plane
from ..physics.raster_overlap import aggregate_cells
from ..study_api import RasterView, StrictInput, Study
from .grid_export import grid_digest
from .grid_original import GridOriginal, read_grid
from .intensity_map import INTENSITY_MAP, IntensityMap
from .project_outputs import WAVELENGTH


class CalibratedPhase(StrictInput):
    image: GridOriginal = Field(title="同一孔面的相位原件")
    scale_rad_per_unit: float = Field(title="每单位读数的相位增量 / rad")
    offset_rad: float = Field(default=0, title="零读数的相位 / rad")


class LensFocalIntensity(StrictInput):
    complex_field: GridOriginal | None = Field(default=None, title="入瞳复场原件",
        description="已知相干场的复数网格，实数网格在此解释为有符号实部且虚部为零。可另叠加相位。")
    amplitude: GridOriginal | None = Field(default=None, title="入瞳振幅或强度原件",
        description="单通道非负采样。若为线性强度，选择强度口径以取平方根。")
    uniform_amplitude: float | None = Field(default=None, ge=0, title="已知均匀入射振幅",
        description="在相位原件的整个矩形采样范围内采用此场振幅，范围外取零。")
    amplitude_kind: Literal["amplitude","intensity"] = Field(default="amplitude", title="振幅原件的读数口径",
        json_schema_extra={"x-enum-labels":{"amplitude":"线性场振幅","intensity":"线性光强"}})
    phase: GridOriginal | None = Field(default=None, title="相位原件")
    phase_scale_rad_per_unit: float | None = Field(default=None, title="每单位原始相位读数 / rad",
        description="有符号的线性读数标定。非线性响应请先按校准曲线还原为弧度，将弧度原件按系数 1 读取。")
    phase_offset_rad: float = Field(default=0, title="原件零读数相位 / rad")
    zero_phase: bool = Field(default=False, title="选用平坦的入射相位",
        description="仅振幅或光强不能给出相位。在有依据时勾选，表示在此孔面采用常相位。")
    phase_layers: list[CalibratedPhase] = Field(default_factory=list, max_length=16, title="同一孔面叠加的已标定相位")
    wavelength_nm: float = Field(gt=0, title="真空波长 / nm")
    medium_n: float = Field(default=1, gt=0, title="焦区介质的相位折射率")
    focal_length_mm: float = Field(gt=0, title="该介质中的正透镜焦距 / mm")
    pitch_x_um: float = Field(gt=0, title="入瞳相邻列的物理间距 / µm")
    pitch_y_um: float = Field(gt=0, title="入瞳相邻行的物理间距 / µm")
    row_direction: Literal["positive_y","negative_y"] = Field(title="原件行号增加所对应的物理方向",
        json_schema_extra={"x-enum-labels":{"positive_y":"行号增加对应 +y","negative_y":"行号增加对应 −y"}})
    column_direction: Literal["positive_x","negative_x"] = Field(default="positive_x", title="原件列号增加所对应的方向",
        json_schema_extra={"x-enum-labels":{"positive_x":"列号增加对应 +x","negative_x":"列号增加对应 −x"}})
    zero_padding: Annotated[int, Field(ge=1, le=8)] = Field(default=1,title="每轴 FFT 补零倍数",
        description="1 保留原生频率格，补零可细分焦面采样。需补充孔面数据才能恢复未取到的空间频率。")
    pupil_power_w: float | None = Field(default=None, gt=0, title="该入瞳采样范围的独立功率标定 / W",
        description="有独立测量时可将全部入瞳样本的积分归一至此值。留空保留原始场标度。")
    basis: str = Field(min_length=1, max_length=2000, pattern=r"\S", title="入瞳、采样与相位标定依据",
        description="记录照明、入瞳参考面、波长、像元物方尺度和方向。多份网格须来自同一坐标系和采样位置。")

    @model_validator(mode="after")
    def complete_field(self):
        if sum(value is not None for value in (self.complex_field,self.amplitude,self.uniform_amplitude)) != 1:
            raise ValueError("请选择一种入瞳场来源：复场原件、非负振幅或光强原件、均匀振幅。")
        if self.amplitude is None and self.amplitude_kind != "amplitude":
            raise ValueError("强度开平方只适用于非负振幅或强度原件。")
        if (self.phase is None) != (self.phase_scale_rad_per_unit is None) or (self.phase is None and self.phase_offset_rad):
            raise ValueError("相位原件需要对应的弧度标定系数，已校准为弧度时请填 1。")
        if self.phase_scale_rad_per_unit == 0 or any(layer.scale_rad_per_unit == 0 for layer in self.phase_layers):
            raise ValueError("相位读数换算系数不能为零，请核对相位标定。")
        if self.zero_phase and (self.complex_field is not None or self.phase is not None):
            raise ValueError("复场或相位原件已指定入射相位，请核对平坦相位选项。")
        if self.complex_field is None and self.phase is None and not self.zero_phase:
            raise ValueError("仅凭光强或振幅无法恢复入瞳相位。请提供相位原件，或明确采用平坦入射相位。")
        if self.uniform_amplitude is not None and self.phase is None and not self.phase_layers:
            raise ValueError("均匀振幅需至少一份相位网格来确定入瞳的采样范围。")
        return self


def _pupil(args):
    max_cells = MAX_FFT_CELLS // args.zero_padding**2
    if args.complex_field is not None:
        values = read_grid(args.complex_field, allow_complex=True, max_cells=max_cells).astype(np.complex128, copy=True)
    elif args.amplitude is not None:
        samples = read_grid(args.amplitude, max_cells=max_cells)
        if np.any(samples < 0):
            raise ValueError("振幅或线性光强原件含负值，请核对标定。带符号的场可作为实部输入。")
        if args.amplitude_kind == "intensity":
            samples = np.sqrt(samples.astype(float, copy=False))
        values = samples.astype(np.complex128)
    else:
        values = None
    layers = [(item.image,item.scale_rad_per_unit,item.offset_rad) for item in args.phase_layers]
    if args.phase is not None:
        layers.insert(0,(args.phase,args.phase_scale_rad_per_unit,args.phase_offset_rad))
    for original, scale, offset in layers:
        phase = read_grid(original, max_cells=max_cells)
        if values is None:
            values = np.full(phase.shape,args.uniform_amplitude,dtype=np.complex128)
        if phase.shape != values.shape:
            raise ValueError(f"相位原件的行列数 {phase.shape} 与入瞳场 {values.shape} 不符，请先配准到同一采样网格。")
        values *= np.exp(1j*(phase.astype(float,copy=False)*scale+offset))
    if args.row_direction == "negative_y":
        values = values[::-1,:]
    if args.column_direction == "negative_x":
        values = values[:,::-1]
    return values


def native_focal_plane(args):
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            return focal_plane(_pupil(args), dx_mm=args.pitch_x_um*.001, dy_mm=args.pitch_y_um*.001,
                               wavelength_mm=args.wavelength_nm*1e-6/args.medium_n, focal_mm=args.focal_length_mm,
                               padding=args.zero_padding,pupil_power_w=args.pupil_power_w)
    except (FloatingPointError, OverflowError, ZeroDivisionError) as exc:
        raise ValueError("数值超出场传播范围，请核对间距、波长、焦距和场标定。") from exc


def _bin(plane, maximum):
    x, y, totals = aggregate_cells(plane.cell_integrals, maximum)
    return plane.x_edges_mm[x], plane.y_edges_mm[y], totals


def lens_focal_intensity(args):
    plane = native_focal_plane(args)
    x, y, cells = _bin(plane, 512)
    quality = "limited" if plane.warnings else "conditional"
    published = IntensityMap(x_edges_mm=x.tolist(), y_edges_mm=y.tolist(), values=cells.tolist(),
        signal_unit=plane.signal_unit, x_label="焦面 x", y_label="焦面 y", basis=args.basis,
        wavelength_nm=args.wavelength_nm, plane="理想薄透镜后焦面", quality=quality, warnings=plane.warnings)
    gx, gy, preview = _bin(plane, 96)
    density = preview/np.outer(np.diff(gy), np.diff(gx))
    if not np.isfinite(density).all():
        raise ValueError("焦面功率密度超出数值范围，请核对物理单位。")
    metrics = plane.metrics
    return {"status":"partial" if plane.warnings else "completed", **metrics,
            "signal_unit":plane.signal_unit, "intensity_map":published.model_dump(mode="json",exclude_none=True),
            "published_shape":list(cells.shape), "preview_shape":list(density.shape),
            "grid":{"x_edges_mm":gx.tolist(),"y_edges_mm":gy.tolist(),"density":density.tolist(),
                    "density_label":f"焦面信号密度 / {plane.signal_unit}/mm²"},
            "native_grid_sha256": grid_digest(plane.x_edges_mm,plane.y_edges_mm,plane.cell_integrals,plane.signal_unit),
            "warnings":plane.warnings,
            "assumptions":["单色、标量相干、近轴及理想无损薄透镜。给定网格为入瞳处的复包络，网格之外场为零。",
              "入瞳像元中心作离散积分取样。焦面覆盖可采样的一段空间频谱。未额外乘以像元填充因子或连续像素的 sinc 包络。",
              "光强使用二维傅里叶变换的模平方。焦面单元积分和输入像元面积积分按 Parseval 核对。补零加密焦面网格，不能校验未知的孔外照明或高频混叠。",
              "峰位和零级积分取自原生计算网格。并列峰给出峰格数量与其中首个坐标。图形展示每面积积分密度，可引用的网格按格保存积分信号。"]}


LENS_FOCAL_STUDY = Study("lens_focal_intensity", "理想透镜的后焦面衍射", "1",
    "用已采样的入瞳复场，或同一孔面的振幅与已标定相位，计算理想薄透镜后焦面的二维光强、零级能量与峰位。",
    LensFocalIntensity, lens_focal_intensity, native_grid=native_focal_plane,
    input_ports={"wavelength_nm":WAVELENGTH},
    output_ports={"intensity_map":INTENSITY_MAP.model_copy(update={"available_on_partial":True})},
    metrics={"peak_x_mm":"峰值横坐标 / mm","peak_y_mm":"峰值纵坐标 / mm","dc_fraction":"零频采样单元占总积分的比例",
             "fourier_pitch_x_mm":"焦面原生横向采样间隔 / mm","fourier_pitch_y_mm":"焦面原生纵向采样间隔 / mm",
             "parseval_relative_error":"输入与输出积分的相对差"},
    raster=RasterView(x="grid.x_edges_mm",y="grid.y_edges_mm",z="grid.density",label="单位面积光强 / 积分单位·mm⁻²",
                      value_label_path="grid.density_label",native_density=True,
                      x_label="焦面 x / mm",y_label="焦面 y / mm",equal_scale=True,power_db=True),
    keywords=("Fourier optics","back focal plane","pupil","Fraunhofer","衍射","傅里叶光学","振幅","相位","焦面","相位标定"))

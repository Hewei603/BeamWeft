"""Single-pass paraxial transfer of explicit weighted rays or bounds on their launch coordinates."""

import math
from typing import Annotated

import numpy as np
from pydantic import ConfigDict, Field, model_validator

from ..physics.apertures import aperture_mask, aperture_support
from ..physics.ray_ensemble import (
    accepted_by_na,
    clip_rays,
    detector_histogram,
    minimum_spot_in_drift,
    na_support,
    projected_box,
    split_diagnostics,
    spot_statistics,
    transfer_rays,
)
from ..study_api import ConditionMatch, MissingStudyInput, Port, RasterView, StrictInput, Study
from .beam_source import compose_ray_matrices
from .forward_path import forward_path
from .intensity_map import INTENSITY_MAP
from .observation import OpticalLayout
from .optics import ClearAperture, _matrix, model_wavelength_notes
from .project_outputs import LAYOUT, OUTGOING_POWER, VACUUM_NM, at_wavelength
from .ray_source import RAY_BUNDLE, RAY_SUPPORT, RayBundle, RaySupport, ray_output


class TraceRays(StrictInput):
    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {"required": [name], "properties": {name: {"not": {"type": "null"}}}}
                for name in ("input_bundle", "input_support")
            ]
        }
    )
    layout: OpticalLayout = Field(title="工程光路模型")
    input_bundle: RayBundle | None = Field(default=None, title="已知权重的入射光线")
    input_support: RaySupport | None = Field(default=None, title="入射位置与方向的已知边界")
    beam_uid: str = Field(
        default="primary",
        min_length=1,
        max_length=100,
        title="光路标识",
        json_schema_extra={"x-beamweft-input": "path-uid"},
    )
    plane_uid: Annotated[str, Field(min_length=1, max_length=100)] | None = Field(
        default=None, title="观察面标识", json_schema_extra={"x-beamweft-input": "plane-uid"}
    )
    window_mm: float = Field(default=4, gt=0, le=1e5, title="探测视场全宽 / mm")
    samples: int = Field(default=32, ge=1, le=256, title="探测器每边分箱数")
    center_t_mm: float = Field(default=0, title="探测视场中心 T / mm")
    center_s_mm: float = Field(default=0, title="探测视场中心 S / mm")
    receiver_opening: ClearAperture | None = Field(
        default=None,
        title="接收器在观察面上的有效开口",
        description="填写投影到当前展开 T/S 平面的实际集光区域。留空表示仅按角度筛选。",
    )
    receiver_na: float | None = Field(
        default=None,
        ge=0,
        title="接收器对展开光轴的数值孔径",
        description="接收半角按出射侧折射率乘 sinθ 定义。与接收开口一起使用时，逐条光线同时满足两个条件才计入。",
    )
    launched_power_w: float | None = Field(
        default=None,
        ge=0,
        title="最初发射面的入射总功率 / W",
        description="选取本束光最初归一化时的总功率。续传经过截光的光线时仍使用同一发射功率。",
    )

    @model_validator(mode="after")
    def one_source(self):
        if self.input_bundle is not None and self.input_support is not None:
            raise ValueError("请选择一份加权光线或一份入射边界，分别运行即可比较两种来源。")
        return self


def _matrices(element, index, length, wavelength):
    if index is None and element.kind in {"space", "grin", "thick_lens"}:
        raise MissingStudyInput(f"请填写「{element.name}」在本束光波长处的折射率。")
    if element.kind == "registered":
        law = element.prescription.evaluate("ray_matrix", wavelength, length)
        return (
            tuple(np.array(law[key]).reshape(2, 2) for key in ("matrix_t", "matrix_s")),
            [f"「{element.name}」{note}" for note in law.get("warnings", [])],
        )
    optic = element.model_copy(update={"n": index, "length_mm": length})
    return tuple(_matrix(optic, axis, wavelength) for axis in ("tangential", "sagittal")), []


def _ray_map(grid, x_edges, y_edges, name, wl, quality, warnings):
    return dict(
        x_edges_mm=x_edges.tolist(),
        y_edges_mm=y_edges.tolist(),
        values=grid.tolist(),
        signal_unit="原始入射功率比例",
        x_label="切向 T",
        y_label="弧矢 S",
        plane=name,
        wavelength_nm=wl,
        quality=quality,
        basis="按到达各矩形探测单元的几何光线权重求和；归一化基准为原始发射功率。",
        warnings=warnings,
    )


class Population:
    def __init__(self, source):
        rows = np.asarray(source.rays, dtype=float).reshape(-1, 5)
        live = rows[:, 4] > 0
        self.rays = rows[live]
        self.split = int(live[: source.replicate_split].sum()) if source.replicate_split is not None else None
        self.drifts = []

    def mass(self):
        return float(self.rays[:, 4].sum())

    def limit(self, aperture):
        before = self.mass()
        self.rays, self.split = clip_rays(self.rays, self.split, aperture)
        after = self.mass()
        return {"before": before, "after": after, "blocked": before - after}

    def transfer(self, matrices):
        transfer_rays(self.rays, matrices)

    def slope(self, n):
        return float(np.hypot(self.rays[:, 2], self.rays[:, 3]).max(initial=0)) / n

    def finish(self, args, frame, quality, warnings, archive):
        base = np.linspace(-0.5, 0.5, args.samples + 1) * args.window_mm
        xs, ys = base + args.center_t_mm, base + args.center_s_mm
        if not (np.diff(xs) > 0).all() or not (np.diff(ys) > 0).all():
            raise ValueError("探测视场在当前中心坐标下无法分辨，请核对视场和坐标单位。")
        grid = detector_histogram(self.rays, xs, ys)
        area = np.outer(np.diff(ys), np.diff(xs))
        diagnostics = split_diagnostics(self.rays, self.split, xs, ys)
        result = {
            "throughput": self.mass(),
            "window_power": float(grid.sum()),
            "surviving_rays": len(self.rays),
            **diagnostics,
            **spot_statistics(self.rays),
            "segment_minima": self.drifts,
            "grid": {"x_edges_mm": xs.tolist(), "y_edges_mm": ys.tolist(), "density": (grid / area).tolist()},
        }
        if args.receiver_opening or args.receiver_na is not None:
            keep = np.ones(len(self.rays), dtype=bool)
            if args.receiver_opening:
                keep &= aperture_mask(args.receiver_opening, self.rays[:, 0], self.rays[:, 1])
            if args.receiver_na is not None:
                keep &= accepted_by_na(self.rays[:, 2], self.rays[:, 3], frame["medium_n"], args.receiver_na)
            result["accepted_fraction"] = float(self.rays[keep, 4].sum())
            if self.split is not None:
                weights = self.rays[:, 4] * keep
                result["replicate_accepted_difference"] = float(
                    2 * abs(weights[: self.split].sum() - weights[self.split :].sum())
                )
        if archive:
            result["exit_bundle"] = {
                **frame,
                "quality": quality,
                "warnings": warnings,
                "rays": self.rays.tolist(),
                "replicate_split": self.split,
            }
            result["intensity_map"] = _ray_map(
                grid,
                xs,
                ys,
                frame["plane"],
                frame["wavelength_nm"],
                "usable" if quality == "documented" else quality,
                warnings,
            )
        return result


class Boundary:
    def __init__(self, source):
        self.initial = np.array([source.x_mm, source.y_mm, source.p_t, source.p_s], dtype=float)
        self.matrices = (np.eye(2), np.eye(2))
        self.lo, self.hi = source.throughput_bounds

    def limit(self, aperture):
        state = aperture_support(aperture, projected_box(self.initial, self.matrices))
        if state != "all":
            self.lo = 0.0
        if state == "none":
            self.hi = 0.0
        return {
            "certificate": state,
            "decision": {"none": "肯定全挡", "all": "肯定全通过", "unresolved": "边界不足以确定通过份额"}[
                state
            ],
            "throughput_min": self.lo,
            "throughput_max": self.hi,
        }

    def transfer(self, matrices):
        # Reproject the original box through the accumulated matrix. Reboxing at every face
        # destroys position–slope correlations and can hide a real conjugate plane.
        self.matrices = tuple(compose_ray_matrices(m, b) for m, b in zip(matrices, self.matrices))

    def slope(self, n):
        p = projected_box(self.initial, self.matrices)[2:]
        return float(np.hypot(*np.abs(p).max(axis=1))) / n

    def finish(self, args, frame, quality, warnings, archive):
        box = projected_box(self.initial, self.matrices)
        bounds = box.tolist()
        result = {
            "throughput_min": self.lo,
            "throughput_max": self.hi,
            "throughput_bounds": [self.lo, self.hi],
        }
        gates = []
        if args.receiver_opening:
            gates.append(aperture_support(args.receiver_opening, box))
        if args.receiver_na is not None:
            gates.append(na_support(box, frame["medium_n"], args.receiver_na))
        if gates:
            result.update(
                accepted_min=self.lo if all(g == "all" for g in gates) else 0.0,
                accepted_max=0.0 if "none" in gates else self.hi,
            )
            result["accepted_bounds"] = [result["accepted_min"], result["accepted_max"]]
        if self.hi:
            result.update(
                x_min_mm=bounds[0][0], x_max_mm=bounds[0][1], y_min_mm=bounds[1][0], y_max_mm=bounds[1][1]
            )
            if archive:
                result["exit_support"] = {
                    **frame,
                    "quality": quality,
                    "warnings": warnings,
                    **dict(zip(("x_mm", "y_mm", "p_t", "p_s"), bounds)),
                    "throughput_bounds": [self.lo, self.hi],
                }
        return result


def _transport(route, source, state, exit_n, *, sample_drifts):
    warnings = list(source.warnings)
    model_notes = model_wavelength_notes([el for el, _, _ in route.elements], route.wavelength_nm)
    if source.wavelength_nm is None and route.wavelength_nm is not None:
        model_notes.append(f"光源未登记波长，本次按工程的 {route.wavelength_nm:g} nm 处方作条件计算。")
    quality = source.quality
    z = route.start_mm
    max_slope = state.slope(source.medium_n)
    rows = []
    for el, index, length in route.elements:
        matrices, notes = _matrices(el, index, length, route.wavelength_nm)
        model_notes.extend(notes)
        if el.clear_aperture is not None:
            rows.append({"element": el.name, "uid": el.uid, "z_mm": z, **state.limit(el.clear_aperture)})
        if sample_drifts and isinstance(state, Population) and el.kind == "space":
            minimum = minimum_spot_in_drift(state.rays, length, index)
            if minimum:
                state.drifts.append(
                    {
                        "element": el.name,
                        "uid": el.uid,
                        "start_mm": z,
                        "end_mm": z + length,
                        "surviving_power_fraction": state.mass(),
                        **minimum,
                        **({"z_mm": z + minimum["depth_mm"]} if minimum["unique"] else {}),
                    }
                )
                row = (
                    rows[-1]
                    if el.clear_aperture is not None
                    else {"element": el.name, "uid": el.uid, "z_mm": z}
                )
                row.update(
                    min_rms_radius_mm=minimum["rms_radius_mm"],
                    min_rms_z_mm=z + minimum["depth_mm"] if minimum["unique"] else None,
                    **({} if minimum["unique"] else {"decision": "该段半径恒定"}),
                )
                if el.clear_aperture is None:
                    rows.append(row)
        # Zero-thickness lenses and mirrors use air-equivalent ABCD. Material sections use nθ.
        n = index if el.kind in {"space", "grin"} else 1.0
        max_slope = max(max_slope, state.slope(n))
        state.transfer(matrices)
        max_slope = max(max_slope, state.slope(n))
        z += length
    max_slope = max(max_slope, state.slope(exit_n))
    quality = "conditional" if quality == "documented" and model_notes else quality
    discrepancy = 1 - 1 / math.hypot(1, max_slope)
    if discrepancy > 0.01:
        model_notes.append(
            "参考面上的斜率或其保守界限使 sinθ 与 tanθ 的相对差超过 1%。请用实测或高角度模型核对近轴传输。"
        )
        quality = "limited"
    warnings = list(dict.fromkeys([*warnings, *model_notes]))
    return {"z_mm": z, "quality": quality, "warnings": warnings, "rows": rows,
            "max_reference_slope": max_slope, "paraxial_sine_slope_difference": discrepancy}


def weighted_rays_at_plane(layout, beam_uid, plane_uid, source):
    """Transport the launch ensemble once to this station without rasterization or resampling."""
    route = forward_path(layout, beam_uid, plane_uid, source_wavelength=source.wavelength_nm)
    index = route.elements[-1][1]
    state = Population(source)
    with np.errstate(over="raise", invalid="raise", divide="raise"):
        transport = _transport(route, source, state, index, sample_drifts=False)
    return state, index, transport


def _trace(args, archive):
    source = args.input_bundle or args.input_support
    if source is None:
        raise MissingStudyInput(
            "请选择一份入射相空间边界或已知权重的光线。芯径和数值孔径可给出几何边界；计算分布需提供联合采样或注明填充假设。"
        )
    route = forward_path(args.layout, args.beam_uid, args.plane_uid, source_wavelength=source.wavelength_nm)
    state = Population(source) if args.input_bundle is not None else Boundary(source)
    plane_name = next(
        (p.name for p in args.layout.observation_planes if p.uid == args.plane_uid),
        route.elements[-1][0].name + "出射面",
    )
    exit_n = route.elements[-1][1] if args.plane_uid is not None else 1.0
    transport = _transport(route, source, state, exit_n, sample_drifts=True)
    if args.receiver_na is not None and args.receiver_na > exit_n:
        raise ValueError(f"接收器数值孔径不能超过该参考面的折射率 {exit_n:g}。请核对本地介质和角度口径。")
    quality, warnings = transport["quality"], transport["warnings"]
    frame = {
        "plane": plane_name,
        "basis": source.basis,
        "medium_n": exit_n,
        "wavelength_nm": route.wavelength_nm,
    }
    result = state.finish(args, frame, quality, warnings, archive)
    if args.launched_power_w is not None:
        for fraction, power in (("throughput", "transmitted"), ("accepted_fraction", "accepted")):
            if fraction in result:
                result[power + "_w"] = args.launched_power_w * result[fraction]
        for fraction, power in (("throughput_bounds", "transmitted"), ("accepted_bounds", "accepted")):
            if fraction in result:
                result[power + "_w_bounds"] = [args.launched_power_w * x for x in result[fraction]]
    assumptions = [
        "在对齐 T/S 的展开坐标中逐元件用工程近轴矩阵传输位置及折射率乘横向斜率。清孔位于每个元件的入口。"
        "参考面之间的黑箱矩阵不提供元件内部孔径、像差或杂散光路径。",
        "几何传播忽略衍射、干涉、表面反射损耗、增益与吸收。零几何斑径不能当作实际衍射焦斑。"
        "不同折射率的均匀段按无损平界面衔接，出射面无显式取样站时用空气等效参考面。",
        "近轴指示量为所检查参考面上 |tanθ−sinθ|/|tanθ| 的最大值或外界；该值不构成透镜像差或预测误差界。",
    ]
    if isinstance(state, Population):
        assumptions += [
            "每条光线的功率权重相对于最初入射总功率。探测视场仅限绘图与积分，视场外的幸存光线仍可传入下一段。"
            "二阶矩和逐均匀段的最小均方根半径使用全部幸存光线，直径按各轴 4σ。每段范围仅针对当段所透过的光线。",
            "分箱内信号为样本权重之和。两组独立取样结果的绝对差用于观察离散化敏感性；增加样本和改变分箱可复核局部结构。"
            if state.split is not None
            else "此来源未标记独立重复样本，单份加权光线不能给出抽样置信区间。",
        ]
        if not state.mass() and source.rays:
            assumptions.append(
                "给定离散光线没有穿过所有孔径。要判断连续实际光束是否全挡，可检查几何边界或增加独立取样。"
            )
    else:
        assumptions.append(
            "仅传播位置与方向的外接区间。各孔面按保守包含或分离判定；边界与孔重叠时透过份额可能从零到原上界。"
            "范围未定义联合权重，不能推出二维强度或典型效率。"
        )
    if args.receiver_opening or args.receiver_na is not None:
        assumptions.append(
            "接收份额同时检查观察面开口及同轴接收角，对原始入射总功率归一化。探测条件不截断传往下一段的光线。"
            "几何接收功率不含菲涅耳反射、材料吸收或相干模式耦合。"
        )
    return {
        **transport,
        "status": "partial" if quality == "limited" else "completed",
        "assumptions": assumptions,
        "source_z_mm": route.start_mm,
        "wavelength_nm": route.wavelength_nm,
        "beam_uid": args.beam_uid,
        "plane": plane_name,
        "result_kind": "weighted" if isinstance(state, Population) else "support",
        **result,
    }


def propagate_ray_bundle(args, *, archive=True):
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            return _trace(args, archive)
    except (FloatingPointError, OverflowError) as exc:
        raise ValueError("光线坐标、元件传输或探测分箱超出数值范围。请核对物理尺度、角度与单位。") from exc


_FRACTION = Port(
    quantity="optics.geometric-aperture-throughput", label="几何孔径通过功率 / 原入射", unit="1", scale=1
)
_ACCEPTED = Port(
    quantity="optics.geometric-receiver-throughput", label="几何接收功率 / 原入射", unit="1", scale=1
)
_TRANSMITTED_POWER = at_wavelength(OUTGOING_POWER.model_copy(update={"label": "到达观察面的模型透过功率"}))
_RECEIVED_POWER = at_wavelength(OUTGOING_POWER.model_copy(update={"label": "满足接收器条件的模型接收功率"}))
RAY_STUDY = Study(
    "propagate_ray_bundle",
    "加权光线传播",
    "1",
    "用已存位置与方向范围给出一阶几何外界，或沿工程的单程、入射支路传输带权非相干光线，计算光阑截取、聚焦、探测器分布及可续传出射态。",
    TraceRays,
    propagate_ray_bundle,
    preview_compute=lambda args: propagate_ray_bundle(args, archive=False),
    metrics={
        "z_mm": "观察位置 / mm",
        "throughput": "累计几何通过功率 / 原始入射",
        "window_power": "视场功率 / 原始入射",
        "throughput_min": "几何通过份额下界",
        "throughput_max": "几何通过份额上界",
        "surviving_rays": "幸存样本数",
        "accepted_fraction": "开口和接收角共同接受 / 原始入射",
        "accepted_min": "几何接收份额下界",
        "accepted_max": "几何接收份额上界",
        "transmitted_w": "到达观察面的模型透过功率 / W",
        "accepted_w": "满足接收器条件的模型接收功率 / W",
        "transmitted_w_bounds": "模型透过功率范围 / W",
        "accepted_w_bounds": "模型接收功率范围 / W",
        "x_min_mm": "T 下界 / mm",
        "x_max_mm": "T 上界 / mm",
        "y_min_mm": "S 下界 / mm",
        "y_max_mm": "S 上界 / mm",
        "center_t_mm": "强度中心 T / mm",
        "center_s_mm": "强度中心 S / mm",
        "diameter_4sigma_t_mm": "T 轴 4σ 全宽 / mm",
        "diameter_4sigma_s_mm": "S 轴 4σ 全宽 / mm",
        "replicate_throughput_difference": "两组采样通过份额绝对差",
        "replicate_accepted_difference": "两组采样接收份额绝对差",
        "replicate_map_l1": "两组探测分箱信号差 L¹ / 原始入射",
        "paraxial_sine_slope_difference": "近轴斜率与正弦的相对差（参考面）",
    },
    table=(
        "rows",
        {
            "element": "元件",
            "z_mm": "入射面 / mm",
            "before": "孔前份额",
            "after": "孔后份额",
            "blocked": "本孔截去",
            "decision": "说明",
            "throughput_min": "剩余下界",
            "throughput_max": "剩余上界",
            "min_rms_radius_mm": "段内最小均方根半径 / mm",
            "min_rms_z_mm": "最小处坐标 / mm",
        },
    ),
    raster=RasterView(
        x="grid.x_edges_mm",
        y="grid.y_edges_mm",
        z="grid.density",
        label="相对入射功率密度 / mm⁻²",
        x_label="切向 T / mm",
        y_label="弧矢 S / mm",
        equal_scale=True,
        power_db=True,
    ),
    input_ports={
        "layout": LAYOUT,
        "input_bundle": RAY_BUNDLE,
        "input_support": RAY_SUPPORT,
        "launched_power_w": OUTGOING_POWER.model_copy(
            update={"label": "原始入射功率", "registration_label": "此功率与光线权重的最初归一化参考面及工况"}
        ),
    },
    output_ports={
        "exit_bundle": ray_output(RAY_BUNDLE, "exit_bundle"),
        "exit_support": ray_output(RAY_SUPPORT, "exit_support"),
        "intensity_map": INTENSITY_MAP.model_copy(update={"available_on_partial": True}),
        "throughput": _FRACTION,
        "throughput_bounds": _FRACTION.model_copy(update={"shape": "interval"}),
        "accepted_fraction": _ACCEPTED,
        "accepted_bounds": _ACCEPTED.model_copy(update={"shape": "interval"}),
        "accepted_w": _RECEIVED_POWER,
        "transmitted_w": _TRANSMITTED_POWER,
        "accepted_w_bounds": _RECEIVED_POWER.model_copy(update={"shape": "interval"}),
        "transmitted_w_bounds": _TRANSMITTED_POWER.model_copy(update={"shape": "interval"}),
        **{
            f"diameter_4sigma_{axis}_mm": at_wavelength(Port(
                quantity=f"optics.geometric-ray.d4sigma-{axis}",
                label=f"观察面入射光线 {axis.upper()} 轴 4σ 全宽", unit="mm", scale=.001,
            ))
            for axis in ("t", "s")
        },
    },
    live_layout="layout",
    ignores_diagram_positions=True,
    layout_prefix_inputs={"path": "beam_uid", "plane": "plane_uid"},
    condition_matches=tuple(
        ConditionMatch(anchor=source, other="launched_power_w", keys={VACUUM_NM: "真空波长"})
        for source in ("input_bundle", "input_support")
    ),
    keywords=(
        "étendue",
        "etendue",
        "incoherent",
        "ray tracing",
        "multimode",
        "numerical aperture",
        "泵浦",
        "非相干",
        "多模",
        "相空间",
        "截光",
        "聚焦",
        "芯径",
        "数值孔径",
    ),
)

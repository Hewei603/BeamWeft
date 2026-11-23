"""Compare independent, spatially registered predictions or measurements on the detector grid."""

import math

import numpy as np
from pydantic import Field, model_validator

from ..physics.raster_overlap import detector_integrals
from ..study_api import StrictInput, Study
from .intensity_map import INTENSITY_MAP, IntensityMap
from .transverse_registration import TRANSVERSE_REGISTRATION, TransverseRegistration


class MapComparison(StrictInput):
    reference: IntensityMap = Field(title="参考图", description="预测或独立基准，使用各单元积分信号以及物理边界。")
    observed: IntensityMap = Field(title="观测图", description="保留相机原件坐标或另一已知的检测网格。")
    registration: TransverseRegistration | None = Field(default=None, exclude_if=lambda v:v is None,
        title="已知或已保存的坐标配准", description="可连接标记点标定结果。也可以分别填写平移、转角和手性。")
    offset_x_mm: float | None = Field(default=None, exclude_if=lambda v:v is None, title="观测原点在参考横轴的位置 / mm")
    offset_y_mm: float | None = Field(default=None, exclude_if=lambda v:v is None, title="观测原点在参考纵轴的位置 / mm")
    rotation_deg: float | None = Field(default=None, exclude_if=lambda v:v is None, title="观测横轴相对参考横轴的转角 / °", description="正角从参考 +x 朝 +y。")
    flip_y: bool | None = Field(default=None, exclude_if=lambda v:v is None, title="旋转前反向观测纵轴", description="用实际坐标系的手性决定。图像的显示方向只影响绘图。")
    registration_basis: str = Field(
        min_length=1,
        max_length=500,
        pattern=r"\S",
        title="同一参考面与坐标配准依据",
        description="说明相机物方尺度、坐标如何映射、工况和面的位置。平移量是观测坐标 (0,0) 在参考图中的位置。",
    )
    observed_units_per_reference_unit: float | None = Field(
        default=None,
        gt=0,
        title="每单位参考信号对应的观测信号",
        description="可选的独立绝对幅值标定。例如按相机响应、曝光时间及入射功率将模型结果换算为原件读数。",
    )
    signal_calibration_basis: str | None = Field(
        default=None, min_length=1, max_length=1000, pattern=r"\S", title="信号换算依据"
    )

    @model_validator(mode="after")
    def calibration(self):
        for key in ("offset_x_mm","offset_y_mm","rotation_deg","flip_y"):
            value = getattr(self,key)
            if self.registration is None and value is None:
                raise ValueError("请选择已存坐标配准，或完整填写观测原点、转角和纵轴朝向。")
            if self.registration is not None and value is not None and value != getattr(self.registration,key):
                raise ValueError("单独填写的坐标与所选标定不同。请清除冲突的值，或采用另一份配准。")
        if (self.observed_units_per_reference_unit is None) != (self.signal_calibration_basis is None):
            raise ValueError("比较共有区域的绝对信号，需要同时填写独立换算系数及其标定依据。")
        return self


def _masked(values, valid):
    return np.where(valid, values.astype(object), None).tolist()


def _compare(args):
    reference, observed = args.reference, args.observed
    frame = args.registration or TransverseRegistration(offset_x_mm=args.offset_x_mm, offset_y_mm=args.offset_y_mm,
        rotation_deg=args.rotation_deg, flip_y=args.flip_y, basis=args.registration_basis)
    rx, ry = np.array(reference.x_edges_mm), np.array(reference.y_edges_mm)
    ox, oy = np.array(observed.x_edges_mm), np.array(observed.y_edges_mm)
    actual = np.array(observed.values)
    expected, valid = detector_integrals(
        rx, ry, np.array(reference.values), ox, oy, frame.offset_x_mm, frame.offset_y_mm, frame.rotation_deg, frame.flip_y
    )
    cell_area = np.outer(np.diff(oy), np.diff(ox))
    area = float(cell_area[valid].sum())
    measured_sum, predicted_sum = float(actual[valid].sum()), float(expected[valid].sum())
    if measured_sum <= 0 or predicted_sum <= 0:
        raise ValueError("共有区域的两份积分信号都须为正才能比较分布形状。请核对背景、原点、视场与曝光。")
    # Normalize only supported cells. Noise outside the common window should not enter any arithmetic.
    seen, model = np.zeros_like(actual), np.zeros_like(expected)
    seen[valid] = actual[valid] / measured_sum / cell_area[valid]
    model[valid] = expected[valid] / predicted_sum / cell_area[valid]
    delta = seen - model
    warnings = [
        f"{label}：{note}" for label, item in (("参考图", reference), ("观测图", observed)) for note in item.warnings
    ]
    if frame.fit_evidence:
        warnings.extend(frame.fit_evidence.comparison_conditions())
    warnings += [
        f"{label}尚无单独的来源质量评定，请结合归档的采集或计算依据解释比较结果。"
        for label, item in (("参考图", reference), ("观测图", observed))
        if item.quality == "unassessed"
    ]
    limitations = [
        f"{label}的来源记录标注了影响强度检验的质量限制，请核对来源说明。"
        for label, item in (("参考图", reference), ("观测图", observed))
        if item.quality == "limited"
    ]
    if (
        reference.wavelength_nm
        and observed.wavelength_nm
        and not math.isclose(reference.wavelength_nm, observed.wavelength_nm, rel_tol=1e-10)
    ):
        limitations.append("两图注明的真空波长不同，请核对这项比较的实验目的。")
    if not valid.all():
        warnings.append("视场边界只比较完整落在参考图内的观测单元。其余位置留空。")
    cells = int(valid.sum())
    if cells == 1:
        limitations.append("共有区域只有一个单元，两图各按其积分归一化后无法检验空间形状。")
    if np.any(actual[valid] < 0) or np.any(expected[valid] < 0):
        warnings.append("共有区域含负的净信号。残差保留这些值，归一化依据带符号的积分。")
    x_centers = ox[:-1] + 0.5 * np.diff(ox)
    y_centers = oy[:-1] + 0.5 * np.diff(oy)

    def centroid(masses, total):
        signed = np.where(valid, masses, 0.0) / total
        return {"x_mm": float(signed.sum(axis=0) @ x_centers), "y_mm": float(signed.sum(axis=1) @ y_centers)}

    predicted_center, observed_center = centroid(expected, predicted_sum), centroid(actual, measured_sum)
    if any(
        not (ox[0] <= point["x_mm"] <= ox[-1] and oy[0] <= point["y_mm"] <= oy[-1])
        for point in (predicted_center, observed_center)
    ):
        limitations.append("净信号的一阶矩落在图像范围外，背景扣除及正负信号抵消使质心解释受到限制。")
    elif any(
        abs(values[valid]).sum() * 1e-6 > abs(total)
        for values, total in ((expected, predicted_sum), (actual, measured_sum))
    ):
        limitations.append("共有区域的净信号低于正负读数绝对值总和的百万分之一，质心对背景误差极为敏感。")
    result = {
        "status": "partial" if limitations else "completed",
        "warnings": [*limitations, *warnings],
        "limitations": limitations,
        "source_quality": {"observed": observed.quality, "reference": reference.quality},
        "coverage_state": "full_observation" if valid.all() else "partial_observation",
        "cells_compared": cells,
        "overlap_area_mm2": area,
        "coverage_observed": min(1.0, area / float(cell_area.sum())),
        "coverage_reference": min(1.0, area / float((rx[-1] - rx[0]) * (ry[-1] - ry[0]))),
        "relative_l1": float(np.sum(np.abs(delta) * cell_area)),
        "rmse_density_mm2": float(np.sqrt(np.sum(delta**2 * cell_area) / area)),
        "centroids": {"reference": predicted_center, "observed": observed_center, "frame": "observed"},
        "centroid_dx_mm": observed_center["x_mm"] - predicted_center["x_mm"],
        "centroid_dy_mm": observed_center["y_mm"] - predicted_center["y_mm"],
        "common_signal": {
            "reference": predicted_sum,
            "reference_unit": reference.signal_unit,
            "observed": measured_sum,
            "observed_unit": observed.signal_unit,
        },
        "grid": {
            "x_edges_mm": observed.x_edges_mm,
            "y_edges_mm": observed.y_edges_mm,
            "x_label": observed.x_label,
            "y_label": observed.y_label,
            "y_down": observed.y_down,
            "reference": _masked(model, valid),
            "observed": _masked(seen, valid),
            "residual": _masked(delta, valid),
        },
        "registration": {
            **frame.model_dump(),
            "basis": args.registration_basis,
            **({"calibration_basis": frame.basis} if args.registration else {}),
        },
        "assumptions": [
            "两份图像表示实验者配准到同一平面的线性积分信号。使用给定的平移、转角和镜像，保留束斑的真实偏移。",
            "参考图各格内采用平均面密度，按像素交叠面积向观测格积分。分箱内的亚格纹理、探测器扩散和额外的光学模糊未建模。",
            "两图各按共有完整观测格的净信号归一化到 1。残差为观测减参考的形状密度 / mm²。L¹ 按面积积分，均方根按共有面积加权。",
            "两图信号都非负时，分布相同的 L¹ 为 0，完全分离时为 2。保留负的净信号时，L¹ 可能超过 2。",
            "两组质心均按共有观测单元的中心与带符号积分计算。位移为观测减参考，以观测图两轴表示。未覆盖部分可能改变全光束质心。",
        ],
    }
    if args.observed_units_per_reference_unit is not None:
        converted = predicted_sum * args.observed_units_per_reference_unit
        if not 0 < converted < math.inf:
            raise ValueError("标定后的参考信号超出可表示范围，请核对信号单位和标定系数。")
        result["calibrated_signal_ratio"] = measured_sum / converted
        result["common_signal"].update(
            predicted_observed_units=converted, calibration_basis=args.signal_calibration_basis
        )
    return result


def compare_intensity_maps(args):
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            return _compare(args)
    except (ArithmeticError, OverflowError) as exc:
        raise ValueError("坐标、信号或像素面积超出数值范围，请核对地图单位、积分信号及物方尺度。") from exc


MAP_COMPARISON = Study(
    "compare_intensity_maps",
    "横向光强图配准比较",
    "1",
    "在已知相对位置、方向和物方尺度的两张图上，积分参考预测到观测像素，检查共有视场的形状与差异。"
    "可比较相机、光传播仿真、干涉端口或其他仪器，已有独立信号标定时也可核对总信号。",
    MapComparison,
    compare_intensity_maps,
    metrics={
        "relative_l1": "归一化形状残差 L¹",
        "rmse_density_mm2": "面积加权残差 RMS / mm⁻²",
        "coverage_observed": "已比较 / 观测图面积",
        "coverage_reference": "已比较 / 参考图面积",
        "overlap_area_mm2": "已比较面积 / mm²",
        "cells_compared": "参与比较的观测单元数",
        "centroids.reference.x_mm": "参考质心（观测横轴）/ mm",
        "centroids.reference.y_mm": "参考质心（观测纵轴）/ mm",
        "centroids.observed.x_mm": "观测质心（观测横轴）/ mm",
        "centroids.observed.y_mm": "观测质心（观测纵轴）/ mm",
        "centroid_dx_mm": "质心位移 · 观测减参考（横）/ mm",
        "centroid_dy_mm": "质心位移 · 观测减参考（纵）/ mm",
        "calibrated_signal_ratio": "标定后的积分信号比 · 观测 / 参考",
    },
    input_ports={"reference": INTENSITY_MAP, "observed": INTENSITY_MAP, "registration": TRANSVERSE_REGISTRATION},
    registration_inputs={"reference": "registration_basis", "observed": "registration_basis", "registration":"registration_basis"},
    view="measurement.registered-map-comparison.v1",
    keywords=(
        "相机",
        "光斑",
        "探测器",
        "测量",
        "实验",
        "差值",
        "残差图",
        "image",
        "detector",
        "residual",
        "registration",
        "intensity map",
    ),
)

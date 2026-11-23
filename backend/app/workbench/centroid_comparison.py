"""Register a detector-window centroid to the predicted coordinates of a physical station."""

import math

from pydantic import Field

from ..study_api import StrictInput, Study
from .centroid_state import DETECTOR_CENTROID, STATION_CENTROID, DetectorCentroid, StationCentroid
from .transverse_registration import TRANSVERSE_REGISTRATION, TransverseRegistration


class CenterComparison(StrictInput):
    observed: DetectorCentroid = Field(title="探测器坐标中的光强质心")
    reference: StationCentroid = Field(title="工程观察面的预测束心")
    registration: TransverseRegistration = Field(title="观测坐标到工程 T/S 的标定")


def compare_centers(args):
    obs, ref, reg = args.observed, args.reference, args.registration
    t, s = reg.map_point(obs.x_mm, obs.y_mm)
    observed = {"tangential": t, "sagittal": s}
    reference = {"tangential": ref.t_mm, "sagittal": ref.s_mm}
    warnings = list(obs.warnings)
    conditions = []
    blockers = []
    if obs.quality == "unassessed":
        conditions.append("观测值尚无采集质量评定。")
    elif obs.quality == "conditional" and not obs.warnings:
        conditions.append("观测记录使用了暂定的采集条件，请核对采集依据。")
    if obs.quality == "limited":
        warnings.append("观测记录含影响强度质心的质量限制，偏差仅供诊断。")
    if ref.launch_basis in ("ideal_on_axis", "reference_axis"):
        conditions.append("此版本采用理想轴上的束心作为模型预测。")
    if obs.wavelength_nm is None or ref.wavelength_nm is None:
        conditions.append("实测或模型未记录波长，差值以两者属于同一光束与工况为条件。")
    elif not math.isclose(obs.wavelength_nm, ref.wavelength_nm, rel_tol=1e-9):
        blockers.append(
            f"观测波长 {obs.wavelength_nm:g} nm 与模型波长 {ref.wavelength_nm:g} nm 不一致，请核对光束和工况。"
        )
    if obs.window_coverage == "unverified":
        conditions.append("尚未核实像素窗包含完整光斑。测量质心属于选区，可扩大选区或独立核对截光。")
    elif obs.window_coverage == "truncated":
        blockers.append("已知探测窗截断光斑。请用相同窗口的强度图比较，或重新采集覆盖整束的读数。")
    if ref.launch_basis == "unrecorded":
        blockers.append("原计算没有记录所用的束心坐标依据。")
    evidence = reg.fit_evidence
    if evidence is not None:
        conditions.extend(evidence.comparison_conditions())
    quality = obs.quality
    if quality == "unassessed" or (conditions and quality == "usable"):
        quality = "conditional"
    residuals, notes = {}, dict(ref.notes)
    for axis, value in reference.items():
        if blockers:
            notes[axis] = " ".join(dict.fromkeys(filter(None, [notes.get(axis), *blockers])))
        elif value is None:
            notes.setdefault(axis, "该方向未保存可比较的模型束心。")
        elif axis not in notes:
            difference = observed[axis] - value
            if not math.isfinite(difference):
                raise ValueError("观测与参考的位置差超出数值范围，请核对共同原点和单位。")
            residuals[axis] = difference
    distance = math.hypot(*residuals.values()) if len(residuals) == 2 else None
    if distance is not None and not math.isfinite(distance):
        raise ValueError("二维位置差超出数值范围，请核对坐标单位。")
    return {
        "status": "completed" if len(residuals) == 2 else "partial" if residuals else "needs_input",
        "comparison_quality": quality if residuals else "unavailable",
        "observed_mm": observed,
        "reference_mm": reference,
        "residual_mm": residuals,
        "distance_mm": distance,
        "identity": {"plane_uid": ref.plane_uid, "beam_uid": ref.beam_uid, "z_mm": ref.z_mm},
        "local_mm": {"x": obs.x_mm, "y": obs.y_mm},
        "window_coverage": obs.window_coverage,
        "registration": reg.model_dump(mode="json"),
        "observed_basis": obs.basis,
        "model_basis": ref.launch_basis,
        "observed_wavelength_nm": obs.wavelength_nm,
        "reference_wavelength_nm": ref.wavelength_nm,
        "warnings": [*warnings, *blockers, *conditions],
        "axis_notes": notes,
        "assumptions": [
            "差值为配准后的观测质心减去模型束心，使用工程切向 T、弧矢 S 与毫米。"
            "坐标标定适用于同一物方参考面、仪器安装和成像倍率。"
        ],
    }


CENTER_COMPARISON = Study(
    "compare_transverse_centers",
    "观察面光斑位置对照",
    "1",
    "将原件中按实际样本计算的质心映射到冻结工程观察面的坐标。按方向比较可用读数，并记录采集窗口、质量和配准依据。",
    CenterComparison,
    compare_centers,
    input_ports={"observed": DETECTOR_CENTROID, "reference": STATION_CENTROID, "registration": TRANSVERSE_REGISTRATION},
    metrics={
        "observed_mm.tangential": "配准后观测 T / mm",
        "observed_mm.sagittal": "配准后观测 S / mm",
        "reference_mm.tangential": "预测 T / mm",
        "reference_mm.sagittal": "预测 S / mm",
        "residual_mm.tangential": "观测减模型 T / mm",
        "residual_mm.sagittal": "观测减模型 S / mm",
        "distance_mm": "二维位置差 / mm",
    },
    view="measurement.transverse-center-comparison.v1",
    keywords=(
        "光斑",
        "质心",
        "中心",
        "束心",
        "相机",
        "位置差",
        "配准",
        "对准",
        "alignment",
        "centroid",
        "camera",
        "offset",
    ),
)

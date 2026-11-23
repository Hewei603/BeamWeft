"""Fit and exchange a measured-to-reference rigid transform for one transverse plane."""

import math
from typing import Annotated, Literal

import numpy as np
from pydantic import Field, model_validator

from ..physics.measurement_data import LENGTH_UNITS
from ..physics.transverse_frames import transverse_matrix
from ..study_api import Port, StrictInput, Study


TRANSVERSE_REGISTRATION = Port(
    quantity="optics.transverse-rigid-registration.v1",
    shape="state",
    unit="observed-to-reference.mm.v1",
    scale=1,
    label="观测坐标到参考坐标的平面刚体配准",
    registration_label="说明本次观测面、物方尺度和坐标基准与标定的对应关系",
)
POSITION = Port(
    quantity="optics.transverse-position",
    label="横向位置",
    shape="series",
    unit="mm",
    scale=0.001,
    alternate_units=LENGTH_UNITS,
)
Column = Annotated[list[float], Field(min_length=2, max_length=10000)]
Basis = Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]


class FitEvidence(StrictInput):
    fit_count: int = Field(ge=2)
    fit_rms_mm: float = Field(ge=0)
    fit_max_mm: float = Field(ge=0)
    check_count: int = Field(ge=0)
    check_new_location_count: int | None = Field(default=None, ge=0, exclude_if=lambda value: value is None)
    check_rms_mm: float | None = Field(default=None, ge=0)
    check_max_mm: float | None = Field(default=None, ge=0)
    max_error_mm: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def check_location_count(self):
        if self.check_new_location_count is not None and self.check_new_location_count > self.check_count:
            raise ValueError("新检验位置的数量不能超过预留标记点的数量。")
        return self

    def comparison_conditions(self):
        notes = []
        if not self.check_count:
            notes.append("标定没有预留检验点。拟合残差只描述参与拟合的标记。")
        elif self.check_new_location_count == 0:
            notes.append("预留点只在拟合标记原位复测，可以检查重复性。其他位置的配准精度仍待检验。")
        elif self.check_new_location_count is None:
            notes.append("这份标定未记录预留位置是否独立于拟合标记。")
        if self.max_error_mm is None:
            notes.append("此次标定未设位置误差限值。请用测得的标记偏差和本次实验的精度需求判断适用性。")
        return notes


class TransverseRegistration(StrictInput):
    offset_x_mm: float = Field(title="观测原点在参考横轴的位置 / mm")
    offset_y_mm: float = Field(title="观测原点在参考纵轴的位置 / mm")
    rotation_deg: float = Field(title="观测横轴相对参考横轴的转角 / °")
    flip_y: bool = Field(title="旋转前反向观测纵轴")
    basis: Basis = Field(title="标定面、坐标方向与依据")
    fit_evidence: FitEvidence | None = Field(default=None, exclude_if=lambda v: v is None)

    def map_point(self, x_mm, y_mm):
        with np.errstate(over="raise", invalid="raise"):
            try:
                position = transverse_matrix(self.rotation_deg, self.flip_y) @ [x_mm, y_mm]
                x, y = position + [self.offset_x_mm, self.offset_y_mm]
                return float(x), float(y)
            except FloatingPointError as exc:
                raise ValueError("配准后的坐标超出数值范围，请核对原点和长度单位。") from exc


class RegistrationFit(StrictInput):
    observed_x_mm: Column = Field(title="标记点的观测横坐标 / mm")
    observed_y_mm: Column = Field(title="标记点的观测纵坐标 / mm")
    reference_x_mm: Column = Field(title="同一标记点的参考横坐标 / mm")
    reference_y_mm: Column = Field(title="同一标记点的参考纵坐标 / mm")
    handedness: Literal["infer", "preserve", "reverse"] = Field(
        default="infer",
        title="两组坐标的手性",
        json_schema_extra={"x-enum-labels": {"infer": "由标记点辨别", "preserve": "相同", "reverse": "相反"}},
    )
    check_rows: list[Annotated[int, Field(ge=1)]] = Field(
        default_factory=list,
        max_length=10000,
        title="仅用于检验的点号",
        description="按输入的配对点顺序从 1 计数。这些点不参与旋转、平移或手性的选择。",
    )
    max_error_mm: float | None = Field(
        default=None,
        gt=0,
        title="允许的最大位置差 / mm",
        description="可选。任一拟合或检验标记超出限值时，保留诊断并停止发布标定。",
    )
    basis: Basis = Field(
        title="标记来源与坐标依据",
        description="说明同一物方面、标记如何一一对应、观测与参考的零点和方向。两组输入都使用已标定的毫米，标定点应独立于待检验的光斑。",
    )

    @model_validator(mode="after")
    def pairs(self):
        n = len(self.observed_x_mm)
        if not all(len(getattr(self, key)) == n for key in ("observed_y_mm", "reference_x_mm", "reference_y_mm")):
            raise ValueError("四列坐标需要相同数量的标记，按同一点的顺序配对。")
        if len(set(self.check_rows)) != len(self.check_rows) or any(i > n for i in self.check_rows):
            raise ValueError("检验点号需要是所给配对点中互不重复的编号，从 1 起算。")
        if n - len(self.check_rows) < 2:
            raise ValueError("预留检验点后至少还需两个不同位置的拟合标记。")
        return self


def _candidate(flip, observed, reference, train):
    # Center using a local origin to preserve small differences at large lab coordinates.
    obs = observed[train] - observed[train][0]
    ref = reference[train] - reference[train][0]
    obs_mean, ref_mean = obs.mean(0), ref.mean(0)
    x, y = obs - obs_mean, ref - ref_mean
    sx, sy = np.max(np.abs(x)), np.max(np.abs(y))
    if sx == 0 or sy == 0:
        return None
    x = x / sx
    if flip:
        x[:, 1] *= -1
    cross = x.T @ (y / sy)
    cosine, sine = cross[0, 0] + cross[1, 1], cross[0, 1] - cross[1, 0]
    rotation_determined = math.hypot(cosine, sine) > 32 * np.finfo(float).eps * np.linalg.norm(x) * np.linalg.norm(
        y / sy
    )
    angle = math.degrees(math.atan2(sine, cosine)) if rotation_determined else 0.0
    matrix = transverse_matrix(angle, flip)
    offset = (reference[train][0] + ref_mean) - matrix @ (observed[train][0] + obs_mean)
    predicted = observed @ matrix.T + offset
    errors = np.hypot(*(reference - predicted).T)
    return (
        {"offset_x_mm": float(offset[0]), "offset_y_mm": float(offset[1]), "rotation_deg": angle, "flip_y": flip},
        predicted,
        errors,
        rotation_determined,
    )


def _rms(values):
    maximum = float(max(values))
    return maximum * math.sqrt(float(np.mean((values / maximum) ** 2))) if maximum else 0.0


def _fit(args):
    obs = np.column_stack((args.observed_x_mm, args.observed_y_mm))
    ref = np.column_stack((args.reference_x_mm, args.reference_y_mm))
    train = np.ones(len(obs), dtype=bool)
    train[np.asarray(args.check_rows, dtype=int) - 1] = False
    flips = [False, True] if args.handedness == "infer" else [args.handedness == "reverse"]
    candidates = [v for flip in flips if (v := _candidate(flip, obs, ref, train)) is not None]
    if not candidates:
        return {
            "status": "needs_input",
            "warnings": ["这些对应点不能确定旋转。请核对点序、坐标手性并给出不同位置的标记。"],
        }
    candidates.sort(key=lambda v: _rms(v[2][train]))
    transform, predicted, errors, rotation_determined = candidates[0]
    if not rotation_determined:
        return {
            "status": "needs_input",
            "warnings": ["拟合标记未确定唯一转角。请核对点序、手性和物方标尺，或在新的位置补充标记。"],
        }
    rms, maximum = _rms(errors[train]), float(max(errors[train]))
    checks = errors[~train]
    training_locations = {tuple(point) for point in ref[train]}
    new_check_locations = {tuple(point) for point in ref[~train]} - training_locations
    evidence = FitEvidence(
        fit_count=int(train.sum()),
        fit_rms_mm=rms,
        fit_max_mm=maximum,
        check_count=len(checks),
        check_new_location_count=len(new_check_locations),
        check_rms_mm=_rms(checks) if len(checks) else None,
        check_max_mm=float(max(checks)) if len(checks) else None,
        max_error_mm=args.max_error_mm,
    )
    alternatives = [{"flip_y": v[0]["flip_y"], "fit_rms_mm": _rms(v[2][train])} for v in candidates]
    report = {
        "status": "completed",
        "diagnostics": evidence.model_dump(),
        "handedness_comparison": alternatives,
        "warnings": [],
        "assumptions": [
            "按同一平面上已校准的长度单位等权拟合刚体变换。标定残差的统计范围为本次提供的标记点。",
            "检验位置按登记的参考坐标去重。同一标记的参考坐标若也经过重复测量，请结合标记身份判断是否检验了新的物理位置。",
        ],
    }
    if len(candidates) == 2:
        alternative = candidates[1][2][train]
        floor = 64 * np.finfo(float).eps * max(float(np.abs(obs[train]).max()), float(np.abs(ref[train]).max()))
        # RMS lower-bounds the maximum point error of every rotation with that handedness.
        # A loose tolerance may not establish parity even with a unique least-squares solution.
        if abs(_rms(alternative) - rms) <= floor or (
            args.max_error_mm and _rms(alternative) <= args.max_error_mm and maximum <= args.max_error_mm
        ):
            return {
                **report,
                "status": "needs_input",
                "warnings": [
                    "拟合标记未能可靠区分两种手性。请增加分布在平面内的非共线标记，或依据坐标安装方向指定手性。"
                ],
            }
    report["rows"] = [
        {
            "point": i + 1,
            "role": "拟合" if train[i] else "检验",
            "x_mm": float(point[0]),
            "y_mm": float(point[1]),
            "dx_mm": float(ref[i, 0] - point[0]),
            "dy_mm": float(ref[i, 1] - point[1]),
            "error_mm": float(errors[i]),
        }
        for i, point in enumerate(predicted)
    ]
    if args.max_error_mm is not None and max(errors) > args.max_error_mm:
        return {
            **report,
            "status": "partial",
            "candidate": transform,
            "warnings": ["标记的位置差超出设定限值。请核对配对、物方尺度及畸变，候选变换保留供查阅。"],
        }
    report["warnings"].extend(evidence.comparison_conditions())
    report["registration"] = TransverseRegistration(**transform, basis=args.basis, fit_evidence=evidence).model_dump()
    return report


def fit_registration(args):
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            return _fit(args)
    except (FloatingPointError, OverflowError) as exc:
        raise ValueError("标记坐标超出可计算的数值范围。请核对单位或选取较近的坐标原点。") from exc


_columns = ("observed_x_mm", "observed_y_mm", "reference_x_mm", "reference_y_mm")
REGISTRATION_FIT = Study(
    "fit_transverse_registration",
    "用标记点配准两个横向坐标系",
    "1",
    "按已标定的横向标记点拟合平移、旋转及可选镜像，保留逐点差值并独立检验预留点。结果可用于位置或强度图比较。",
    RegistrationFit,
    fit_registration,
    input_ports={k: POSITION.model_copy(update={"label": RegistrationFit.model_fields[k].title}) for k in _columns},
    paired_inputs=(_columns,),
    output_ports={"registration": TRANSVERSE_REGISTRATION},
    metrics={
        "diagnostics.fit_rms_mm": "拟合位置差 RMS / mm",
        "diagnostics.check_rms_mm": "预留点位置差 RMS / mm",
        "registration.rotation_deg": "转角 / °",
        "registration.offset_x_mm": "观测原点参考横坐标 / mm",
        "registration.offset_y_mm": "观测原点参考纵坐标 / mm",
    },
    table=(
        "rows",
        {
            "point": "点号",
            "role": "用途",
            "x_mm": "配准后 x / mm",
            "y_mm": "配准后 y / mm",
            "dx_mm": "参考减配准 x / mm",
            "dy_mm": "参考减配准 y / mm",
            "error_mm": "位置差 / mm",
        },
    ),
    keywords=("坐标", "配准", "标记点", "标定", "镜像", "fiducial", "registration", "landmarks", "calibration"),
)

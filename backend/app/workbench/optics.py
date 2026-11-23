"""General ordered paraxial systems. No experiment-specific topology selection.

Elements use reduced q=q_physical/n; each dielectric segment has flat faces.
Standing-wave systems are unfolded automatically; ring systems specify one full
round trip. Coplanar T/S planes only, no rotated astigmatism or nonparaxial solve.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Annotated, ClassVar, Literal

import numpy as np
from pydantic import AfterValidator, ConfigDict, Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from ..study_api import StrictInput
from ..solver_receipt import solver_receipt
from .beam_source import BeamSource, ParaxialBeamState, SourceAxis, axis_origin, compose_ray_matrices, missing_axis_inputs, ray_matrix_product, transfer_ray
from .component_origin import ComponentOrigin, model_wavelength_notes
from .device_laws import Prescription, device_catalog, scalar_scan_fields
from .profile_sampling import grin_depths, uniform_depths

from ..physics.materials import MaterialError, index_info, load_materials
from ..physics import beam as B

ELEMENT_CATALOG = json.loads(Path(__file__).with_name("element_catalog.json").read_text(encoding="utf-8"))
SCAN_PARAMETERS = {kind: model["parameters"] for kind, model in ELEMENT_CATALOG["models"].items()}
ZERO_THICKNESS_MODELS = frozenset({"lens", "mirror", "matrix"})


def _scan_parameter(name: str) -> str:
    if name not in ELEMENT_CATALOG["parameters"] and not (name.startswith("parameters.") and name[11:].isidentifier()):
        raise ValueError("请选择模型声明的可扫描参数。")
    return name


ScanParameter = Annotated[str, AfterValidator(_scan_parameter), Field(description="A catalog parameter or parameters.<numeric device setting>.",
    json_schema_extra={"anyOf":[{"enum":list(ELEMENT_CATALOG["parameters"])},{"pattern":r"^parameters\.[^\W\d]\w*$"}]})]


def describe_index(schema):
    # A JSON Schema property default is unconditional. The actual default depends on element kind.
    schema.pop("default", None)


class ClearAperture(StrictInput):
    """Hard amplitude stop projected onto the entrance plane normal to the nominal unfolded ray."""
    shape: Literal["ellipse", "rectangle"]
    width_mm: float = Field(gt=0, description="Full opening along the local first axis. For a circle this is the diameter.")
    height_mm: float | None = Field(default=None, gt=0, description="Full opening along the second axis. Omit to share width.")
    center_t_mm: float = 0
    center_s_mm: float = 0
    angle_deg: float = Field(default=0, description="Rotation of the first aperture axis from T toward S.")


class IndexReference(StrictInput):
    wavelength_nm: float | None = Field(default=None, gt=0, le=1e6)
    model: str | None = Field(default=None, min_length=1, max_length=100,
                             description="Canonical dispersion branch, or null for a wavelength-specific sample index.")


def resolve_medium_wavelength(data):
    """Resolve at the owning path, before either live preview or revision evaluation."""
    if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
        return data
    wavelength = data.get("wavelength_nm")
    updated = None
    for i, element in enumerate(data["elements"]):
        reference = element.get("index_reference") if isinstance(element, dict) else None
        if not isinstance(reference, dict):
            continue
        same_wavelength = reference.get("wavelength_nm") == wavelength
        ideal_air = reference.get("model") == "air"
        if same_wavelength and wavelength is None and element.get("n") is not None and not ideal_air:
            raise ValueError(f"请先填写工作波长，再登记「{element.get('name', '')}」的折射率。")
        if same_wavelength and not reference.get("model"):
            continue
        n = 1. if ideal_air and wavelength is None else None
        if reference.get("model") and wavelength is not None:
            try:
                info = index_info(reference["model"], float(wavelength))
            except (MaterialError, TypeError, ValueError) as exc:
                raise ValueError(f"「{element.get('name', '')}」的色散模型或工作波长无法识别。") from exc
            if info.in_range and math.isfinite(info.n):
                n = info.n
        if same_wavelength:
            supplied = element.get("n")
            if n is None:
                if reference.get("model") and supplied is not None:
                    raise ValueError(f"「{element.get('name', '')}」的材料模型在当前波长没有有效折射率。"
                                     "请清空模型值，或把独立测得的 n 登记为该波长的单点折射率。")
                continue
            if supplied is not None and not math.isclose(float(supplied), n, abs_tol=1e-9):
                condition = f"{float(wavelength):g} nm 处" if wavelength is not None else "当前"
                raise ValueError(f"「{element.get('name', '')}」的折射率与{condition}已选用的材料模型不一致。")
            if supplied == n:
                continue
        if updated is None:
            updated = list(data["elements"])
        updated[i] = {**element, "n":n, "index_reference":{**reference, "wavelength_nm":wavelength}}
    return {**data, "elements": updated} if updated is not None else data


def air_gap(name, length_mm):
    """A designed free-space interval uses the catalog's vacuum approximation for air."""
    return {"kind": "space", "name": name, "length_mm": length_mm, "index_reference": {"model": "air"}}


def material_interval(element, part):
    if element["kind"] != "space":
        return False
    if element.get("medium_role") or part.get("role") == "medium":
        return True
    reference = element.get("index_reference")
    if reference is not None and reference.get("model") == "air":
        return element.get("n") not in (None, 1)
    return reference is not None or element.get("n", 1) != 1


def axial_material_path(element, index):
    """Single traversal on the nominal axis of a homogeneous segment, using its real phase index."""
    if element.kind != "space":
        return {}
    length = element.length_mm
    opl = length * index if index is not None else None
    return {"length_mm": length, "reference_axis_opl_mm": 0. if length == 0 else opl}


def material_index_readings(owners):
    return [{"path_uid": uid, "element_uid": element.uid, "wavelength_nm": path.wavelength_nm,
             "model": element.index_reference.model, "n": element.n, **axial_material_path(element, element.n)}
            for uid, path in owners for element in path.elements if element.uid and element.index_reference is not None]


class OpticalElement(StrictInput):
    allow_incomplete: ClassVar[bool] = False
    uid: str = Field(default="", max_length=100, description="Stable project element identity; preserve on edit.")
    component_origin: ComponentOrigin | None = None
    index_reference: IndexReference | None = Field(default=None, exclude_if=lambda ref: ref is None,
        description="Spectral basis of a homogeneous segment index, or a single-wavelength calibration of a thick lens or GRIN center index.")
    medium_role: str | None = Field(default=None, min_length=1, max_length=100, description="Declared role of a material segment, for annotations. Does not change propagation.")
    name: str = Field(min_length=1, max_length=100)
    clear_aperture: ClearAperture | None = Field(default=None, exclude_if=lambda v: v is None,
        description="Projected clear opening at the entrance face, applied before component transfer in scalar-field and weighted-ray propagation.")
    kind: Literal["space", "lens", "mirror", "thick_lens", "grin", "matrix", "registered"]
    prescription: Prescription | None = Field(default=None, exclude_if=lambda v: v is None,
        description="Pinned ray_matrix study and physical settings. Discover available prescriptions with read_component_library.")
    length_mm: float = Field(default=0, ge=0, le=1e6)
    n: float | None = Field(default=1, ge=1, le=5, json_schema_extra=describe_index,
        description="厚透镜和 GRIN 元件需要提供工作波长处的折射率。普通空间省略时按 n=1；薄元件采用空气等效光焦度。")
    focal_mm: float | None = Field(default=None, description="薄透镜的空气等效焦距。正值会聚，负值发散，薄透镜的焦距为空时待补。")
    radius_mm: float | None = Field(default=None, description="镜面曲率半径。空值表示平面，正值表示凹面，负值表示凸面。")
    aoi_deg: float = Field(default=0, ge=0, lt=85)
    front_radius_mm: float | None = Field(default=None, description="厚透镜前表面的曲率半径。正值表示曲率中心在下游，空值表示平面。")
    back_radius_mm: float | None = Field(default=None, description="厚透镜后表面的曲率半径。正值表示曲率中心在下游，空值表示平面。")
    gradient_per_mm: float = Field(default=0, ge=0, le=10, description="Focusing quadratic GRIN coefficient g; n(r) approximately n0*(1-g²r²/2).")
    matrix_t: list[float] | None = Field(default=None, min_length=4, max_length=4, description="A,B,C,D in reduced (y,n*theta), determinant 1. B mm, C 1/mm.")
    matrix_s: list[float] | None = Field(default=None, min_length=4, max_length=4)

    @model_validator(mode="before")
    @classmethod
    def material_index(cls, value):
        if not isinstance(value, dict):
            return value
        kind = value.get("kind")
        if value.get("n") is None and (kind in {"thick_lens","grin"} or (kind == "space" and
                                       ("n" in value or value.get("index_reference") is not None))):
            if not cls.allow_incomplete:
                raise PydanticCustomError("material_index", "「{name}」缺少工作波长处的折射率 n。",
                                          {"name":value.get("name", ""), "parameter":"n"})
            return {**value, "n": None}
        return value

    @model_validator(mode="after")
    def physical(self):
        if self.index_reference is not None and self.kind != "space":
            calibrated = self.kind in {"thick_lens", "grin"} and self.index_reference.model is None
            if not calibrated:
                raise ValueError("目录折射率模型须绑定到均匀介质段。厚透镜和 GRIN 的中心折射率可登记为工作波长下的单点值。")
        if (self.kind == "registered") != (self.prescription is not None):
            raise ValueError("已注册元件须声明计算方法、版本和参数。")
        if self.prescription and (self.n != 1 or self.matrix_t is not None or self.matrix_s is not None):
            raise ValueError("已注册元件的 ABCD 由扩展按波长计算，参考面采用空气等效坐标。")
        if self.kind == "lens" and self.focal_mm is None and not self.allow_incomplete:
            raise PydanticCustomError("missing_focal_length", "「{name}」需要填写带符号的焦距 f / mm。会聚取正，发散取负。",
                                      {"name":self.name, "parameter":"focal_mm"})
        if self.kind == "lens" and self.focal_mm is not None and abs(self.focal_mm) < 1e-6:
            raise ValueError(f"「{self.name}」需要填写带符号的焦距 f / mm，绝对值至少为 0.000001 mm。会聚取正，发散取负。")
        if self.kind == "mirror" and self.radius_mm is not None and abs(self.radius_mm) < 1e-6:
            raise ValueError(f"「{self.name}」的曲率半径 R / mm 绝对值须至少为 0.000001 mm；平面镜留空。")
        if self.kind in ZERO_THICKNESS_MODELS and (self.length_mm != 0 or self.n not in (1,None)):
            raise ValueError(f"「{self.name}」采用零厚度、空气等效模型。请把长度设为 0、折射率设为 1，或把介质建为独立传播段。")
        if self.kind in {"thick_lens", "grin"} and self.length_mm <= 0 and not self.allow_incomplete:
            raise ValueError(f"请为「{self.name}」填写大于零的物理长度，单位为 mm。")
        if self.front_radius_mm == 0 or self.back_radius_mm == 0:
            raise ValueError(f"「{self.name}」的平面折射面请将曲率半径留空。曲面需填写非零的带符号半径。")
        if self.kind == "matrix":
            if self.matrix_t is None and not self.allow_incomplete:
                raise ValueError(f"请为「{self.name}」填写切向 T 的四个 ABCD 系数。")
            for axis, values in (("T", self.matrix_t), ("S", self.matrix_s)):
                if values is not None and not math.isclose(values[0]*values[3]-values[1]*values[2], 1, abs_tol=1e-8):
                    raise ValueError(f"「{self.name}」的 {axis} 矩阵需满足 AD − BC = 1。请核对 B 的单位为 mm，C 的单位为每 mm。")
        return self


@cache
def element_catalog():
    """Editing and scanning use the same model parameters as the solver; presets are examples only."""
    properties = OpticalElement.model_json_schema()["properties"]
    bounds = {
        name: {k: v for k, v in properties[name].items() if k in {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"}}
        for name in ELEMENT_CATALOG["parameters"]
    }
    devices, issues = device_catalog()
    presets = dict(ELEMENT_CATALOG["presets"])
    for name, device in devices.items():
        if device["representation"] == "ray_matrix":
            presets["study:"+name] = {"kind":"registered","name":device["title"],"prescription":device["prescription"],"length_mm":device["length_mm"]}
    models = {**ELEMENT_CATALOG["models"], "registered":{"label":"参数化元件","parameters":[]}}
    materials = [{"key": key, "name": entry.get("name", key), "index_basis": entry.get("index_basis"),
                  "valid_range_nm": [1000 * x for x in entry["valid_range_um"]], "source": entry["source"],
                  "confidence": entry["confidence"]} for key, entry in load_materials().items() if not key.startswith("_")]
    return {**ELEMENT_CATALOG, "models":models, "bounds":bounds, "presets":presets,
            "material_models":materials, "devices":devices, "extension_issues":issues}


def scan_parameters(element):
    if element.prescription is None:
        fields = SCAN_PARAMETERS[element.kind]
        return [name for name in fields if name != "n"] if element.index_reference and element.index_reference.model else fields
    study = element.prescription.definition("ray_matrix")
    law = study.element_law
    return [("length_mm" if k == law.length_input else "parameters."+k)
            for k in scalar_scan_fields(study)]


def _set_scan_value(element, parameter, value):
    if parameter.startswith("parameters."):
        element["prescription"]["parameters"][parameter[11:]] = float(value)
    else:
        element[parameter] = float(value)


class OpticalSystem(StrictInput):
    resolve_indices = model_validator(mode="before")(resolve_medium_wavelength)
    require_input_radius: ClassVar[bool] = True
    require_closed_path: ClassVar[bool] = True
    mode: Literal["single_pass", "standing_wave", "ring"] = Field(description=(
        "单程按传播顺序排列。驻波腔从第一面端镜写到第二面端镜，求解器自动处理返程。"
        "共面环形腔按一次完整环行排列。"))
    wavelength_nm: float = Field(gt=0, le=1e6)
    elements: list[OpticalElement] = Field(min_length=1, max_length=80)
    input_radius_mm: float | None = Field(default=None, gt=0, description=(
        "单程入射面半径，单位 mm。理想高斯束取 1/e² 强度半径。"
        "实测束用 M² 传播二阶矩时取固定横轴的 2σ 半径，即 D4σ 直径的一半，并记录测量口径。"))
    input_curvature_mm: float | None = Field(default=None, description="空气中单程入射波前的曲率半径。空值表示入射面位于束腰。")
    samples_per_segment: int = Field(default=40, ge=2, le=150)
    beam_quality_m2: float = Field(default=1, ge=1, le=10000, description="Single-pass second-moment/equivalent Gaussian M²; cavity eigenmode always M²=1.")
    source: BeamSource | None = Field(default=None, exclude_if=lambda value: value is None, description=(
        "单程光路在入射空气参考面的光束。各轴可登记半径、波前、M²、测量来源与指向。"
        "给出 source 时省略旧式标量入射参数。"))

    def for_propagation(self):
        return self

    def primary_source(self):
        if self.mode != "single_pass":
            raise ValueError("谐振腔的基模由往返矩阵确定。")
        if self.source is not None:
            return self.source
        axis = SourceAxis(radius_mm=self.input_radius_mm, m2=self.beam_quality_m2,
                          wavefront="curved" if self.input_curvature_mm is not None else "waist",
                          curvature_mm=self.input_curvature_mm)
        return BeamSource(tangential=axis)

    def entrance_q(self, plane, effective_wavelength):
        axis = self.primary_source().axis(plane)
        if missing_axis_inputs(self.wavelength_nm, axis):
            return None
        return B.q_from_beam(axis.radius_mm, axis.curvature_mm if axis.wavefront == "curved" else None, effective_wavelength)

    @model_validator(mode="after")
    def topology(self):
        if self.input_curvature_mm == 0:
            raise ValueError("Flat input wavefront is null, not zero curvature radius")
        if self.source is not None and (self.mode != "single_pass" or self.input_radius_mm is not None
                                        or self.input_curvature_mm is not None or self.beam_quality_m2 != 1):
            raise ValueError("双轴入射源仅适用于单程光路。请在 source 中登记参数，并清除原标量半径、曲率和 M² 设置。")
        if self.require_input_radius and self.mode == "single_pass" and self.input_radius_mm is None and self.source is None:
            raise ValueError("Single pass requires input beam radius and wavefront (null for waist)")
        if self.mode != "single_pass" and self.beam_quality_m2 != 1:
            raise ValueError("Resonator eigenmode is fundamental; M²>1 is only a single-pass propagation surrogate")
        if self.require_closed_path:
            if self.mode == "standing_wave" and (len(self.elements) < 3 or self.elements[0].kind != "mirror" or self.elements[-1].kind != "mirror"):
                raise ValueError("驻波腔需要首尾两面端镜，中间包含传播光路。")
            if self.mode != "single_pass" and not any(e.length_mm > 0 for e in self.elements):
                raise ValueError("腔的往返光路需要正的传播长度。")
        return self


class AxisQ(StrictInput):
    real_mm: float
    imag_mm: float = Field(gt=0)


class ParaxialField(StrictInput):
    frame: Literal["air.aligned-forward-ts.v1"]
    wavelength_nm: float = Field(gt=0, le=1e6)
    beam_quality_m2: float = Field(ge=1, le=10000)
    tangential: AxisQ
    sagittal: AxisQ


def _propagation_inputs(schema):
    # Either a registered v2 beam or a user-supplied wavelength provides the vacuum wavelength.
    schema["required"] = [name for name in schema["required"] if name != "wavelength_nm"]
    schema["anyOf"] = [{"required": ["wavelength_nm"]},
                       {"required": ["input_beam"], "properties": {"input_beam": {"type": "object"}}}]


class PropagationStudySystem(OpticalSystem):
    """The study runner can continue a saved field at its outgoing air reference plane."""
    require_input_radius: ClassVar[bool] = False
    model_config = ConfigDict(json_schema_extra=_propagation_inputs)
    wavelength_nm: float = Field(gt=0, le=1e6, description="真空波长；引用逐轴入射光束时可沿用其波长。",
                                 json_schema_extra={"x-default-from": {"field": "input_beam", "path": "wavelength_nm"}})
    beam_quality_m2: float = Field(default=1, ge=1, le=10000, description="标量光源的 M²。引用同轴状态时沿用其 M²；逐轴状态使用各轴自己的 M²。",
                                   json_schema_extra={"x-default-from": {"field": "input_field", "path": "beam_quality_m2"}})
    input_field: ParaxialField | None = Field(default=None, title="入射光束状态", description="在空气中衔接共面、同向、同轴的 T/S 参考面。")
    input_beam: ParaxialBeamState | None = Field(default=None, title="逐轴入射光束", description=(
        "衔接空气中的共面参考面，保留 T/S 各自的宽度口径、M²、波前及束心指向；缺失值保持未知。"))

    @model_validator(mode="before")
    @classmethod
    def wavelength_from_beam(cls, values):
        if isinstance(values, dict) and "wavelength_nm" not in values:
            beam = values.get("input_beam")
            wavelength = beam.get("wavelength_nm") if isinstance(beam, dict) else getattr(beam, "wavelength_nm", None)
            if wavelength is not None:
                return {**values, "wavelength_nm": wavelength}
        return values

    @model_validator(mode="after")
    def check_field(self):
        field = self.input_beam or self.input_field
        if field is None:
            return self
        if (self.mode != "single_pass" or self.input_radius_mm is not None or self.input_curvature_mm is not None
                or self.source is not None or (self.input_field and self.input_beam)):
            raise ValueError("引用光束状态时请选择单程传播，并只保留一个入射来源，清除重复的半径、曲率或状态。")
        if field.wavelength_nm != self.wavelength_nm:
            raise ValueError("入射光束与本段传播须使用同一真空波长。")
        if self.input_beam:
            if self.beam_quality_m2 != 1:
                raise ValueError("逐轴入射光束自带各轴的 M²，请清除模型外的标量 M² 设置。")
            return self
        if "beam_quality_m2" in self.model_fields_set and self.beam_quality_m2 != self.input_field.beam_quality_m2:
            raise ValueError("本段 M² 与引用的光束状态不一致。")
        self.beam_quality_m2 = self.input_field.beam_quality_m2
        return self

    def entrance_q(self, plane, effective_wavelength):
        if self.input_field:
            axis = getattr(self.input_field, plane)
            return complex(axis.real_mm, axis.imag_mm)
        return super().entrance_q(plane, effective_wavelength)

    def primary_source(self):
        if self.input_beam:
            return self.input_beam
        if self.input_field:
            # A registered q transfers the field at the outgoing *air* reference plane.
            def axis(name):
                state = getattr(self.input_field, name)
                q = complex(state.real_mm, state.imag_mm)
                curvature = B.radius_from_q(q)
                return SourceAxis(radius_mm=B.w_from_q(q, self.wavelength_nm * self.beam_quality_m2),
                                  m2=self.beam_quality_m2, wavefront="curved" if math.isfinite(curvature) else "waist",
                                  curvature_mm=curvature if math.isfinite(curvature) else None)
            return BeamSource(tangential=axis("tangential"), sagittal=axis("sagittal"))
        return super().primary_source()


class OpticalScan(StrictInput):
    system: OpticalSystem
    element_index: int = Field(ge=0)
    parameter: ScanParameter
    start: float
    stop: float
    count: int = Field(default=101, ge=2, le=501)


class ScanAxis(StrictInput):
    element_index: int = Field(ge=0)
    parameter: ScanParameter
    start: float
    stop: float
    count: int = Field(default=41, ge=2, le=101)


class OpticalScan2D(StrictInput):
    system: OpticalSystem
    x: ScanAxis
    y: ScanAxis


def _scan_issue(error, system):
    if not isinstance(error, ValidationError):
        return str(error)
    reasons = []
    bounds = {"greater_than": ("大于", "gt"), "greater_than_equal": ("大于等于", "ge"),
              "less_than": ("小于", "lt"), "less_than_equal": ("小于等于", "le")}
    for issue in error.errors(include_input=False, include_url=False):
        location = list(issue["loc"])
        if len(location) >= 2 and location[0] == "elements" and isinstance(location[1], int):
            location[:2] = [system.elements[location[1]].name]
        field = " › ".join(str(ELEMENT_CATALOG["parameters"].get(part, part)) for part in location)
        message = issue["msg"].removeprefix("Value error, ")
        if issue["type"] in bounds:
            relation, key = bounds[issue["type"]]
            message = f"需要{relation} {issue['ctx'][key]}"
        reasons.append(f"{field} · {message}" if field else message)
    return "。".join(dict.fromkeys(reasons))


def _scan_summary(total, invalid, partial, first_issue=None):
    valid = total - invalid
    status = "needs_input" if not valid else "partial" if invalid or partial else "completed"
    counts = {"status": status, "grid_points": total, "valid_count": valid, "invalid_count": invalid,
              "partial_count": partial, "completed_count": valid - partial}
    if invalid:
        counts["message"] = f"{total} 个采样点中 {invalid} 个未能计算。{first_issue}"
    elif partial:
        counts["message"] = f"{partial} 个采样点仅能计算部分物理量。请核对各平面的输入。"
    return counts


def _scan_grid(start, stop, count):
    if math.isfinite(stop - start):
        return np.linspace(start, stop, count).tolist()
    # Finite endpoints can span more than the largest representable number.
    return [start, *(start * (1-i/(count-1)) + stop * (i/(count-1)) for i in range(1, count-1)), stop]


def _solve_scan_point(raw):
    try:
        with np.errstate(over="raise", invalid="raise"):
            return analyze_system(raw, profile=False, stamp=False)
    except (FloatingPointError, OverflowError) as exc:
        raise ValueError("该参数组合的光线传输或光束数值超出可表示范围，请核对参数和单位。") from exc


def scan_system_2d(config):
    scan = config if isinstance(config, OpticalScan2D) else OpticalScan2D.model_validate(config)
    if scan.system.mode == "single_pass":
        raise ValueError("Stability map requires a resonator")
    if (scan.x.element_index, scan.x.parameter) == (scan.y.element_index, scan.y.parameter):
        raise ValueError("Two axes must change different parameters")
    for axis in (scan.x, scan.y):
        if axis.element_index >= len(scan.system.elements) or axis.stop <= axis.start:
            raise ValueError("Invalid scan range or element index")
        if axis.parameter not in scan_parameters(scan.system.elements[axis.element_index]):
            raise ValueError("Scan parameter does not affect this element")
    xs, ys = _scan_grid(scan.x.start, scan.x.stop, scan.x.count), _scan_grid(scan.y.start, scan.y.stop, scan.y.count)
    maps = {"tangential": [], "sagittal": [], "stable_both": []}
    issues = {}
    for yi, y in enumerate(ys):
        row = {key: [] for key in maps}
        for xi, x in enumerate(xs):
            raw = scan.system.model_dump()
            for axis, value in ((scan.x, x), (scan.y, y)):
                _set_scan_value(raw["elements"][axis.element_index], axis.parameter, value)
            try:
                output = _solve_scan_point(raw)
                for plane in ("tangential", "sagittal"):
                    row[plane].append(output["planes"][plane]["m"])
                row["stable_both"].append(int(output["stable_both_planes"]))
            except (ValueError, ArithmeticError) as exc:
                reason = _scan_issue(exc, scan.system)
                issues.setdefault(reason, []).append([yi, xi])
                for key in row:
                    row[key].append(None)
        for key in maps:
            maps[key].append(row[key])
    invalid = sum(map(len, issues.values()))
    return {**_scan_summary(len(xs)*len(ys), invalid, 0, next(iter(issues), None)),
            "solver_receipt": solver_receipt("scan_optical_system_2d", "LaserClaw", "2",
            compute=scan_system_2d, inputs=scan.system),
            "x": xs, "y": ys, "x_axis": scan.x.model_dump(), "y_axis": scan.y.model_dump(),
            "maps": maps, "invalid_groups": [{"message": reason, "cells": cells} for reason, cells in issues.items()],
            **({"warnings":[APERTURE_REFERENCE]} if any(e.clear_aperture for e in scan.system.elements) else {}),
            "scope": "Sampled two-parameter map; null cells invalid. Stability does not establish output power or global optimum."}


APERTURE_REFERENCE = (
    "光路含有限孔径。近轴曲线与稳定参数取无限孔径参考值。"
    "需要单程截光分布时，相干入射场可用「相干光场传播」，已知位置和方向的非相干光线可用「加权光线传播」。"
    "有限孔径腔的自洽模式还需要包含孔径损耗与衍射的往返模型。"
)


@dataclass(frozen=True)
class UniformSection:
    length_mm: float
    index: float

    def matrix(self, plane):
        return B.slab(self.length_mm, self.index)


@dataclass(frozen=True)
class QuadraticSurface:
    """Signed C coefficients in reduced ray coordinates, in reciprocal millimetres."""
    tangential: float
    sagittal: float

    def matrix(self, plane):
        c = self.sagittal if plane == "sagittal" else self.tangential
        return np.array([[1., 0.], [c, 1.]])


def paraxial_sections(element, wavelength_nm=None, *, length=None, index=None):
    """Physical sections shared by the ray and scalar field solvers, in propagation order."""
    n = element.n if index is None else index
    distance = element.length_mm if length is None else length
    if element.kind == "space" or (element.kind == "grin" and not element.gradient_per_mm):
        return (UniformSection(distance, n),)
    if element.kind == "thick_lens":
        front = (1 - n) / element.front_radius_mm if element.front_radius_mm else 0.
        back = (n - 1) / element.back_radius_mm if element.back_radius_mm else 0.
        return (QuadraticSurface(front, front), UniformSection(distance, n), QuadraticSurface(back, back))
    if element.kind == "lens":
        c = B.thin_lens(element.focal_mm)[1, 0]
        return (QuadraticSurface(c, c),)
    if element.kind == "mirror":
        if element.radius_mm is None:
            return (QuadraticSurface(0., 0.),)
        return (QuadraticSurface(*(B.mirror_curved(element.radius_mm, element.aoi_deg, axis)[1, 0]
                                   for axis in ("tangential", "sagittal"))),)
    if element.kind == "registered" and element.prescription.definition("ray_matrix").element_law.scalar_transfer == "quadratic_phase":
        transfer = element.prescription.evaluate("ray_matrix", wavelength_nm, distance)
        return (QuadraticSurface(transfer["matrix_t"][2], transfer["matrix_s"][2]),)
    return None


def _matrix(element, plane, wavelength_nm=None):
    if element.kind == "registered":
        values = element.prescription.evaluate("ray_matrix", wavelength_nm, element.length_mm)
        return np.asarray(values["matrix_s" if plane == "sagittal" else "matrix_t"]).reshape(2, 2)
    if element.kind == "matrix":
        return np.array(element.matrix_s if plane == "sagittal" and element.matrix_s is not None else element.matrix_t).reshape(2, 2)
    if element.kind == "grin":
        g, n, length = element.gradient_per_mm, element.n, element.length_mm
        if g:
            c, s = math.cos(g * length), math.sin(g * length)
            return np.array([[c, s / (n*g)], [-n*g*s, c]])
    sections = paraxial_sections(element, wavelength_nm)
    if sections:
        if len(sections) == 1:
            return sections[0].matrix(plane)
        return B.system_matrix(section.matrix(plane) for section in sections)
    raise ValueError(f"未注册元件模型 {element.kind} 的近轴传播矩阵。")


def center_profile(elements, source, plane, wavelength_nm, samples_per_segment, *, start=0., confined=True,
                   at_entrance=None, face_matrices=None):
    """Signed chief-ray offsets in the unfolded engineering frame; coordinates and offsets are in mm."""
    specified = source is not None and source.launch_ray is not None
    points, breaks = [], []
    output = {"center_basis":"specified" if specified else "reference_axis",
              "center_profile":points, "center_profile_breaks":breaks}
    if not confined:
        return output
    ray = (0., 0.)
    transfer = np.eye(2)
    if specified:
        origin = axis_origin(source, plane)
        ray = (origin.offset_mm, origin.angle_mrad / 1000 * getattr(origin, "medium_n", 1.)
               if origin.angle_mrad is not None else None)
        transfer = np.asarray(origin.matrix)
    direct = at_entrance is None and None not in ray
    if direct:
        ray = transfer_ray(ray, transfer)
        transfer = np.eye(2)

    disconnected = False
    linked_to_origin = True
    def mark(z, x, *, separate=False):
        nonlocal disconnected
        if x is None or not math.isfinite(x):
            disconnected = True
            return
        if points and points[-1] == [z, x]:
            return
        if points and (separate or disconnected or points[-1][0] == z):
            breaks.append(len(points))
        points.append([z, x])
        disconnected = False

    z = start
    mark(z, transfer_ray(ray, transfer)[0])
    for index, (element, n) in enumerate(elements):
        length = element.length_mm
        if at_entrance:
            at_entrance(element, ray, transfer, linked_to_origin)
        if n is None and element.kind == "space":
            if length:
                # An unknown n changes the unknown drift distance L/n. A parallel
                # ray still keeps its height and all rays retain their reduced slope.
                x, p = transfer_ray(ray, transfer)
                ray = (x if p == 0 else None, p)
                transfer = np.eye(2)
                linked_to_origin = False
                direct = at_entrance is None and None not in ray
                mark(z + length, ray[0])
            z += length
            continue
        if n is None and element.kind in ("thick_lens", "grin"):
            break
        optic = element.model_copy(update={"n":n}) if n is not None and n != element.n else element
        try:
            if optic.allow_incomplete:
                optic = OpticalElement.model_validate(optic.model_dump())
            matrix = face_matrices[index] if face_matrices is not None else _matrix(optic, plane, wavelength_nm)
        except (ValueError, ArithmeticError):
            break
        interior = optic.kind in ("space", "thick_lens", "grin")
        g = optic.gradient_per_mm if optic.kind == "grin" else 0.
        # A rapidly oscillating GRIN cannot be drawn from a handful of chords.
        # Beyond the display budget retain both solved faces without a line.
        cycles = g * length / math.pi
        count = max(samples_per_segment, math.ceil(10 * cycles) + 1) if g else 2
        resolved = interior and (ray == (0., 0.) or count <= 1024)
        if samples_per_segment and length and resolved and ray != (0., 0.):
            inside = (compose_ray_matrices(paraxial_sections(optic)[0].matrix(plane), transfer)
                      if optic.kind == "thick_lens" else transfer)
            # When either launch coordinate is unknown, known heights are isolated
            # conjugate planes. Solve for zero sensitivity to that coordinate.
            if (ray[0] is None) != (ray[1] is None):
                col = 0 if ray[0] is None else 1
                a, c = inside[:, col]
                if g:
                    phase = math.atan2(-a, c / (n*g)) % math.pi
                    roots = ((phase + k*math.pi)/g for k in range(math.ceil(cycles)+1))
                else:
                    roots = [-a*n/c] if c else ()
                for depth in roots:
                    if 0 < depth < length:
                        local = _matrix(optic.model_copy(update={"length_mm":depth}), plane, wavelength_nm) if g else B.slab(depth, n)
                        imaging = compose_ray_matrices(local, inside)
                        imaging[0, col] = 0.
                        mark(z + depth, transfer_ray(ray, imaging)[0], separate=True)
                disconnected = True
            elif g and ray[0] is not None:
                for depth in np.linspace(0, length, count)[1:-1]:
                    local = _matrix(optic.model_copy(update={"length_mm":float(depth)}), plane, wavelength_nm)
                    mark(z + float(depth), transfer_ray(ray, compose_ray_matrices(local, inside))[0])
        z += length
        if direct:
            # A centered ray stays centered under a homogeneous ABCD transform.
            if ray != (0., 0.):
                ray = transfer_ray(ray, matrix)
            center = ray[0]
        else:
            transfer = compose_ray_matrices(matrix, transfer)
            center = transfer_ray(ray, transfer)[0]
        mark(z, center, separate=bool(length and not resolved))
    return output


def _point(q, z, wl, n=1):
    # Reduced q has identical spot radius on either side of a flat interface.
    physical_q = n * q
    radius = B.radius_from_q(physical_q)
    return {"z_mm": float(z), "radius_mm": B.w_from_q(physical_q, wl, n),
            "curvature_mm": float(radius) if math.isfinite(radius) else None,
            "q_real_mm": float(physical_q.real), "q_imag_mm": float(physical_q.imag), "n": n}


def evaluated_devices(elements, wavelength_nm):
    records = []
    for index, element in enumerate(elements):
        recipe = element.prescription
        if recipe is not None:
            transfer = recipe.evaluate("ray_matrix", wavelength_nm, element.length_mm)
            records.append({"element_uid": element.uid, "element_index": index, "wavelength_nm": wavelength_nm,
                            "prescription": recipe.model_dump(), **transfer})
    return records


def trace_element(element, plane, q, z, wavelength_nm, effective_wl, samples_per_segment, *, face_matrix=None,
                  profile_omissions=None):
    """Propagate a reduced q through one physical element and sample its known interior."""
    samples, waists, breaks = [], [], []
    profile = bool(samples_per_segment)
    if profile and element.kind == "grin" and element.gradient_per_mm:
        depths, minima, bounds = grin_depths(q, element.n, element.gradient_per_mm, element.length_mm, samples_per_segment)
        if bounds is not None:
            breaks.append(1)
            entrance_radius = B.w_from_q(q, effective_wl)
            minimum, maximum, first = bounds
            period = math.pi/element.gradient_per_mm
            waists.append({"z_mm":z+first,"radius_mm":entrance_radius*minimum,"segment":element.name,
                           "medium":"quadratic_index","period_mm":period,"repeats_until_z_mm":z+element.length_mm})
            if profile_omissions is not None:
                profile_omissions.append({"reason":"sampling_budget","element_uid":element.uid,"name":element.name,
                    "start_mm":z,"end_mm":z+element.length_mm,
                    "radius_min_mm":entrance_radius*minimum,"radius_max_mm":entrance_radius*maximum,
                    "first_min_z_mm":z+first,"period_mm":period})
        minima = set(minima)
        for distance in depths:
            sub = element.model_copy(update={"length_mm": float(distance)})
            point = _point(B.propagate_q(_matrix(sub, plane), q), z+distance, effective_wl, element.n)
            samples.append(point)
            if distance in minima:
                waists.append({"z_mm":point["z_mm"],"radius_mm":point["radius_mm"],"segment":element.name,
                               "medium":"quadratic_index"})
    elif profile and element.kind in {"space","thick_lens","grin"}:
        inside = q
        if element.kind == "thick_lens":
            entrance, _, _ = paraxial_sections(element)
            inside = B.propagate_q(entrance.matrix(plane), q)
        distances, minima = uniform_depths(inside, element.n, element.length_mm, samples_per_segment)
        for waist_at in minima:
            zr = inside.imag * element.n
            waists.append({"z_mm": z + waist_at, "radius_mm": B.w_from_q(complex(0, zr), effective_wl, element.n),
                           "rayleigh_length_mm": zr, "segment": element.name})
        samples.extend(_point(inside+d/element.n, z+d, effective_wl, element.n) for d in distances)
    # Lumped elements have two solved faces even when they occupy the same z.
    # A finite-length transfer law without interior samples leaves the interval open.
    faces_only = profile and not samples
    if faces_only:
        samples.append(_point(q, z, effective_wl))
        if element.length_mm:
            breaks.append(1)
    q = B.propagate_q(face_matrix if face_matrix is not None else _matrix(element, plane, wavelength_nm), q)
    z += element.length_mm
    exit_point = _point(q, z, effective_wl)
    if faces_only:
        samples.append(exit_point)
    return q, exit_point, samples, waists, breaks


def axis_geometry(system, plane, forward=None):
    """First-order ray transfer, independent of the incident Gaussian field."""
    paths = system.elements
    outward = paths[1:] if system.mode == "standing_wave" else paths
    matrices = list(forward) if forward is not None else [_matrix(e, plane, system.wavelength_nm) for e in outward]
    if system.mode == "standing_wave":
        for matrix in reversed(matrices[:-1]):
            a, b, c, d = matrix.ravel()
            matrices.append(np.array([[d, b], [c, a]]))
        matrices.append(_matrix(paths[0], plane))
    matrix = ray_matrix_product(matrices) if system.mode == "single_pass" else B.system_matrix(matrices)
    info = {"matrix": matrix.tolist()}
    if system.mode == "single_pass":
        # After the exit plane in air a parallel input ray has y(z)=(A+zC)y(0).
        a, c = float(matrix[0, 0]), float(matrix[1, 0])
        if c != 0:
            focus = -a / c
            if math.isfinite(focus):
                info["collimated_focus_from_exit_mm"] = focus
        if "collimated_focus_from_exit_mm" not in info:
            info["collimated_focus_note"] = "平行入射光在该轴没有可表示的有限几何焦点。"
    else:
        info.update(B.stability(matrix))
    return matrix, info


def axis_metadata(source, wavelength):
    quality = source.m2 if source is not None else 1.0
    return {"beam_quality_m2": quality, "width_basis": source.width_basis() if source is not None else "gaussian",
            "effective_wavelength_nm": wavelength * quality if wavelength is not None and quality is not None else None}


def entrance_width_only(source, wavelength, issue=None):
    """Retain a supplied source-plane width without constructing a q or downstream profile."""
    missing = missing_axis_inputs(wavelength, source)
    return {"elements": [], "samples": [{"z_mm": 0., "radius_mm": source.radius_mm}] if source.radius_mm is not None else [],
            "message": issue or "入射光源需补充" + "、".join(missing) + "。"}


def partial_axis_faces(info, source, name, elements, wavelength_nm, matrices):
    """Expose each independently solved reference face, without filling the unmeasured intervals."""
    origin = axis_origin(source, name)
    if origin.axis.radius_mm is None and origin.axis.wavefront == "unknown":
        return
    z = 0.
    faces = []
    samples = info["samples"]
    transfer = np.eye(2)
    for element, matrix in zip(elements, matrices, strict=True):
        z += element.length_mm
        transfer = compose_ray_matrices(matrix, transfer)
        axis, _ = origin.followed_by(transfer).at_plane(wavelength_nm)
        known = {}
        if axis.radius_mm is not None:
            known["radius_mm"] = axis.radius_mm
        if axis.wavefront != "unknown":
            known.update(wavefront=axis.wavefront, curvature_mm=axis.curvature_mm)
        point = {"z_mm": z, **known}
        faces.append({"kind": element.kind, "name": element.name, **point})
        if known:
            if samples:
                info.setdefault("profile_breaks", []).append(len(samples))
            samples.append(point)
    if any("wavefront" in face or "radius_mm" in face for face in faces):
        info["elements"] = faces
        if known:
            info["output"] = point


def analyze_system(config: OpticalSystem | dict, *, profile=True, stamp=True) -> dict:
    system = config.for_propagation() if isinstance(config, OpticalSystem) else OpticalSystem.model_validate(config)
    paths = system.elements
    if system.mode == "standing_wave":
        outward = paths[1:]
    else:
        outward = paths
    axes = ("tangential", "sagittal")
    source = system.primary_source() if system.mode == "single_pass" else None
    sources = {axis: source.axis(axis) for axis in axes} if source else {}
    metadata = {axis: axis_metadata(sources.get(axis), system.wavelength_nm) for axis in axes}
    effective = [item["effective_wavelength_nm"] for item in metadata.values()]
    gaussian = all(axis.width_basis() == "gaussian" for axis in sources.values())
    result = {"status": "completed", "engine": "BeamWeft Python/NumPy ABCD (not the Rezonator C++ runtime)",
              "mode": system.mode, "wavelength_nm": system.wavelength_nm,
              "effective_wavelength_nm": effective[0] if effective[0] == effective[1] else None,
              "system": system.model_dump(), "intensity_model": "ideal_gaussian" if gaussian else "width_only",
              "scope": ["Paraxial, coplanar tangential/sagittal axes; M²=1 eigenmodes, equivalent M² single-pass", "Centered elements on the nominal axis; registered observation planes report supplied input offsets and pointing",
                        "No gain, output-power, diffraction/aperture, or thermal-field prediction"], "planes": {}}
    warnings = model_wavelength_notes(paths, system.wavelength_nm)
    if any(e.clear_aperture for e in paths):
        warnings.append(APERTURE_REFERENCE)
        result["intensity_model"] = "unclipped_reference"
    if warnings:
        result["model_warnings"] = warnings
        result["scope"].extend(warnings)
    for plane in axes:
        forward = [_matrix(el, plane, system.wavelength_nm) for el in outward]
        matrix, info = axis_geometry(system, plane, forward)
        info.update(metadata[plane])
        if profile:
            info.update(center_profile(((el, el.n) for el in outward), source, plane, system.wavelength_nm,
                                       system.samples_per_segment, confined=info.get("stable", True), face_matrices=forward))
        effective_wl = info["effective_wavelength_nm"]
        if system.mode == "single_pass":
            issue = None
            try:
                q = system.entrance_q(plane, effective_wl)
                if q is not None and (not math.isfinite(q.real) or not math.isfinite(q.imag) or q.imag <= 0):
                    raise ValueError("unrepresentable q")
            except (ArithmeticError, ValueError):
                q = None
                issue = "源面光束参数超出数值范围，请核对半径、曲率和单位。"
            if q is None:
                result["status"] = "partial"
                info.update(entrance_width_only(sources[plane], system.wavelength_nm, issue))
                if not issue and (system.source or getattr(system, "input_beam", None) or system.input_radius_mm is not None
                                  or system.input_curvature_mm is not None):
                    partial_axis_faces(info, source, plane, paths, system.wavelength_nm, forward)
                result["planes"][plane] = info
                continue
        else:
            q = B.cavity_mode_q(matrix) if info["stable"] else None
        if q is None:
            info.update({"samples": [], "elements": [], "message": "No confined Gaussian eigenmode (unstable or marginal)."})
            result["planes"][plane] = info
            continue
        info["input"] = _point(q, 0, effective_wl)
        z, samples, nodes, waists, omissions = 0., [], [], [], []
        if system.mode == "standing_wave":
            nodes.append({"name": paths[0].name, "kind": "mirror", **_point(q, 0, system.wavelength_nm)})
        for element, face_matrix in zip(outward, forward, strict=True):
            q, exit_point, points, local_waists, breaks = trace_element(
                element, plane, q, z, system.wavelength_nm, effective_wl,
                system.samples_per_segment if profile else 0, face_matrix=face_matrix, profile_omissions=omissions)
            if breaks:
                info.setdefault("profile_breaks", []).extend(len(samples) + i for i in breaks)
            samples.extend(points)
            waists.extend(local_waists)
            nodes.append({"name":element.name, "kind":element.kind, **exit_point})
            z += element.length_mm
        info.update({"output": _point(q, z, effective_wl), "samples": samples, "elements": nodes, "waists": waists})
        if omissions:
            info["profile_omissions"] = omissions
        if system.mode == "single_pass":
            w0, distance = B.waist_from_q(q, effective_wl)
            info["downstream_waist_in_air"] = {"radius_mm": w0, "distance_from_output_mm": distance,
                                               "note": "Free propagation beyond last element; negative distance is a virtual upstream waist."}
        result["planes"][plane] = info
    devices = evaluated_devices(paths, system.wavelength_nm)
    if devices:
        result["evaluated_devices"] = devices
        for record in devices:
            for note in record.get("warnings", []):
                result.setdefault("model_warnings", []).append(f"{paths[record['element_index']].name} · {note}")
    if system.mode != "single_pass":
        result["stable_both_planes"] = all(p["stable"] for p in result["planes"].values())
    if stamp:
        result["solver_receipt"] = solver_receipt("paraxial_abcd", "LaserClaw", "1", compute=analyze_system, inputs=system)
        if readings := material_index_readings([("primary", system)]):
            result["material_indices"] = readings
    return result


def scan_system(config: OpticalScan | dict) -> dict:
    scan = config if isinstance(config, OpticalScan) else OpticalScan.model_validate(config)
    if scan.element_index >= len(scan.system.elements) or scan.stop <= scan.start:
        raise ValueError("Scan requires an existing element index and stop > start")
    element = scan.system.elements[scan.element_index]
    if scan.parameter not in scan_parameters(element):
        raise ValueError("Scan parameter does not affect the chosen element type")
    rows = []
    for value in _scan_grid(scan.start, scan.stop, scan.count):
        raw = scan.system.model_dump()
        _set_scan_value(raw["elements"][scan.element_index], scan.parameter, value)
        try:
            solved = _solve_scan_point(raw)
            row = {"value": float(value), "status": solved["status"], "stable_both_planes": solved.get("stable_both_planes")}
            for plane, data in solved["planes"].items():
                row[plane] = {key: data[key] for key in ("m", "stable", "input", "output", "downstream_waist_in_air",
                                                         "message", "width_basis", "effective_wavelength_nm", "beam_quality_m2") if key in data}
            rows.append(row)
        except (ValueError, ArithmeticError) as exc:
            rows.append({"value": float(value), "status": "invalid", "message": _scan_issue(exc, scan.system)})
    intervals = {}
    for plane in ("tangential", "sagittal", "both"):
        spans, active = [], None
        for row in rows:
            stable = row.get("stable_both_planes") if plane == "both" else row.get(plane, {}).get("stable")
            if stable:
                if active is None:
                    active = {"first_stable_sample": row["value"], "last_stable_sample": row["value"]}
                    spans.append(active)
                active["last_stable_sample"] = row["value"]
            else:
                active = None
        intervals[plane] = spans
    invalid_rows = [row for row in rows if row["status"] == "invalid"]
    partial = sum(row["status"] == "partial" for row in rows)
    span = scan.stop - scan.start
    step = span/(scan.count-1) if math.isfinite(span) else scan.stop/(scan.count-1) - scan.start/(scan.count-1)
    return {**_scan_summary(len(rows), len(invalid_rows), partial, invalid_rows[0]["message"] if invalid_rows else None),
            "solver_receipt": solver_receipt("scan_optical_system", "LaserClaw", "2", compute=scan_system, inputs=scan.system),
            "mode": scan.system.mode, "parameter": scan.parameter, "element": element.name, "points": rows,
            **({"warnings":[APERTURE_REFERENCE]} if any(e.clear_aperture for e in scan.system.elements) else {}),
            "stable_intervals_sampled": intervals, "step": step if math.isfinite(step) else None,
            "scope": "Finite one-parameter grid; no global optimum claim. Recalculate chosen candidate with analyze_optical_system.",
            "stable_count": sum(row.get("stable_both_planes") is True for row in rows) if scan.system.mode != "single_pass" else None}

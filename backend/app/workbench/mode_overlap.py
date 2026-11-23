"""Cross-sectional overlap of separable scalar Gaussian modes in a shared T/S frame."""

import math
from pydantic import Field, model_validator
from ..physics.beam import gaussian_product_density
from ..study_api import Condition, Port, Study

from .observation import AXES, OpticalLayout, analyze_sections
from .optics import StrictInput, analyze_system, model_wavelength_notes
from .project_outputs import LAYOUT, project_scope_warnings
from .projects import read_project


class SectionPair(StrictInput):
    plane_uid: str = Field(min_length=1, max_length=100, title="登记观察面",
                           json_schema_extra={"x-beamweft-input":"plane-uid"})
    first_beam_uid: str = Field(default="primary", min_length=1, max_length=100, title="第一束光",
                                json_schema_extra={"x-beamweft-input":"path-uid"})
    second_beam_uid: str = Field(min_length=1, max_length=100, title="第二束光",
                                 json_schema_extra={"x-beamweft-input":"path-uid"})

    @model_validator(mode="after")
    def different_beams(self):
        if self.first_beam_uid == self.second_beam_uid:
            raise ValueError("请选择两束不同的光。")
        return self


class SectionModeStudy(SectionPair):
    layout: OpticalLayout = Field(title="包含两束光与登记观察面的工程光路")


class ModeOverlap(SectionPair):
    revision_id: int | None = Field(default=None, gt=0)
    layout: OpticalLayout | None = Field(
        default=None,
        description="An explicit exploratory layout. Omit to use the bound model, or provide revision_id to select a historical version.",
    )

    @model_validator(mode="after")
    def valid_query(self):
        if self.layout is not None and self.revision_id is not None:
            raise ValueError("请指定一个工程版本或一份光路草稿。")
        return self


class InspectModeOverlap(ModeOverlap):
    save_result: bool = Field(default=False, description="Save this result and its input in the project's scientific calculation history for a parameter search, comparison, or replay.")


def overlap_input(db, case_id, request, working=None):
    if request.layout is not None:
        return request.layout, None
    revision_id = request.revision_id or (working or {}).get("revision_id")
    if working and revision_id is None:
        raise ValueError("这条问题尚未绑定光路，请选择模型或提供显式光路。")
    record = read_project(db, case_id, revision_id)
    if not record:
        raise ValueError("此案例尚无光路，请先建立模型或提供草稿。")
    layout = OpticalLayout.model_validate(
        {k: v for k, v in record["project"].items() if k in OpticalLayout.model_fields}
    )
    return layout, record


def _unavailable(status, note):
    return {"status": status, "note": note}


def _axis_integrals(first, second, axis, wavelength_mm):
    # Shift the Gaussian product to its real envelope maximum before integrating
    # the quadratic phase. This avoids subtracting two large absolute positions.
    u, v = first["field_axes"][axis], second["field_axes"][axis]
    w1, w2 = first["radii_mm"][axis], second["radii_mm"][axis]
    scale = min(w1, w2)
    h1, h2 = (scale / w1) ** 2, (scale / w2) ** 2
    a = h1 + h2
    span = math.hypot(w1, w2)
    delta = v["center_mm"] - u["center_mm"]
    relative = delta / span
    attenuation = math.exp(-2 * relative * relative)
    width_factor = 2 * (scale / max(w1, w2)) / a
    shape = math.sqrt(width_factor) * attenuation
    density = gaussian_product_density(w1, w2, delta)
    out = {"intensity_cosine": shape, "product_per_mm": density}
    if wavelength_mm is not None and wavelength_mm > 0:
        k = 2 * math.pi / wavelength_mm
        c1, c2 = u["reduced_vergence_per_mm"], v["reduced_vergence_per_mm"]
        b = 0.5 * k * (c2 - c1) * scale * scale
        norm = math.hypot(a, b)
        gradient = k * ((v["reduced_angle"] - u["reduced_angle"]) - delta * (c1 * h2 + c2 * h1) / a)
        phase = (gradient * scale / norm) * math.sqrt(a * 0.5)
        out["field_power_fraction"] = width_factor * (a / norm) * attenuation * math.exp(-phase * phase)
    return out


def _coherence(first, second):
    waves = (first["wavelength_nm"], second["wavelength_nm"])
    if None in waves:
        return None, _unavailable("needs_input", "需确认两束光的真空波长。")
    if not math.isclose(*waves, rel_tol=1e-12):
        return None, _unavailable("not_applicable", "波长不同，仅比较强度分布；跨波长的场投影不能作为注入耦合比例。")
    indices = (first.get("index"), second.get("index"))
    if None in indices:
        return None, _unavailable("needs_input", "需确认该截面对这两束光使用的折射率。")
    if not math.isclose(*indices, rel_tol=1e-12):
        return None, _unavailable(
            "not_applicable", "两束光在该截面的折射率不同，当前标量场投影要求同一传播介质与偏振模型。"
        )
    missing = []
    for beam in (first, second):
        for field, label in (("reduced_vergence_per_mm", "波前曲率"), ("reduced_angle", "指向")):
            axes = [
                letter
                for axis, letter in zip(AXES, ("T", "S"))
                if beam.get("field_axes", {}).get(axis, {}).get(field) is None
            ]
            if axes:
                missing.append(f"{beam['name']}的 {'/'.join(axes)} {label}")
    if missing:
        return None, _unavailable("needs_input", "需补充" + "、".join(missing) + "。")
    return waves[0] * 1e-6, None


def _metrics(first, second):
    for beam in (first, second):
        if "clipped" in beam.get("width_basis", {}).values():
            clipped = _unavailable("unsupported", "此光束经过有限孔径，当前高斯积分无法计算其孔后强度或相干重叠。")
            return {"intensity": clipped, "coherent": clipped}
    wl, coherent = _coherence(first, second)
    for beam in (first, second):
        for axis in AXES:
            if beam.get("width_basis", {}).get(axis) != "gaussian":
                note = _unavailable("unsupported", beam.get("notes",{}).get(axis) or "此轴未确定为理想基模高斯分布。仅凭 M² 或二阶矩半径无法确定该积分。")
                return {"intensity": note, "coherent": coherent or note}
            radius = beam["radii_mm"].get(axis)
            if radius is None or radius <= 0 or beam.get("field_axes", {}).get(axis, {}).get("center_mm") is None:
                issue = beam.get("notes", {}).get(axis) or "需补充观察面处的光斑半径和中心位置。"
                note = _unavailable("needs_input", issue)
                return {"intensity": note, "coherent": coherent or note}
    try:
        axes = [_axis_integrals(first, second, axis, wl) for axis in AXES]
        cosine = math.prod(a["intensity_cosine"] for a in axes)
        density = math.prod(a["product_per_mm"] for a in axes)
        if not (math.isfinite(cosine) and math.isfinite(density)):
            raise ValueError("强度分布超出双精度计算范围，请核对光斑与位置单位。")
    except (ArithmeticError, ValueError) as exc:
        note = _unavailable("needs_input", str(exc) if isinstance(exc, ValueError) else "该组参数超出数值范围。")
        return {"intensity": note, "coherent": coherent or note}
    if coherent is None:
        if all(math.isfinite(v.get("field_power_fraction", math.nan)) for v in axes):
            projected = {a: min(1.0, v["field_power_fraction"]) for a, v in zip(AXES, axes)}
            coherent = {"status": "completed", "power_fraction": math.prod(projected.values()), "per_axis": projected}
        else:
            coherent = _unavailable("needs_input", "相位超出双精度计算范围，请核对波长、曲率和角度单位。")
    return {
        "intensity": {"status": "completed", "cosine": min(1.0, cosine), "normalized_product_per_mm2": density},
        "coherent": coherent,
    }


def _scope_warnings(layout, record, ids, by_id):
    warnings = []
    if "primary" in ids:
        warnings += project_scope_warnings((record or {}).get("project", {}))
        warnings += model_wavelength_notes(layout.system.elements, layout.system.wavelength_nm)
    for path in layout.incident_paths:
        if path.uid in ids:
            warnings += [f"{path.name} · {line}" for line in model_wavelength_notes(path.elements, path.wavelength_nm)]
    for uid in ids:
        beam = by_id.get(uid, {})
        angles = [state.get("reduced_angle") for state in beam.get("field_axes", {}).values()]
        n = beam.get("index")
        if n and len(angles) == len(AXES) and None not in angles:
            angle = math.hypot(*angles) / n
            if angle >= math.pi / 2 or (angle and math.tan(angle) / angle - 1 > 0.01):
                warnings.append(
                    f"光束 {uid} 的截面指向为 {1000 * angle:.4g} mrad。近轴斜率近似已偏离实际几何，需用大角度模型复核。"
                )
    return list(dict.fromkeys(warnings))


def evaluate_overlap(layout, record, request):
    plane = next((p for p in layout.observation_planes if p.uid == request.plane_uid), None)
    if plane is None:
        raise ValueError("找不到指定观察面，请核对本版工程的观察面标识。")
    ids = (request.first_beam_uid, request.second_beam_uid)
    known = {
        "primary": "主光束" if layout.system.mode == "single_pass" else "腔基模",
        **{p.uid: p.name for p in layout.incident_paths},
    }
    if any(uid not in known for uid in ids):
        raise ValueError("指定光束不属于该版光路，请核对支路标识。")
    section = None
    if record:
        sections = record["analysis"].get("sections", {}).get("planes", [])
        section = next(
            (
                s
                for s in sections
                if s["uid"] == plane.uid and s.get("beams") and all("field_axes" in b for b in s["beams"])
            ),
            None,
        )
    if section is None:
        paths = [p.model_copy(update={"power_budget": None}) for p in layout.incident_paths if p.uid in ids]
        local = layout.model_copy(
            update={"observation_planes": [plane], "single_pass_power": None, "incident_paths": paths}
        )
        primary = record["analysis"] if record else analyze_system(layout.system, profile=False)
        section = analyze_sections(local, primary)["planes"][0]
    by_id = {b["uid"]: {**b, "name": known[b["uid"]]} for b in section["beams"]}
    if all(uid in by_id for uid in ids):
        metrics = _metrics(by_id[ids[0]], by_id[ids[1]])
    else:
        note = _unavailable("needs_input", section.get("message", "所选两束光未同时连通到这个截面。"))
        metrics = {"intensity": note, "coherent": note}
    states = [m["status"] for m in metrics.values()]
    status = (
        "completed"
        if all(s in {"completed", "not_applicable"} for s in states)
        else "partial"
        if "completed" in states
        else "needs_input"
    )
    return {
        "status": status,
        "method": "separable-gaussian-overlap-v1",
        "revision_id": record["revision_id"] if record else None,
        "plane": {
            "uid": plane.uid,
            "name": plane.name,
            "z_mm": section.get("z_mm"),
            "target_uid": section.get("target_uid"),
            "depth_mm": section.get("depth_mm"),
            "position_reference": section.get("position_reference", "element_depth"),
        },
        "beams": [{"name": known[uid], **by_id.get(uid, {"uid": uid})} for uid in ids],
        "scope_warnings": _scope_warnings(layout, record, ids, by_id),
        "assumptions": [
            "同一展开截面、平行 T/S 主轴、共向近轴传播、无限孔径。",
            "场投影假设相同偏振，表示理想空间基模的功率份额。实际注入、吸收、增益及输出功率还取决于其他物理条件。",
        ],
        **metrics,
    }


def study_overlap(args):
    result = evaluate_overlap(args.layout, None, args)
    # The scientific record carries exact source revisions per input, including standalone layouts.
    result.pop("revision_id")
    result["first_beam_uid"] = args.first_beam_uid
    result["second_beam_uid"] = args.second_beam_uid
    plane = result["plane"]
    plane["datum"] = ("element:" + plane["target_uid"] if plane["position_reference"] == "element_depth" and plane["target_uid"]
                      else plane["position_reference"])
    result["first_wavelength_nm"], result["second_wavelength_nm"] = (b.get("wavelength_nm") for b in result["beams"])
    result["summary"] = f"{result['beams'][0]['name']} × {result['beams'][1]['name']} · {result['plane']['name']}"
    result["output_limits"] = {path: result[path.split(".")[0]]["note"] for path in _SECTION_PORTS
                               if result[path.split(".")[0]]["status"] != "completed"}
    if result["status"] != "completed":
        result["message"] = " ".join(dict.fromkeys(result[key]["note"] for key in ("intensity", "coherent")
                                                   if result[key].get("note")))
    return result


_SITE = {
    "optics.observation-plane.uid": Condition(path="plane.uid", label="登记观察面", value_type="text"),
    "optics.reference-beam.uid": Condition(path="first_beam_uid", label="第一束光", value_type="text"),
    "optics.compared-beam.uid": Condition(path="second_beam_uid", label="第二束光", value_type="text"),
    "optics.observation-mount": Condition(path="plane.datum", label="观察面的定位基准", value_type="text"),
}


def _section_port(quantity, label, unit="1", scale=1):
    conditions = {**_SITE,
                  "optics.first-beam-wavelength.nm": Condition(path="first_wavelength_nm", label="第一束真空波长 / nm"),
                  "optics.second-beam-wavelength.nm": Condition(path="second_wavelength_nm", label="第二束真空波长 / nm")}
    return Port(quantity=quantity, label=label, unit=unit, scale=scale, available_on_partial=True,
                conditions=conditions, comparison_conditions=tuple(_SITE), inherit_conditions_from="layout")


_SECTION_PORTS = {
    "intensity.cosine": _section_port("optics.transverse-intensity-cosine", "横向强度相似度"),
    "intensity.normalized_product_per_mm2": _section_port(
        "optics.transverse-normalized-intensity-product", "截面归一化强度乘积积分", "mm⁻²", 1e6),
    "coherent.power_fraction": _section_port("optics.coherent-spatial-mode-projection", "空间基模投影功率比例"),
}

SECTION_MODE_STUDY = Study(
    "compare_section_modes", "比较登记截面的两束光", "1",
    "在共向、可分离高斯近轴模型中，比较同一观察面的横向强度相似度和强度乘积密度。"
    "同波长、同介质并有波前和指向时还计算空间基模投影。结果只描述所选截面。",
    SectionModeStudy, study_overlap, input_ports={"layout": LAYOUT}, live_layout="layout",
    ignores_diagram_positions=True, view="optics.section-mode-comparison.v1",
    output_ports=_SECTION_PORTS, output_requirements={path: ("layout", "plane_uid", "first_beam_uid", "second_beam_uid")
                                                      for path in _SECTION_PORTS},
    keywords=("模式匹配", "mode matching", "Gaussian overlap", "模场", "耦合", "强度相似度"),
)

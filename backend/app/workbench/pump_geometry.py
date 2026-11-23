"""Reopen a saved second-moment pump calculation at its original entrance plane."""
import math
from uuid import uuid4

from ..design.analysis import candidate_launch, saved_pump_index
from ..design.assessment import PUMP_POSITIONS, candidate_pump_scope, positioned_train, pump_lenses
from ..design.brief import FIELDS
from .projects import OpticalProject, Part
from .optics import air_gap


def project_from_pump_assessment(design):
    pump = (design.get("evidence_assessment") or {}).get("pump_imaging") or {}
    values = design.get("resolved_inputs") or {}
    lenses = pump.get("lenses") or pump_lenses(values)
    modeled = pump.get("status") == "modeled"
    if not pump or design.get("assumption_policy") != "evidence_only" or not (modeled or positioned_train(values, lenses)):
        missing = "、".join(FIELDS[k][0] for k in pump.get("missing", []) if k in FIELDS)
        reason = (f"还缺{missing}。" if missing else pump.get("summary", "请先保存一次泵浦传播计算。"))
        raise ValueError("设计记录中缺少可还原的泵浦光路。" + reason)

    required = [*PUMP_POSITIONS, "crystal_length_mm"]
    if modeled:
        required.extend(("pump_wavelength_nm", "pump_exit_waist_radius_um", "pump_farfield_half_angle_mrad", "pump_index"))
    missing = [FIELDS[k][0] for k in required if values.get(k) is None]
    if missing:
        raise ValueError("原设计缺少复算输入：" + "、".join(missing) + "。请补齐后保存新设计。")
    if not all((lenses.get(role) or {}).get("focal_mm") is not None for role in ("collimator", "focus_lens")):
        raise ValueError("原设计缺少已求解光路的透镜焦距。请核对元件资料后保存新设计。")

    radius = values["pump_exit_waist_radius_um"] / 1000 if values.get("pump_exit_waist_radius_um") else None
    angle = values["pump_farfield_half_angle_mrad"] / 1000 if values.get("pump_farfield_half_angle_mrad") else None
    beam = pump.get("beam") or {}
    assumptions = [pump["scope"]]
    if not modeled:
        assumptions.append(pump["summary"])
    return _build_train(values, lenses, radius, angle, measured=True, propagated=modeled, quality=beam.get("m2"),
                        assumptions=assumptions, minimum=beam.get("minimum_inside_crystal"))


def project_from_candidate_pump(design, candidate):
    pump = candidate.get("pump") or {}
    scope, values, lenses = candidate_pump_scope(pump, candidate["crystal"], design["resolved_inputs"])
    if scope is None:
        raise ValueError("这个候选缺少泵浦元件的有序坐标或薄透镜焦距。请在设计记录中补齐后重新计算。")
    try:
        values["pump_index"] = saved_pump_index(design)
    except ValueError:
        if scope == "propagation":
            raise
        values["pump_index"] = None
    propagated = scope == "propagation"
    basis = pump.get("launch_basis")
    radius, angle = candidate_launch(values, pump) if propagated or basis == "incomplete_second_moment" else (None, None)
    assumptions = ["采用候选方案中的轴向位置和薄透镜焦距。泵浦途经 M1 时忽略透射基底的光焦度。",
                   *pump.get("notes", [])]
    measured = basis in {"measured_second_moment", "incomplete_second_moment"}
    if propagated and measured:
        assumptions.append("按记录中的出光端束腰二阶矩半径和远场发散半角传播，元件布局取所选候选。")
    elif propagated:
        assumptions.append("本候选将标称纤芯和 NA 或存档源填充假设换算为等效半径、发散半角。")
    elif basis == "incomplete_second_moment":
        assumptions.append("已知的一项出射测量随几何保存，补齐另一项后才能传播束宽。")
    elif basis != "invalid_second_moment":
        assumptions.append("本候选保留几何布局，源端光束分布仍待测。")
    return _build_train(values, lenses, radius, angle, measured=measured, propagated=propagated,
                        quality=pump.get("estimated_m2"), assumptions=assumptions,
                        minimum=pump.get("minimum_inside_crystal"))


def _build_train(values, lenses, radius, angle, *, measured, propagated, quality, assumptions, minimum=None):
    wavelength = values.get("pump_wavelength_nm")
    computed = math.pi * radius * angle / (wavelength * 1e-6) if radius and angle and wavelength else None
    if propagated and (computed is None or (quality is None and measured)
                       or quality is not None and not math.isclose(quality, computed, rel_tol=2e-13)):
        raise ValueError("原设计的 M² 与源端束腰或发散半角不一致。请核对源端资料后重新计算。")
    if computed is not None and not 1 - 8 * math.ulp(1.) <= computed <= 10000:
        if propagated:
            raise ValueError("该记录的 M² 超出单程工作台支持的 1–10000，请核对源端半径、发散半角与波长。")
        assumptions.append("输入的束宽与发散角超出当前 M² 模型的范围，需要核对测量。")
        computed = None
    source_z, collimator, focus, front = (values[k] for k in PUMP_POSITIONS)
    if not source_z < collimator < focus < front:
        raise ValueError("原设计的坐标未按源端、准直镜、聚焦镜、晶体前表面递增。请核对安装位置。")
    thickness = values["crystal_length_mm"]
    crystal_uid = str(uuid4())
    elements = [
        air_gap("出光端到准直镜", collimator - source_z),
        {"name": "准直镜", "kind": "lens", "focal_mm": lenses["collimator"]["focal_mm"]},
        air_gap("准直镜到聚焦镜", focus - collimator),
        {"name": "聚焦镜", "kind": "lens", "focal_mm": lenses["focus_lens"]["focal_mm"]},
        air_gap("聚焦镜到晶体前表面", front - focus),
        {"name": "晶体", "kind": "space", "uid": crystal_uid, "length_mm": thickness, "n": values.get("pump_index")},
    ]
    parts = []
    for index, role, parameter, basis in (
        (1, "collimator", "focal_mm", lenses["collimator"]["basis"]),
        (3, "focus_lens", "focal_mm", lenses["focus_lens"]["basis"]),
        (5, "crystal", "length_mm", "设计记录中的晶体总长与泵浦波长处的有效折射率"),
    ):
        inventory_id = values.get(f"{role}_inventory_id")
        owned = values.get(f"{role}_owned") or inventory_id is not None
        if inventory_id is not None:
            basis += f"，原设计绑定库存 #{int(inventory_id)}"
        parts.append(Part(element_index=index, role=role, inventory_id=inventory_id, source=basis,
                          ownership="owned" if owned else "unspecified",
                          locked_parameters=["kind", parameter] if owned else []))

    planes = []
    if minimum:
        if not front <= minimum["z_mm"] <= front + thickness:
            raise ValueError("原设计记录的最小光斑截面位于晶体以外。请核对坐标并重新计算。")
        depth = min(thickness, minimum["z_mm"] - front)
        planes.append({"name": "原设计最小光斑截面", "uid": str(uuid4()), "target_uid": crystal_uid, "depth_mm": depth})
    notes = f"画布 z=0 位于泵浦出光端。该面在原设计中的坐标为 {source_z:g} mm（以 M1 为零点）。"
    if radius is not None:
        notes += f" 源面{'二阶矩' if measured else '情景等效'}半径为 {radius:g} mm。"
    if angle is not None:
        notes += f" 远场发散半角取 {angle * 1000:g} mrad。"
    return OpticalProject(
        title=f"{wavelength:g} nm 泵浦光路" if wavelength is not None else "泵浦光路",
        system={"mode": "single_pass", "wavelength_nm": wavelength, "elements": elements,
                "source": {"tangential": {"wavefront": "waist" if radius is not None else "unknown",
                                          "radius_mm": radius, "m2": max(1., computed) if computed is not None else None,
                                          "second_moment_radius": measured and radius is not None,
                                          "equivalent_radius": not measured and radius is not None},
                           "launch_ray": {"t_offset_mm": None, "s_offset_mm": None,
                                          "t_angle_mrad": None, "s_angle_mrad": None}}},
        parts=parts, observation_planes=planes, network=None, assemblies=[],
        assumptions=assumptions, notes=notes,
        component_context={"design_coordinates": {"reference": "M1", "origin_z_mm": source_z,
            "launch_radius_mm": radius, "launch_half_angle_rad": angle,
            "source_basis": ("unspecified" if radius is None and angle is None else
                             "measured_second_moment" if measured else "nominal_source_scenario")}},
    )

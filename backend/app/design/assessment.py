"""Numerical predictions supported by the supplied inputs of the linear design model."""
from __future__ import annotations

import math

from ..physics import beam
from .brief import FIELDS


def _gap_samples(values, rear):
    """Return the fixed spacing or the user's interval, including both endpoints."""
    fixed = values.get("output_gap_mm")
    lower = values.get("min_output_gap_mm", 0)
    limits = []
    if "max_output_gap_mm" in values:
        limits.append(values["max_output_gap_mm"])
    if "max_cavity_mm" in values:
        limits.append(values["max_cavity_mm"] - rear)
    upper = min(limits) if limits else fixed
    if upper is None:
        return [], None
    if upper < lower or fixed is not None and not lower <= fixed <= upper:
        return [], "固定距离或允许的搜索区间与晶体位置、最大腔长或装配间隙不相容。"
    if fixed is not None or upper == lower:
        return [fixed if fixed is not None else lower], None
    # Numerical probes, not new physical constraints. Preserve fractional endpoints.
    intervals = min(100, max(1, math.ceil(upper - lower)))
    return [lower + (upper - lower) * i / intervals for i in range(intervals)] + [upper], None


def _crystal_segments(values, inventory_segments):
    if inventory_segments:
        if any(s.get("length_mm") is None for s in inventory_segments):
            return []
        return [{"name": s.get("material") or s.get("role") or "介质段", "medium_role": s.get("role"),
                 "length_mm": s["length_mm"]} for s in inventory_segments]
    keys = ("front_cap_mm", "active_length_mm", "rear_cap_mm")
    lengths = [values.get(key) for key in keys]
    total = values["crystal_length_mm"]
    unknown = [i for i, length in enumerate(lengths) if length is None]
    residual = total - sum(length for length in lengths if length is not None)
    if len(unknown) == 1 and residual >= 0:
        lengths[unknown[0]] = residual
    elif math.isclose(residual, 0, abs_tol=1e-9):
        lengths = [0 if length is None else length for length in lengths]
    if any(length is None for length in lengths) or not math.isclose(sum(lengths), total, abs_tol=.01):
        return []
    return [{"name": name, "medium_role": role, "length_mm": length}
            for name, role, length in zip(("前端帽", "增益段", "后端帽"), ("undoped", "gain", "undoped"), lengths)
            if length > 0]


def _cold_cavity(values, inventory_segments):
    result = {"status": "needs_input", "rows": [], "missing": []}
    if values.get("cavity_type") not in {None, "linear"}:
        return {**result, "status": "unsupported", "summary": "此求解器仅处理两镜直线腔；可在通用工程中描述实际拓扑。"}
    missing = result["missing"]
    missing.extend(key for key in ("cavity_type", "crystal_front_mm", "crystal_length_mm",
                                  "crystal_index", "laser_wavelength_nm") if values.get(key) is None)
    for prefix in ("input", "output"):
        if values.get(f"{prefix}_mirror_flat") is not True and values.get(f"{prefix}_roc_mm") is None:
            missing.append(f"{prefix}_roc_mm")
    if all(values.get(key) is None for key in ("output_gap_mm", "max_output_gap_mm", "max_cavity_mm")):
        missing.append("output_gap_mm")
    if missing:
        names = {"input_roc_mm": "输入镜面形或曲率", "output_roc_mm": "输出镜面形或曲率",
                 "output_gap_mm": "晶体后端到 M2 的固定间隙或扫描上界"}
        required = "、".join(names.get(key) or FIELDS[key][0] for key in missing)
        result["summary"] = f"冷腔计算还缺{required}。"
        return result
    front, thickness = values["crystal_front_mm"], values["crystal_length_mm"]
    n, wavelength = values["crystal_index"], values["laser_wavelength_nm"]
    rear = front + thickness
    gaps, error = _gap_samples(values, rear)
    if error:
        return {**result, "status": "conflict", "summary": error}
    radii = [values.get(f"{prefix}_roc_mm") for prefix in ("input", "output")]
    mirrors = [beam.mirror_flat() if roc is None else beam.mirror_curved(roc) for roc in radii]
    result.update(status="computed", mode="fixed" if len(gaps) == 1 else "scan", geometry={
        "R1_mm": radii[0], "R2_mm": radii[1], "wavelength_nm": wavelength,
        "crystal": {"position_mm": front, "thickness_mm": thickness, "n": n},
        "basis": "工程条件及明确绑定的库存；扫描点仅为输入区间内的数值试算。",
        "scope": "轴对称近轴冷腔，空气间隙与均匀有效折射率、平行平面端面的晶体。观察面为晶体几何中点。未计孔径截断、增益和热透镜。"})
    segments = _crystal_segments(values, inventory_segments)
    if segments:
        result["geometry"]["crystal"]["segments"] = segments
    target = values.get("target_mode_radius_um")
    active_depth = None
    if target is not None and all(s["medium_role"] is not None for s in segments):
        start, gain_length = None, 0
        depth = 0
        for segment in segments:
            if segment["medium_role"] == "gain":
                if start is None:
                    start = depth
                gain_length += segment["length_mm"]
            depth += segment["length_mm"]
        if start is not None:
            active_depth = start + gain_length / 2
    for gap in gaps:
        # A plane-parallel slab in air has a reduced propagation distance t/n.
        propagation = beam.free_space(front + thickness / n + gap)
        matrix = beam.system_matrix([propagation, mirrors[1], propagation, mirrors[0]])
        stability = beam.stability(matrix)
        stable = stability["stable"] and not stability["marginal"]
        row = {"length_mm": rear + gap, "input_gap_mm": front, "output_gap_mm": gap,
               "stable": stable, "marginal": stability["marginal"], "stability_m": stability["m"],
               "evaluation_z_mm": front + thickness / 2, "mode_radius_mm": None}
        if stable:
            q = beam.cavity_mode_q(matrix)
            if q is not None:
                q_front = beam.propagate_q(beam.interface_flat(1, n), q + front)
                row["mode_radius_mm"] = beam.w_from_q(q_front + thickness / 2, wavelength, n)
                if active_depth is not None:
                    row["target_plane_radius_um"] = 1000 * beam.w_from_q(q_front + active_depth, wavelength, n)
                    row["target_error_um"] = abs(row["target_plane_radius_um"] - target)
        result["rows"].append(row)
    count = sum(row["stable"] for row in result["rows"])
    if len(gaps) == 1:
        row = result["rows"][0]
        condition = "处于稳定边界" if row["marginal"] else "不稳定"
        if row["mode_radius_mm"] is not None:
            condition = f"稳定，晶体几何中点模半径为 {row['mode_radius_mm'] * 1000:.3g} μm"
        result["summary"] = f"已复算固定的 {rear + gaps[0]:g} mm 冷腔，{condition}。"
    else:
        result["summary"] = f"在给定的 M2 间隙 {gaps[0]:g}–{gaps[-1]:g} mm 内计算了 {len(gaps)} 个采样点，其中 {count} 个稳定。"
    if target is not None:
        comparison = {"target_radius_um": target, "depth_from_crystal_front_mm": active_depth}
        errors = [row["target_error_um"] for row in result["rows"] if "target_error_um" in row]
        if errors:
            best = min(errors)
            comparison["minimum_error_um"] = best
            comparison["closest_rows"] = [
                number for number, row in enumerate(result["rows"], start=1)
                if "target_error_um" in row and math.isclose(row["target_error_um"], best, abs_tol=1e-10)
            ]
            selected = "、".join(f"#{number}" for number in comparison["closest_rows"])
            result["summary"] += f"最接近增益段中心目标半径 {target:g} μm 的试算点为 {selected}，最小差值为 {best:.3g} μm。"
        elif active_depth is None:
            result["summary"] += "已保留模半径目标；确定增益段中点的实际位置后才能比较。"
        result["target_comparison"] = comparison
    if any(s.get("uncertain") for s in inventory_segments):
        note = "所绑定库存包含待核验分段，本次几何按该记录的尺寸计算。"
        result["geometry"]["basis"] += note
        result["summary"] += note
    return result


PUMP_MEASUREMENTS = ("pump_exit_waist_radius_um", "pump_farfield_half_angle_mrad")


def pump_prediction(values, f1, f2, *, observation_z_mm=None):
    radius = values["pump_exit_waist_radius_um"] / 1000
    angle = values["pump_farfield_half_angle_mrad"] / 1000
    effective_wavelength = math.pi * radius * angle * 1e6
    actual_wavelength = values["pump_wavelength_nm"]
    quality = effective_wavelength / actual_wavelength
    if quality < 1 - 8 * math.ulp(1.):
        return None, "实测束腰半径与发散半角给出的 M² 小于 1，请核对半径／直径、半角／全角及测量平面。"
    # An exactly diffraction-limited launch can round one ulp below unity during unit conversions.
    quality = max(1., quality)
    effective_wavelength = quality * actual_wavelength
    z = values["source_z_mm"]
    front = values["crystal_front_mm"]
    rear = front + values["crystal_length_mm"]
    if not z < values["collimator_z_mm"] < values["focus_lens_z_mm"] < front:
        return None, "固定元件位置没有沿传播方向依次经过出光端、准直镜、聚焦镜与晶体。"
    n = values["pump_index"]
    q = complex(0, radius / angle)
    medium = 1
    rows = [{"z_mm": z, "w_mm": radius}]
    observed_radius = None
    for plane, transform, next_index in (
        (values["collimator_z_mm"], beam.thin_lens(f1), 1),
        (values["focus_lens_z_mm"], beam.thin_lens(f2), 1),
        (front, beam.interface_flat(1, n), n),
        (rear, beam.interface_flat(n, 1), 1),
    ):
        # Include each local waist exactly. The regular samples are only for drawing.
        offsets = {i * (plane - z) / 32 for i in range(1, 33)}
        if 0 < -q.real < plane - z:
            offsets.add(-q.real)
        if observation_z_mm is not None and z <= observation_z_mm <= plane:
            observed_radius = beam.w_from_q(q + (observation_z_mm - z), effective_wavelength, medium)
        for dz in sorted(offsets):
            rows.append({"z_mm": plane if dz == plane - z else z + dz,
                         "w_mm": beam.w_from_q(q + dz, effective_wavelength, medium)})
        q = beam.propagate_q(transform, q + (plane - z))
        z, medium = plane, next_index
    prediction = {"points": rows, "m2": quality,
                  "minimum_inside_crystal": min((row for row in rows if front <= row["z_mm"] <= rear),
                                                key=lambda row: row["w_mm"])}
    if observation_z_mm is not None:
        prediction["radius_at_observation_mm"] = observed_radius
    return prediction, None


def pump_lenses(values):
    """Record the thin-lens powers actually available to the fixed pump model."""
    lenses = {}
    if values.get("collimator_f_mm") is not None:
        lenses["collimator"] = {"focal_mm": values["collimator_f_mm"], "basis": "设计记录中的准直镜焦距"}
    elif values.get("collimator_roc_mm") and (values.get("collimator_index") or 0) > 1:
        radius, index = values["collimator_roc_mm"], values["collimator_index"]
        lenses["collimator"] = {"focal_mm": radius / (index - 1),
                                "basis": f"平凸薄透镜模型，泵浦波长下 R={radius:g} mm，n={index:g}，f=R/(n−1)"}
    if values.get("focus_f_mm") is not None:
        lenses["focus_lens"] = {"focal_mm": values["focus_f_mm"], "basis": "设计记录中的聚焦镜焦距"}
    return lenses


PUMP_POSITIONS = ("source_z_mm", "collimator_z_mm", "focus_lens_z_mm", "crystal_front_mm")


def positioned_train(values, lenses):
    coordinates = [values.get(key) for key in PUMP_POSITIONS]
    return (all(v is not None for v in coordinates) and all(a < b for a, b in zip(coordinates, coordinates[1:]))
            and values.get("crystal_length_mm") is not None
            and all((lenses.get(role) or {}).get("focal_mm") is not None for role in ("collimator", "focus_lens")))


def candidate_pump_scope(pump, crystal, values):
    positions = pump.get("positions") or {}
    inputs = {**values, "source_z_mm": positions.get("source"), "collimator_z_mm": positions.get("collimator"),
              "focus_lens_z_mm": positions.get("focus_lens"), "crystal_front_mm": crystal["position_mm"],
              "crystal_length_mm": crystal["thickness_mm"]}
    lenses = pump_lenses(inputs)
    for role, key in (("collimator", "collimator_f_mm"), ("focus_lens", "focus_f_mm")):
        if pump.get(key) is not None:
            lenses[role] = {"focal_mm": pump[key], "basis": "候选设计中用于传播的薄透镜焦距"}
    if pump.get("status") in {"no_fit", "needs_specs"} or not positioned_train(inputs, lenses):
        return None, inputs, lenses
    return ("propagation" if pump.get("points") else "geometry"), inputs, lenses


def _pump_imaging(values):
    scope = ("轴对称近轴二阶矩、空气中两片薄透镜与平面晶体端面。泵浦透过 M1 的基底、"
             "两面光焦度、像差和孔径衍射未计入，包络为该模型的条件预测。")
    lenses = pump_lenses(values)
    result = {"scope": scope, "status": "needs_input", "missing": [], "lenses": lenses}
    f1 = lenses.get("collimator", {}).get("focal_mm")
    f2 = lenses.get("focus_lens", {}).get("focal_mm")
    if f1 is None:
        result["missing"].append("collimator_f_mm")
    if f2 is None:
        result["missing"].append("focus_f_mm")
    if f1 is None and f2 is None:
        summary = "泵浦成像还缺准直镜和聚焦镜焦距。"
    elif f1 is None:
        summary = f"已知聚焦镜焦距 {f2:g} mm，计算共轭倍率还缺准直镜焦距。"
    elif f2 is None:
        summary = f"已知准直镜焦距 {f1:g} mm，计算共轭倍率还缺聚焦镜焦距。"
    else:
        result["magnification"] = f2 / f1
        result["status"] = "conditional"
        summary = f"若满足准直、聚焦共轭，两镜的几何成像倍率为 {f2 / f1:.3g}。"
        if values.get("fiber_core_um") is not None and values.get("pump_source") == "fiber":
            result["geometric_core_image_radius_mm"] = values["fiber_core_um"] / 2000 * f2 / f1
            summary += "标称芯像半径描述几何边界，不能直接作为泵浦光斑半径。"
    required = ("pump_wavelength_nm", "pump_exit_waist_radius_um", "pump_farfield_half_angle_mrad",
                "source_z_mm", "collimator_z_mm", "focus_lens_z_mm", "crystal_front_mm", "crystal_length_mm", "pump_index")
    result["missing"].extend(key for key in required if values.get(key) is None)
    if positioned_train(values, lenses):
        result["editable_geometry"] = "geometry"
    if not result["missing"]:
        prediction, error = pump_prediction(values, f1, f2)
        if error:
            return {**result, "status": "conflict", "summary": error}
        result.update(status="modeled", beam=prediction, editable_geometry="propagation")
        point = prediction["minimum_inside_crystal"]
        summary = f"按输入的二阶矩光束及固定元件位置传播，晶体内最小半径为 {point['w_mm'] * 1000:.3g} μm（z={point['z_mm']:.4g} mm）。"
    result["summary"] = summary
    return result


def assess_inputs(result, values, inventory_segments=()):
    cold = _cold_cavity(values, inventory_segments)
    pump = _pump_imaging(values)
    result["evidence_assessment"] = {"cold_cavity": cold, "pump_imaging": pump}
    result["threshold_scan"] = []
    questions = {}
    for part, label in ((cold, "冷腔"), (pump, "泵浦")):
        if part["status"] in {"conflict", "unsupported"}:
            result["issues"].append({"level": "blocking", "text": part["summary"]})
        for key in part["missing"]:
            questions.setdefault(key, []).append(label)
    alternatives = {
        "input_roc_mm": "请提供输入镜曲率，或注明为平面镜。",
        "output_roc_mm": "请提供输出镜曲率，或注明为平面镜。",
        "output_gap_mm": "请提供晶体后端面到 M2 的固定距离，或允许扫描的上界。",
    }
    result["questions"].extend({"key": key, "question": alternatives.get(key) or f"请补充{FIELDS[key][0]}。",
                                "why": f"用于计算{'、'.join(parts)}模型。"} for key, parts in questions.items())
    if cold["status"] == "computed" or pump["status"] in {"conditional", "modeled"}:
        result["status"] = "preliminary"
    elif cold["status"] == "unsupported":
        result["status"] = "unsupported"
    result["summary"] = cold["summary"] + pump["summary"]
    return result

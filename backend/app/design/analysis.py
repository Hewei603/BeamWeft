"""Read-only optical analysis of a saved design, evaluated by the ABCD kernel.

Plot samples and exact cursor values share the same piecewise q propagation.
Interfaces and thin lenses preserve beam width but can change q and curvature;
both sides of those boundaries are kept. Infinity is represented by JSON null.
"""
from __future__ import annotations

import math

import numpy as np

from ..physics import beam as B
from ..physics.design import _round_trip_with_lens
from .solver import _thermal_stability_m, mirror_radius


def _point(z, q, n, wavelength, segment):
    # The propagated distance is a difference of axial coordinates. At a waist
    # its rounding error can exceed the scale of the much smaller local q.
    if abs(q.real) < 64 * np.finfo(float).eps * max(1, abs(q), abs(z)):
        q = complex(0, q.imag)
    inverse_r = (1 / q).real
    return {"z_mm": float(z), "w_mm": B.w_from_q(q, wavelength, n),
            "R_mm": None if abs(inverse_r) < 1e-13 else float(1 / inverse_r),
            "inverse_R_per_mm": float(inverse_r), "q_real_mm": float(q.real),
            "q_imag_mm": float(q.imag), "n": float(n), "segment": segment}


def _segments(q, start, end, cuts, events, wavelength):
    """Build homogeneous segments and retain BEFORE/AFTER interface values."""
    points = sorted(set([start, end] + [z for z in cuts if start <= z <= end] + list(events)))
    segments, boundaries = [], []
    n = 1.0
    for i, z in enumerate(points):
        if i:
            q += z - points[i - 1]
        before = _point(z, q, n, wavelength, max(0, len(segments) - 1))
        for matrix, next_n in events.get(z, []):
            q = B.propagate_q(matrix, q)
            n = next_n
        after = _point(z, q, n, wavelength, len(segments))
        boundaries.append({"z_mm": z, "before": before, "after": after})
        if i < len(points) - 1 and points[i + 1] > z:
            segments.append({"start": z, "end": points[i + 1], "q": q, "n": n})
    return segments, boundaries


def _trace(segments, wavelength, samples):
    span = segments[-1]["end"] - segments[0]["start"]
    points, waists = [], []
    for index, segment in enumerate(segments):
        start, end, q, n = segment["start"], segment["end"], segment["q"], segment["n"]
        count = max(2, math.ceil(samples * (end - start) / span))
        zs = set(float(z) for z in np.linspace(start, end, count))
        waist_z = start - q.real
        if start - 1e-9 <= waist_z <= end + 1e-9:
            waist_z = min(end, max(start, waist_z))
            wp = _point(waist_z, complex(0, q.imag), n, wavelength, index)
            wp.update(z_R_mm=float(q.imag), divergence_half_mrad=wavelength * 1e-3 / (math.pi * n * wp["w_mm"]))
            if not any(abs(old["z_mm"] - waist_z) < 1e-9 and abs(old["n"] - n) < 1e-12 for old in waists):
                waists.append(wp)
            # Resolve a narrow pump focus even in a hundreds-of-mm global plot.
            for multiple in [-10, -5, -2, -1, -.5, -.25, -.1, -.05, 0, .05, .1, .25, .5, 1, 2, 5, 10]:
                z = waist_z + multiple * q.imag
                if start <= z <= end:
                    zs.add(float(z))
        for z in sorted(zs):
            points.append(_point(z, q + (z - start), n, wavelength, index))
    return {"points": points, "waists": waists,
            "minimum": min(points, key=lambda p: p["w_mm"]),
            "sample_count": len(points), "requested_samples": samples,
            "sampling": "分段采样，额外保留介质边界、薄透镜两侧及解析束腰附近；游标另做精确传播。"}


def _values(design):
    return design.get("resolved_inputs") or {k: f["value"] for k, f in design["facts"].items()}


def saved_pump_index(design):
    resolved = (design.get("resolved_inputs") or {}).get("pump_index")
    fact = (design.get("facts") or {}).get("pump_index") or {}
    if resolved is not None:
        values = [resolved]
    elif fact.get("value") is not None:
        values = [fact["value"]]
    else:
        # The legacy schema stored this scalar as a labelled assumption.
        values = [item.get("value") for item in design.get("assumptions", [])
                  if item.get("key") == "pump_index" or item.get("name") == "泵浦折射率"]
    indices = {v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)
               and math.isfinite(v) and v > 0}
    if len(indices) != 1:
        raise ValueError("该版设计没有唯一可复核的泵浦波长折射率。请补充该波长的折射率并另存设计后复算。")
    return indices.pop()


def candidate_launch(values, pump):
    if pump.get("launch_basis") is not None and pump.get("status") != "assumed_beam":
        return pump.get("launch_radius_mm"), pump.get("launch_half_angle_rad")
    # Older records used the nominal filled-core model, even if the brief also contained measurements.
    radius = values["fiber_core_um"] / 2000 if values.get("fiber_core_um") else None
    na = values.get("fiber_na")
    if pump.get("status") == "assumed_beam" and pump.get("points"):
        radius = radius or pump["points"][0].get("w_mm")
        na = na or pump.get("assumed_na")
    return radius, na


def _model(design, candidate, *, output_gap_mm=None, thermal_f_mm=None, mode="cavity"):
    v = _values(design)
    crystal = candidate["crystal"]
    front, total, n = crystal["position_mm"], crystal["thickness_mm"], crystal["n"]
    rear = front + total
    gap = candidate["length_mm"] - rear if output_gap_mm is None else output_gap_mm
    length = rear + gap
    if not .001 <= gap <= 2000 or (thermal_f_mm is not None and not 1 <= thermal_f_mm <= 1e7):
        raise ValueError("M2 间隙须为 0.001–2000 mm，热透镜焦距须为 1–10000000 mm。")
    active = next(p for p in candidate["positions"] if p["id"] == "active")
    cuts = [front, rear, active["z_mm"], active["end_z_mm"]]
    markers = [{"z_mm": 0, "label": "M1", "kind": "mirror"},
               {"z_mm": front, "label": "S1", "kind": "interface"},
               {"z_mm": active["z_mm"], "label": "增益段起点", "kind": "active"},
               {"z_mm": active["end_z_mm"], "label": "增益段终点", "kind": "active"},
               {"z_mm": rear, "label": "S2", "kind": "interface"}]
    common = {"output_gap_mm": gap, "length_mm": length, "crystal": crystal,
              "active_start_mm": active["z_mm"], "active_end_mm": active["end_z_mm"],
              "thermal_f_mm": thermal_f_mm, "markers": markers, "mode": mode,
              "planes": "T=S：当前正入射、轴对称标量模型；未模拟双折射走离或像散分离。"}
    if mode == "cavity":
        wavelength = v["laser_wavelength_nm"]
        matrix = _round_trip_with_lens(length, mirror_radius(candidate.get("R1_mm")), mirror_radius(candidate["R2_mm"]), crystal, thermal_f_mm)
        state = B.stability(matrix)
        common.update(wavelength_nm=wavelength, effective_wavelength_nm=wavelength, stability=state,
                      round_trip={"A": float(matrix[0, 0]), "B_mm": float(matrix[0, 1]), "C_per_mm": float(matrix[1, 0]),
                                  "D": float(matrix[1, 1]), "determinant": float(np.linalg.det(matrix))},
                      notes=["腔内传播方向 M1 → M2；q 和 R 使用当前介质的物理长度。热透镜焦距为晶体内集总模型参数。"])
        common["markers"] += [{"z_mm": length, "label": "M2", "kind": "mirror"}]
        q = B.cavity_mode_q(matrix)
        if q is None:
            return common, [], []
        events = {front: [(B.interface_flat(1, n), n)], rear: [(B.interface_flat(n, 1), 1)]}
        if thermal_f_mm:
            lens_z = front + total / 2
            events[lens_z] = [(B.thin_lens(thermal_f_mm), n)]
            common["markers"] += [{"z_mm": lens_z, "label": "热透镜", "kind": "lens"}]
        segments, boundaries = _segments(q, 0, length, cuts, events, wavelength)
    elif mode == "pump":
        pump = candidate["pump"]
        if pump.get("launch_error"):
            raise ValueError(pump["launch_error"])
        positions = pump.get("positions", {})
        f1, f2 = pump.get("collimator_f_mm"), pump.get("focus_f_mm")
        if not f1 or not f2 or not all(k in positions for k in ["source", "collimator", "focus_lens"]):
            raise ValueError("此版本尚无完整泵浦几何，请先更新设计。")
        wavelength = v["pump_wavelength_nm"]
        npump = saved_pump_index(design)
        radius, na = candidate_launch(v, pump)
        if radius is None or na is None:
            raise ValueError("计算泵浦包络需要出射尺寸和角分布。可补充测量，或另存明确的源填充假设后试算。")
        effective = math.pi * radius * na * 1e6
        if effective / wavelength < 1 - 8 * math.ulp(1.):
            raise ValueError("给定的束腰半径和发散半角对应 M² 小于 1，请核对口径和单位。")
        effective = max(effective, wavelength)
        measured = pump.get("launch_basis") == "measured_second_moment"
        basis_note = "泵浦出射面为束腰，采用实测二阶矩半径及发散半角。" if measured else "此场景采用名义芯径与 NA 填满的近似。"
        common.update(wavelength_nm=wavelength, effective_wavelength_nm=effective, thermal_f_mm=None,
                      stability=None, round_trip=None, estimated_M2=effective / wavelength,
                      launch_basis=pump.get("launch_basis") or "filled_fiber",
                      notes=pump.get("notes", []) + [basis_note + "传播采用等效 M² 的轴对称近轴模型，热透镜选择只作用于腔模分析。"])
        common["markers"] += [{"z_mm": positions[key], "label": label, "kind": "lens" if key != "source" else "source"}
                              for key, label in [("source", "泵浦出射面"), ("collimator", "准直镜"), ("focus_lens", "聚焦镜")]]
        focus_z = pump.get("focus_z_mm", candidate["active_center_mm"])
        common["markers"] += [{"z_mm": focus_z, "label": "名义焦点", "kind": "focus"}]
        cuts += [0, focus_z]
        events = {positions["collimator"]: [(B.thin_lens(f1), 1)], positions["focus_lens"]: [(B.thin_lens(f2), 1)],
                  front: [(B.interface_flat(1, npump), npump)], rear: [(B.interface_flat(npump, 1), 1)]}
        segments, boundaries = _segments(complex(0, radius / na), positions["source"], rear, cuts, events, effective)
        common["pump_index"] = npump
    else:
        raise ValueError("未知分析对象")
    common["markers"].sort(key=lambda m: m["z_mm"])
    return common, segments, boundaries


def probe_design(design, candidate, *, z_mm, **kwargs):
    common, segments, _ = _model(design, candidate, **kwargs)
    if not segments:
        return {"point": None, "stable": False, "note": "当前参数下没有稳定自洽腔模。"}
    if not segments[0]["start"] <= z_mm <= segments[-1]["end"]:
        raise ValueError("游标位置不在当前光路范围内。")
    # At an interface return the downstream side, except the final exit plane.
    index = next((i for i, seg in enumerate(segments) if seg["start"] <= z_mm < seg["end"]), len(segments) - 1)
    seg = segments[index]
    point = _point(z_mm, seg["q"] + (z_mm - seg["start"]), seg["n"], common["effective_wavelength_nm"], index)
    return {"point": point, "stable": True, "method": "ABCD 精确传播；非绘图采样点插值。", "side": "介质边界取下游侧；终点取腔内侧。"}


def analyze_design(design, candidate, *, samples=900, scan_samples=600, **kwargs):
    common, segments, boundaries = _model(design, candidate, **kwargs)
    trace = _trace(segments, common["effective_wavelength_nm"], samples) if segments else {
        "points": [], "waists": [], "minimum": None, "sample_count": 0, "requested_samples": samples}
    common.update(trace=trace, boundaries=boundaries)
    if segments:
        common["z_range_mm"] = [segments[0]["start"], segments[-1]["end"]]
    else:
        common["z_range_mm"] = [0, common["length_mm"]]
    if common["mode"] == "cavity":
        if common["thermal_f_mm"] is not None:
            _, cold_segments, _ = _model(design, candidate, output_gap_mm=common["output_gap_mm"])
            common["cold_reference"] = _trace(cold_segments, common["wavelength_nm"], samples) if cold_segments else None
        crystal, roc, r1 = candidate["crystal"], candidate["R2_mm"], candidate.get("R1_mm")
        front, total, n = crystal["position_mm"], crystal["thickness_mm"], crystal["n"]
        upper = max(0, min(mirror_radius(roc), mirror_radius(r1)) - front - total / n)
        low = float(design.get("resolved_inputs", {}).get("min_output_gap_mm", 5))
        high = min(2000, max(common["output_gap_mm"] + 10, upper + 20))
        fs = [None, 500, 200, 100, 50]
        if common["thermal_f_mm"] not in fs:
            fs.append(common["thermal_f_mm"])
        gaps = sorted(set(float(g) for g in np.linspace(min(low, common["output_gap_mm"]), high, scan_samples)) | {common["output_gap_mm"]})
        common["scan"] = {"cold_upper_exclusive_mm": upper if r1 is None or roc is None else None, "series": [
            {"f_mm": f, "points": [{"gap_mm": g, "m": _thermal_stability_m(front + total + g, roc, crystal, f, r1)} for g in gaps]} for f in fs],
            "range_mm": [gaps[0], gaps[-1]], "step_mm": (gaps[-1] - gaps[0]) / (scan_samples - 1)}
    return common

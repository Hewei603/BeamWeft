"""Workflow-facing adapters over the physics kernel.

These functions take a plain config dict and return a plain result dict using
the case-module compute convention (``status`` in ``needs_input`` | ``failed`` |
``completed``), so both the REST module runner and the Agent tool layer can call
them without duplicating logic.  Still pure: numpy + this package only.

Tools:

- :func:`run_cavity_design`  — analyze a two-mirror cavity (optionally with an
  intracavity crystal) at a fixed length, or scan the mirror spacing to find
  stable windows and recommend a length: the "where do the elements go" tool.
- :func:`run_phase_match`    — phase-matching angles / walk-off for SHG/SFG.
- :func:`run_coating_tmm`    — re-export of :func:`coating_tool.evaluate_coating`.
"""
from __future__ import annotations

from typing import Any
from math import ceil, isfinite, ulp

from . import beam as B
from . import nonlinear as NL
from .coating_tool import evaluate_coating

run_coating_tmm = evaluate_coating

_FLAT_ROC_MM = 1e15  # numerically-flat mirror


def _cavity_mirror(radius):
    return B.mirror_flat() if radius >= _FLAT_ROC_MM else B.mirror_curved(radius)


def _roc(value: Any) -> float | None:
    """Parse a mirror ROC config value: number (mm), 'flat'/'inf'/None -> flat."""
    if value is None:
        return _FLAT_ROC_MM
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("flat", "inf", "infinite", "plane", "平镜", ""):
            return _FLAT_ROC_MM
        try:
            parsed = float(v)
            return parsed if isfinite(parsed) and parsed != 0 else None
        except ValueError:
            return None
    try:
        parsed = float(value)
        return parsed if not isinstance(value, bool) and isfinite(parsed) and parsed != 0 else None
    except (TypeError, ValueError):
        return None


def _cavity_elements(l_mm: float, r2_mm: float, r1_mm: float, crystal: dict | None):
    """Forward path M1 -> M2 (+ its reverse and mirrors) as one round-trip list.

    Returns ``(round_trip_elements, forward_named)`` where ``forward_named`` is a
    list of :class:`beam.Element` used to trace spot sizes at each plane.
    """
    if not crystal:
        fwd = [B.Element("M1 -> M2", B.free_space(l_mm), 1.0, l_mm)]
        rev = [B.Element("M2 -> M1", B.free_space(l_mm), 1.0)]
    else:
        n = float(crystal["n"])
        t = float(crystal["thickness_mm"])
        pos = float(crystal.get("position_mm", (l_mm - t) / 2.0))
        air2 = l_mm - pos - t
        if pos < 0 or air2 < 0:
            raise ValueError(f"crystal (position {pos} mm, thickness {t} mm) does not fit in L={l_mm} mm")
        fwd = [
            B.Element("M1 -> crystal", B.free_space(pos), 1.0, pos),
            B.Element("crystal entrance", B.interface_flat(1.0, n), n),
            B.Element("crystal center", B.free_space(t / 2.0), n, t / 2.0),
            B.Element("crystal second half", B.free_space(t / 2.0), n, t / 2.0),
            B.Element("crystal exit", B.interface_flat(n, 1.0), 1.0),
            B.Element("crystal -> M2", B.free_space(air2), 1.0, air2),
        ]
        rev = [
            B.Element("M2 -> crystal", B.free_space(air2), 1.0),
            B.Element("crystal entrance (return)", B.interface_flat(1.0, n), n),
            B.Element("crystal (return)", B.free_space(t), n),
            B.Element("crystal exit (return)", B.interface_flat(n, 1.0), 1.0),
            B.Element("crystal -> M1", B.free_space(pos), 1.0),
        ]
    round_trip = [el.M for el in fwd] + [_cavity_mirror(r2_mm)] + [el.M for el in rev] + [_cavity_mirror(r1_mm)]
    return round_trip, fwd


def beam_envelope(
    l_mm: float,
    r1_mm: float,
    r2_mm: float,
    wavelength_nm: float,
    crystal: dict | None,
    samples: int = 180,
) -> dict[str, Any]:
    """Sample the cold-cavity eigenmode radius w(z), z PHYSICAL in mm from M1.

    Exists so a layout drawing is rendered from computed physics instead of from
    a Gaussian formula reimplemented in the UI: the caller only maps mm to pixels.

    Two things this deliberately does not do:

    * It does not reduce the crystal to :func:`beam.slab`.  A slab collapses the
      medium to an equivalent air length, which destroys the physical z axis and
      hides that the beam diverges more slowly inside the crystal.  Instead it
      walks the very elements :func:`_cavity_elements` builds — one source of
      geometry, so the drawing cannot drift from the analysis.
    * It does not report :func:`beam.waist_from_q`'s distance as the waist
      position.  With a crystal in the path that distance is a reduced
      (optical-path) length, off by ``t(1 - 1/n)``.  The waist is instead solved
      per segment from ``Re(q) = 0``, which is exact and independent of the
      sampling step, and can legitimately land inside the crystal.

    A located waist includes either mirror plane. A convex mirror in a stable
    two-mirror cavity gives an extrapolated waist outside the physical path.
    Older archived envelopes can contain a null waist without this distinction.
    """
    round_trip, fwd = _cavity_elements(l_mm, r2_mm, r1_mm, crystal)
    matrix = B.system_matrix(round_trip)
    q = B.cavity_mode_q(matrix)
    if q is None:
        if B.stability(matrix)["stable"]:
            return {"points": [], "waist": None, "waist_location": "unresolved",
                    "note": "往返矩阵在数值退化附近，本次未能确定自洽基模包络。"}
        return {"points": [], "waist": None, "waist_location": "no_confined_mode",
                "note": "该腔长不稳定或处于临界条件，无法确定受限基模包络。"}

    span = sum(el.length_mm for el in fwd) or float(l_mm)
    step = max(span / max(int(samples), 8), 1e-6)

    z = 0.0
    points = [{"z_mm": 0.0, "w_mm": round(B.w_from_q(q, wavelength_nm, 1.0), 6), "n": 1.0}]
    waist: dict[str, float] | None = None
    propagation = [el for el in fwd if el.length_mm > 0]
    exterior = r1_mm < 0 or r2_mm < 0

    for el in fwd:
        if el.length_mm <= 0:
            # Thin element (interface / mirror): acts in place, z unchanged.
            # w is continuous across a flat interface, so no point is emitted —
            # a second point at the same z would only add a redundant vertex.
            q = B.propagate_q(el.M, q)
            continue

        # A flat end mirror requires a plane wavefront there. Use that boundary
        # condition directly: round-off in the round-trip root can put Re(q)
        # just to the wrong side. A convex mirror's virtual waist stays outside.
        dz_star = -q.real
        if el is propagation[0] and r1_mm >= _FLAT_ROC_MM:
            dz_star = 0.0
        elif el is propagation[-1] and r2_mm >= _FLAT_ROC_MM:
            dz_star = el.length_mm
        # Interior interfaces use physical lengths and a few float spacings for
        # the ABCD arithmetic. Neither the plotted sample pitch nor the number
        # of displayed decimals determines whether a waist is in the cavity.
        edge_roundoff = 8 * max(ulp(span), ulp(dz_star))
        if not exterior and -edge_roundoff <= dz_star <= el.length_mm + edge_roundoff:
            dz_star = min(max(dz_star, 0.0), el.length_mm)
            q_waist = q + dz_star
            w0 = B.w_from_q(q_waist, wavelength_nm, el.n_after)
            if waist is None or w0 < waist["w0_mm"]:
                waist = {"z_mm": round(z + dz_star, 4), "w0_mm": round(w0, 6),
                         "in_medium_n": el.n_after}

        # Within each homogeneous segment q(dz) = q(0) + dz. Reference every
        # sample to the same start so changing the point count cannot move a face.
        count = max(1, ceil(el.length_mm / step))
        for i in range(1, count + 1):
            dz = el.length_mm if i == count else i * step
            points.append({
                "z_mm": round(z + dz, 4),
                "w_mm": round(B.w_from_q(q + dz, wavelength_nm, el.n_after), 6),
                "n": el.n_after,
            })
        q += el.length_mm
        z += el.length_mm

    return {
        "points": points,
        "waist": waist,
        "waist_location": "inside" if waist is not None else "outside" if exterior else "unresolved",
        "note": "冷腔基模包络(未包含热透镜);z 为距 M1 的物理距离,w 为光斑半径。",
    }


def _analyze_length(l_mm: float, r1_mm: float, r2_mm: float, wavelength_nm: float, crystal: dict | None) -> dict:
    """Full mode analysis of one cavity geometry.  Raises ValueError on bad geometry."""
    round_trip, fwd = _cavity_elements(l_mm, r2_mm, r1_mm, crystal)
    m_rt = B.system_matrix(round_trip)
    st = B.stability(m_rt)
    out: dict[str, Any] = {
        "length_mm": float(l_mm),
        "stability_m": round(st["m"], 6),
        "stable": st["stable"],
    }
    q1 = B.cavity_mode_q(m_rt)
    if q1 is None:
        return out
    out["w_on_mirror1_mm"] = round(B.w_from_q(q1, wavelength_nm), 5)
    trace = B.trace_beam(fwd, q1, wavelength_nm)
    out["spots"] = [{"at": t["name"], "w_mm": round(t["w_mm"], 5)} for t in trace]
    out["w_on_mirror2_mm"] = round(trace[-1]["w_mm"], 5)
    w0, dist = B.waist_from_q(q1, wavelength_nm)
    out["waist_w0_mm"] = round(w0, 5)
    out["waist_position_note"] = (
        "waist size is exact; its position is measured from M1 in reduced (optical-path) "
        "units when a crystal is present" if crystal else "waist position measured from M1"
    )
    out["waist_from_mirror1_mm"] = round(dist, 3)
    if crystal:
        center = next((t for t in trace if t["name"] == "crystal center"), None)
        if center:
            out["w_in_crystal_mm"] = round(center["w_mm"], 5)
    return out


def run_cavity_design(config: dict[str, Any]) -> dict[str, Any]:
    """Analyze or design a linear two-mirror cavity.

    Config keys:
      ``wavelength_nm`` (default 1064), ``R1_mm``, ``R2_mm`` (number | 'flat'),
      one of ``L_mm`` (analyze) or ``L_scan_mm`` = {start, stop, step} (design),
      optional ``crystal`` = {n | material-free index, thickness_mm, position_mm}
      and ``target_waist_mm`` (scan recommendation criterion).
    """
    wavelength_nm = float(config.get("wavelength_nm", 1064.0))
    # Nothing geometric at all means the user has not started yet — tell them
    # exactly which fields to fill instead of failing on an implicit default.
    if all(config.get(k) is None for k in ("R1_mm", "R2_mm", "L_mm", "L_scan_mm")):
        return {"status": "needs_input",
                "message": "请先提供腔镜曲率:在案例的物理参数里填写 R1_mm / R2_mm"
                           "(单位 mm,平镜填 flat),腔长留空时会自动扫描 30–800 mm。"}
    r1 = _roc(config.get("R1_mm"))
    r2 = _roc(config.get("R2_mm"))
    if r1 is None or r2 is None:
        return {"status": "needs_input",
                "message": "腔镜曲率无法解析:R1_mm / R2_mm 请填数字(单位 mm)或 flat(平镜)。"}
    crystal = config.get("crystal") or None
    if crystal is not None and not isinstance(crystal, dict):
        # A case's free-form entry can put a string here (e.g. "lbo"). A string
        # crystal belongs to the phase-match tool; here it must be a slab spec.
        return {"status": "needs_input",
                "message": "crystal 参数需为字典 {\"n\": 折射率, \"thickness_mm\": 厚度, "
                           "\"position_mm\": 距M1位置}。如果你填的是倍频晶体名(如 lbo),"
                           "请改用相位匹配模块的 crystal 字段。"}
    if crystal:
        if "n" not in crystal or "thickness_mm" not in crystal:
            return {"status": "needs_input",
                    "message": "crystal 字典缺少 'n'(折射率)或 'thickness_mm'(厚度 mm)。"}
        try:
            n, thickness = float(crystal["n"]), float(crystal["thickness_mm"])
            if not (isfinite(n) and n > 0 and isfinite(thickness) and thickness >= 0):
                raise ValueError
            if "position_mm" in crystal and not (0 <= float(crystal["position_mm"]) < float("inf")):
                raise ValueError
        except (TypeError, ValueError):
            return {"status": "needs_input", "message": "请核对晶体折射率、厚度与位置。折射率须为正，长度须为非负有限值（mm）。"}
    # Imported here because the design search shares the matrix routines above.
    from .design import thermal_lens_margin

    base = {
        "status": "completed",
        "tool": "cavity_design",
        "wavelength_nm": wavelength_nm,
        "geometry": {
            "R1_mm": None if r1 >= _FLAT_ROC_MM else r1,
            "R2_mm": None if r2 >= _FLAT_ROC_MM else r2,
            "crystal": crystal,
        },
        "assumptions": [
            "理想对准的线性腔,近轴基模",
            "腔镜正入射(不含像散)",
            "晶体按平行平板建模(未配置热透镜时不含热透镜)",
        ],
        "disclaimer": "确定性 ABCD/高斯光束计算。高功率运行前需实验验证。",
    }

    if config.get("L_mm") is not None:
        l_mm = float(config["L_mm"])
        try:
            margin = thermal_lens_margin(l_mm, r1, r2, crystal)
            analysis = _analyze_length(l_mm, r1, r2, wavelength_nm, crystal)
        except ValueError as exc:
            return {"status": "failed", "message": str(exc)}
        base["analysis"] = {**analysis, **margin}
        base["placement"] = _placement(l_mm, crystal)
        # Sampled only for the single geometry being reported — never inside the
        # scan loop, which evaluates thousands of lengths.
        base["beam_envelope"] = beam_envelope(l_mm, r1, r2, wavelength_nm, crystal)
        base["summary"] = _summary_line(analysis, wavelength_nm)
        return base

    # Mirrors given but no length: scan the standard range instead of refusing,
    # matching the plan path's default (case_context) and the guide's promise
    # that module parameters may be left empty.
    scan = config.get("L_scan_mm") or {"start": 30.0, "stop": 800.0, "step": 5.0}

    start, stop = float(scan["start"]), float(scan["stop"])
    step = float(scan.get("step", max((stop - start) / 200.0, 0.1)))
    if not (stop > start and step > 0):
        return {"status": "failed", "message": "L_scan_mm 需满足 stop > start 且 step > 0。"}

    target = config.get("target_waist_mm")
    target = float(target) if target is not None else None

    points = []
    l_mm = start
    while l_mm <= stop + 1e-9:
        try:
            points.append(_analyze_length(l_mm, r1, r2, wavelength_nm, crystal))
        except ValueError:
            points.append({"length_mm": round(l_mm, 4), "stable": False, "stability_m": None})
        l_mm += step

    stable_pts = [p for p in points if p.get("stable") and "waist_w0_mm" in p]
    windows = _stable_windows(points)
    recommended = None
    if stable_pts:
        if target is not None:
            recommended = min(stable_pts, key=lambda p: abs(p["waist_w0_mm"] - target))
            margin = thermal_lens_margin(recommended["length_mm"], r1, r2, crystal)
            criterion = f"束腰最接近目标 {target} mm"
        else:
            candidates = ((thermal_lens_margin(p["length_mm"], r1, r2, crystal), p) for p in stable_pts)
            margin, recommended = max(candidates, key=lambda item: (
                item[0]["thermal_lens_tolerance_dioptre"], item[0]["thermal_lens_limit_kind"] == "ceiling",
                -abs(item[1]["stability_m"])))
            criterion = "在已扫描的腔长中，优先选取从冷腔起具有最大连续正热透镜裕度的设定（比较至 10 D）。"
        base["recommended"] = {
            "criterion": criterion,
            **recommended,
            **margin,
        }
        base["placement"] = _placement(recommended["length_mm"], crystal)
        base["beam_envelope"] = beam_envelope(
            recommended["length_mm"], r1, r2, wavelength_nm, crystal
        )

    base["scan"] = {
        "L_range_mm": [start, stop],
        "step_mm": step,
        "stable_windows_mm": windows,
        "n_points": len(points),
        # keep the table compact: at most ~60 rows for display
        "points": points[:: max(1, len(points) // 60)],
    }
    base["summary"] = f"扫描腔长 {start:g} 至 {stop:g} mm，得到 {len(windows)} 段连续达标的采样位置。"
    if recommended:
        power = margin["thermal_lens_tolerance_dioptre"]
        endpoint = (f"计算至 {power:.6g} D 仍稳定。" if margin["thermal_lens_limit_kind"] == "ceiling"
                    else f"首个热透镜临界值约为 {power:.6g} D。")
        base["summary"] += f"推荐腔长 {recommended['length_mm']:g} mm，束腰半径 {recommended['waist_w0_mm']:g} mm。{endpoint}"
    else:
        base["summary"] += "在已采样的腔长中没有找到稳定基模，可核对腔镜曲率或改变扫描范围。"
    return base


def _placement(l_mm: float, crystal: dict | None) -> list[dict[str, Any]]:
    placement = [{"element": "Mirror M1", "position_mm": 0.0}]
    if crystal:
        pos = float(crystal.get("position_mm", (l_mm - float(crystal["thickness_mm"])) / 2.0))
        placement.append({"element": "Crystal entrance", "position_mm": round(pos, 3)})
        placement.append({"element": "Crystal exit", "position_mm": round(pos + float(crystal["thickness_mm"]), 3)})
    placement.append({"element": "Mirror M2", "position_mm": round(l_mm, 3)})
    return placement


def _summary_line(analysis: dict, wavelength_nm: float) -> str:
    if not analysis.get("stable"):
        return (f"该腔在 L={analysis['length_mm']} mm 不稳定(m={analysis.get('stability_m')}),"
                "当前近轴冷腔模型没有稳定受限基模。可扫描腔长或腔镜曲率，再用相应的增益与损耗模型评估起振条件。")
    return (
        f"L={analysis['length_mm']} mm 稳定(m={analysis['stability_m']}):"
        f"w0={analysis.get('waist_w0_mm')} mm,镜面光斑 w(M1)={analysis.get('w_on_mirror1_mm')} mm、"
        f"w(M2)={analysis.get('w_on_mirror2_mm')} mm(@{wavelength_nm} nm)。"
    )


def _stable_windows(points: list[dict]) -> list[list[float]]:
    windows: list[list[float]] = []
    open_start: float | None = None
    for p in points:
        if p.get("stable"):
            if open_start is None:
                open_start = p["length_mm"]
            last = p["length_mm"]
        else:
            if open_start is not None:
                windows.append([open_start, last])
                open_start = None
    if open_start is not None:
        windows.append([open_start, last])
    return [[round(a, 3), round(b, 3)] for a, b in windows]


def run_phase_match(config: dict[str, Any]) -> dict[str, Any]:
    """Phase-matching solve for SHG/SFG.

    Config keys: ``crystal`` (bbo|lbo|ktp|bibo), ``lambda1_nm``, optional
    ``lambda2_nm`` (SFG), ``pm_type`` ('I'|'II', default 'I'), optional
    ``planes`` for biaxial (default all three principal planes).
    """
    # Accept the case-parameter names as aliases: a case describes its
    # nonlinear stage as nonlinear_crystal / wavelength_nm / shg_pm_type, and
    # its `crystal` key is the GAIN slab dict — which must not be mistaken for
    # the nonlinear crystal name here.
    raw_crystal = config.get("crystal")
    if not isinstance(raw_crystal, str) or not raw_crystal.strip():
        raw_crystal = config.get("nonlinear_crystal") or config.get("shg_crystal") or ""
    crystal = str(raw_crystal).strip().lower()
    if not crystal:
        return {"status": "needs_input",
                "message": "请提供倍频晶体名 crystal(可选:bbo、lbo、ktp、bibo),"
                           "或在案例物理参数里填写「倍频晶体」。"}
    lambda1_raw = config.get("lambda1_nm", config.get("wavelength_nm"))
    try:
        lambda1 = float(lambda1_raw)
    except (TypeError, ValueError):
        return {"status": "needs_input",
                "message": "请提供基频波长 lambda1_nm(单位 nm,如 1064),"
                           "或在案例物理参数里填写「激光波长」。"}
    lambda2 = config.get("lambda2_nm")
    lambda2 = float(lambda2) if lambda2 is not None else None
    pm_type = str(config.get("pm_type") or config.get("shg_pm_type") or "I").upper()

    solutions = []
    try:
        if crystal in ("bbo",):
            pm = NL.phase_match_uniaxial(crystal, lambda1, lambda2, pm_type=pm_type)
            solutions.append(pm)
        else:
            planes = config.get("planes") or ["xy", "yz", "xz"]
            for plane in planes:
                solutions.append(NL.phase_match_biaxial_plane(crystal, plane, lambda1, lambda2, pm_type=pm_type))
    except KeyError as exc:
        return {"status": "failed",
                "message": f"不支持的晶体或配置:{exc}。当前支持 bbo、lbo、ktp、bibo。"}
    except ValueError as exc:
        return {"status": "failed",
                "message": f"相位匹配求解失败:{exc}。请检查波长与匹配类型(I/II)。"}

    found = []
    for pm in solutions:
        entry = {
            "crystal": pm.crystal,
            "process": pm.process,
            "type": pm.type,
            "plane": pm.plane,
            "theta_deg": None if pm.theta_deg is None else round(pm.theta_deg, 2),
            "phi_deg": None if pm.phi_deg is None else round(pm.phi_deg, 2),
            "walkoff_mrad": None if pm.walkoff_mrad is None else round(pm.walkoff_mrad, 2),
            "lambda1_nm": pm.lambda1_nm,
            "lambda2_nm": pm.lambda2_nm,
            "lambda3_nm": round(pm.lambda3_nm, 2),
            "confidence": pm.confidence,
            "notes": pm.notes,
        }
        found.append(entry)

    matched = [f for f in found if f["theta_deg"] is not None]
    return {
        "status": "completed",
        "tool": "phase_match",
        "solutions": found,
        "matched": len(matched),
        "summary": (
            f"{crystal.upper()} type-{pm_type} {'倍频(SHG)' if lambda2 is None else '和频(SFG)'}"
            f" @ {lambda1} nm:共 {len(matched)} 个相位匹配解。"
            if matched else
            f"{crystal.upper()} type-{pm_type} 在所检索主平面内没有角度相位匹配解——"
            "请尝试另一匹配类型或更换晶体。"
        ),
        "disclaimer": (
            "切角由内置 Sellmeier 色散数据计算;双轴晶体采用主平面近似(置信度「中」)。"
            "订购晶体前务必与厂商核实切角。"
        ),
    }

"""Dual-mode coating evaluation — the single entrypoint for all coating questions.

``evaluate_coating(request)`` answers "what does this coating do at these
wavelengths" honestly, choosing one of three modes:

- **exact_stack**  : two specified media and an ordered layer stack -> forward TMM
  within the supplied index models' wavelength ranges.
- **vendor_curve** : a digitized/measured (lambda, R/T) curve -> interpolation,
  ``confidence='high'`` (it *is* the spec'd curve).
- **archetype**    : only a nominal label ``{function, design_wavelength_nm}`` ->
  a representative stopband estimate, ``confidence='low'`` +
  ``recommend_measurement=True``.  The inverse problem is unsolvable; we return
  an interval and categorical band position, never a fake point value that L1
  could mistake for the real curve.

The result dict matches the case-module compute convention (``status`` in
``needs_input`` | ``failed`` | ``completed``) so the API adapter is a thin
unpacker.  This module imports only numpy + the physics package — no web/DB/LLM.
"""
from __future__ import annotations

import math

import numpy as np

from .archetypes import archetype_stopband_estimate
from .materials import MaterialError, index_info, load_materials, resolve_material
from .tmm import Layer, Stack, solve_stack


def _interp_curve(curve: dict, wavelengths: list[float], key: str) -> list[float | None]:
    xs = np.asarray(curve.get("wl_nm", []), dtype=float)
    ys = curve.get(key)
    if ys is None or len(xs) == 0:
        return [None] * len(wavelengths)
    ys = np.asarray(ys, dtype=float)
    order = np.argsort(xs)
    xs, ys = xs[order], ys[order]
    out: list[float | None] = []
    for w in wavelengths:
        if w < xs[0] or w > xs[-1]:
            out.append(None)  # outside measured range: honest null, no extrapolation
        else:
            val = float(np.interp(w, xs, ys))
            out.append(None if math.isnan(val) else val)  # NaN in the source curve -> honest null
    return out


def _stack_medium(value, label, sources, missing):
    """Resolve a supplied medium without converting an unknown name into a different material."""
    if value is None or value == "":
        missing.append(f"请填写{label}的材料或工作波长下的折射率。")
        return None
    if isinstance(value, str):
        try:
            key = resolve_material(value)
        except MaterialError:
            missing.append(f"{label}「{value}」没有可用的色散模型。请提供折射率，或选择已有测量曲线。")
            return None
        sources.setdefault(key, []).append(label)
        return key
    if isinstance(value, dict):
        real_index = value.get("n")
        if real_index is None:
            missing.append(f"请填写{label}的折射率 n。")
            return None
        if isinstance(real_index, bool):
            raise ValueError(f"{label}的折射率需要数值。")
        index = complex(real_index)
        if value.get("k") is not None:
            extinction = float(value["k"])
            if index.imag != 0 or not math.isfinite(extinction) or extinction < 0:
                raise ValueError(f"{label}请使用实数 n 和非负消光系数 k，或单个复折射率 n − ik。")
            index = complex(index.real, -extinction)
    elif isinstance(value, bool) or not isinstance(value, (int, float, complex)):
        raise ValueError(f"{label}需要材料名称或数值折射率。")
    else:
        index = complex(value)
    if not (math.isfinite(index.real) and index.real > 0 and math.isfinite(index.imag) and index.imag <= 0):
        raise ValueError(f"{label}的折射率需有限、实部为正；被动介质采用 n − ik，k ≥ 0。")
    return index


def _read_stack(stack_spec):
    """Return either the user's optical prescription or the list of missing physical inputs."""
    if not isinstance(stack_spec, dict):
        raise ValueError("膜系 stack 需要包含边界介质与有序膜层。")
    sources, missing = {}, []
    incident = _stack_medium(stack_spec.get("incident"), "入射介质", sources, missing)
    substrate = _stack_medium(stack_spec.get("substrate"), "基底介质", sources, missing)
    raw_layers = stack_spec.get("layers")
    if raw_layers is None:
        missing.append("请填写有序膜层 layers；明确为裸界面时可填空数组 []。")
        raw_layers = []
    if not isinstance(raw_layers, list):
        raise ValueError("layers 需要按入射顺序列出各膜层。")
    layers = []
    for i, layer in enumerate(raw_layers, 1):
        label = f"第 {i} 层"
        if not isinstance(layer, dict):
            raise ValueError(f"{label}需要材料或折射率，以及厚度。")
        fixed_index = layer.get("n") is not None or layer.get("k") is not None
        if fixed_index and layer.get("material"):
            raise ValueError(f"{label}同时给出了色散材料和固定折射率。请选择本次计算采用的模型。")
        medium = _stack_medium(layer if fixed_index else layer.get("material"), label, sources, missing)
        if layer.get("thickness_nm") is None:
            missing.append(f"请填写{label}的厚度（nm）。")
            continue
        thickness = float(layer["thickness_nm"])
        if not math.isfinite(thickness) or thickness < 0:
            raise ValueError(f"{label}的厚度需要非负有限数值（nm）。")
        layers.append(Layer(thickness, material=medium) if isinstance(medium, str) else Layer(thickness, n=medium))
    stack = None if missing else Stack(layers=layers, incident=incident, substrate=substrate)
    return stack, sources, missing


def _eval_exact_stack(request: dict) -> dict:
    stack, sources, missing = _read_stack(request.get("stack", {}))
    if missing:
        return {"status": "needs_input", "mode_used": "exact_stack", "message": " ".join(missing)}
    aoi = float(request.get("aoi_deg", 0.0))
    if not math.isfinite(aoi) or not 0 <= aoi <= 90:
        raise ValueError("入射角应在 0 至 90 度之间。")
    pol = request.get("polarization", "avg")
    query = request["query_wavelengths_nm"]
    catalog = load_materials()
    representative = [key for key in sources if catalog[key]["confidence"] == "representative"]
    confidence = "medium" if any(catalog[key]["confidence"] == "medium" for key in sources) else "high"
    warnings = []
    if representative:
        confidence = "low"
        warnings.append(f"膜系使用代表性色散数据（{', '.join(representative)}），反射率与透射率仅适用于该假设。")
    material_sources = {
        key: {"locations": labels, "confidence": catalog[key]["confidence"],
              "source": catalog[key]["source"], "valid_range_um": catalog[key]["valid_range_um"]}
        for key, labels in sources.items()
    }
    query_results = []
    for w in query:
        issues = []
        for key in sources:
            info = index_info(key, w)
            if not info.in_range or not math.isfinite(info.n):
                lo, hi = catalog[key]["valid_range_um"]
                issues.append(f"{key} 色散模型的有效范围为 {lo * 1000:g}–{hi * 1000:g} nm")
        if issues:
            query_results.append({"wavelength_nm": w, "R": None, "T": None, "A": None, "reason": "；".join(issues)})
            continue
        if stack.incident_index(w).imag != 0:
            raise ValueError("此膜系的功率归一化要求无吸收入射介质。")
        res = solve_stack(stack, w, aoi_deg=aoi, pol=pol)
        if not all(math.isfinite(res[key]) for key in ("R", "T", "A")):
            raise ValueError(f"{w:g} nm 处的膜系超出当前数值范围。请核对折射率、单位和膜厚。")
        query_results.append(
            {
                "wavelength_nm": w,
                "R": round(res["R"], 6),
                "T": round(res["T"], 6),
                "A": round(res["A"], 6),
                "is_representative_only": bool(representative),
            }
        )
    valid = sum(row["R"] is not None for row in query_results)
    status = "completed" if valid == len(query) else ("partial" if valid else "needs_input")
    if valid < len(query):
        warnings.append("查询波长超出材料模型适用范围的行已留空。请补充覆盖相应波段的折射率或实测曲线。")
    return {
        "status": status,
        **({"message": warnings[-1]} if not valid else {}),
        "mode_used": "exact_stack",
        "confidence": confidence if valid else "unknown",
        "polarization": pol,
        "aoi_deg": aoi,
        "query_results": query_results,
        "material_sources": material_sources,
        "warnings": warnings,
        "assumptions": ["膜层均匀、各向同性，界面平面且彼此平行。", "基底按半无限介质处理，不计背面反射。",
                        "给定的固定折射率适用于本次查询的波长，实数折射率取 k = 0。",
                        "膜厚和折射率按输入值计算，未传播沉积误差。"],
        "recommend_measurement": bool(representative) or valid != len(query),
        "summary": f"已按给定的两侧介质和 {len(stack.layers)} 层膜系计算 {valid}/{len(query)} 个波长。",
    }


def _eval_vendor_curve(request: dict) -> dict:
    curve = request["vendor_curve"]
    xs = np.asarray(curve.get("wl_nm", []), dtype=float)
    if xs.ndim != 1 or xs.size < 2 or not np.all(np.isfinite(xs)) or np.any(xs <= 0) or len(np.unique(xs)) != len(xs):
        raise ValueError("Measured wavelengths must contain at least two distinct positive finite values.")
    for key in ("R", "T"):
        if curve.get(key) is not None:
            values = np.asarray(curve[key], dtype=float)
            if values.shape != xs.shape:
                raise ValueError(f"{key} must have one value for every measured wavelength.")
            if not np.all(np.isfinite(values)):
                raise ValueError(f"{key} contains non-finite values; correct the measurement data before interpolation.")
            if np.any((values < 0) | (values > 1)):
                raise ValueError(f"{key} must use fractions between 0 and 1; convert percent before calculation.")
    if curve.get("R") is None and curve.get("T") is None:
        raise ValueError("A measured curve requires R or T.")
    if curve.get("R") is not None and curve.get("T") is not None and np.any(np.asarray(curve["R"]) + np.asarray(curve["T"]) > 1.000001):
        raise ValueError("Passive coating R + T exceeds 1; check measurement normalization and units.")
    query = request["query_wavelengths_nm"]
    r_vals = _interp_curve(curve, query, "R")
    t_vals = _interp_curve(curve, query, "T")
    query_results = []
    measured_range = f"原始测量覆盖 {xs.min():g}–{xs.max():g} nm"
    for w, r, t in zip(query, r_vals, t_vals):
        available = r is not None or t is not None
        query_results.append(
            {
                "wavelength_nm": w,
                "R": None if r is None else round(r, 6),
                "T": None if t is None else round(t, 6),
                "in_measured_range": available,
                **({"reason": measured_range} if not available else {}),
                "is_representative_only": False,
            }
        )
    valid = sum(row["in_measured_range"] for row in query_results)
    message = f"已在原始测量波段内取得 {valid}/{len(query)} 个波长的插值。"
    return {
        "status": "completed" if valid == len(query) else ("partial" if valid else "needs_input"),
        **({"message": "查询波长均在原始测量范围之外。请提供覆盖该波段的光谱，或调整查询波长。"} if not valid else {}),
        "mode_used": "vendor_curve",
        "confidence": "high" if valid else "unknown",
        "query_results": query_results,
        "assumptions": ["values interpolated from the supplied vendor/measured curve"],
        "recommend_measurement": False,
        "source": curve.get("source", ""),
        "summary": message,
    }


def _eval_archetype(request: dict) -> dict:
    nominal = request["nominal"]
    query = [float(w) for w in request.get("query_wavelengths_nm", [])]
    est = archetype_stopband_estimate(
        lambda0_nm=float(nominal["design_wavelength_nm"]),
        function=nominal.get("function", "HR"),
        query_wavelengths_nm=query,
        aoi_deg=float(request.get("aoi_deg", 0.0)),
        pol=request.get("polarization", "avg"),
        high=nominal.get("high", "ta2o5_film"),
        low=nominal.get("low", "sio2_film"),
        substrate=nominal.get("substrate", "fused_silica"),
        target_R=float(nominal.get("target_R", 0.995)),
    )
    est["status"] = "completed"
    est["mode_used"] = "archetype"
    sb = est.get("stopband")
    if sb:
        est["summary"] = (
            f"Nominal {nominal.get('function','HR')} at {nominal['design_wavelength_nm']} nm: "
            f"archetype stopband ~{sb['edges_nm'][0]}-{sb['edges_nm'][1]} nm."
        )
    return est


def evaluate_coating(request: dict) -> dict:
    """Evaluate a coating in the mode implied by *request*.

    ``mode`` may be given explicitly (``exact_stack`` | ``vendor_curve`` |
    ``archetype``) or left as ``auto`` (default), which infers from which
    payload is present, preferring exact data.
    """
    mode = request.get("mode", "auto")
    has_stack = request.get("stack") is not None
    has_curve = bool(request.get("vendor_curve"))
    has_nominal = bool(request.get("nominal"))

    if mode == "auto":
        if has_stack:
            mode = "exact_stack"
        elif has_curve:
            mode = "vendor_curve"
        elif has_nominal:
            mode = "archetype"
        else:
            return {
                "status": "needs_input",
                "message": "Provide a stack, a vendor R/T curve, or a nominal {function, design_wavelength_nm}.",
            }

    if not request.get("query_wavelengths_nm") and mode != "archetype":
        return {"status": "needs_input", "message": "query_wavelengths_nm is required."}

    try:
        if mode in ("exact_stack", "vendor_curve"):
            raw_query = request["query_wavelengths_nm"]
            if not isinstance(raw_query, (list, tuple)) or any(isinstance(value, bool) for value in raw_query):
                raise ValueError("查询波长需要以 nm 为单位的数值列表。")
            query = [float(value) for value in raw_query]
            if not all(math.isfinite(w) and w > 0 for w in query):
                raise ValueError("查询波长需要以 nm 表示的正有限数值。")
            request = {**request, "query_wavelengths_nm": query}
        if mode == "exact_stack":
            output = _eval_exact_stack(request)
            if request.get("mode", "auto") == "auto" and output["status"] == "needs_input" and has_curve:
                curve = _eval_vendor_curve(request)
                if any(row["in_measured_range"] for row in curve["query_results"]):
                    curve["warnings"] = ["膜系输入不完整或超出材料模型范围。本次结果来自所提供的测量曲线。",
                                        *([output["message"]] if output.get("message") else output.get("warnings", []))]
                    return curve
            return output
        if mode == "vendor_curve":
            return _eval_vendor_curve(request)
        if mode == "archetype":
            return _eval_archetype(request)
    except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
        return {"status": "failed", "message": f"Coating evaluation failed: {exc}"}
    return {"status": "failed", "message": f"Unknown coating mode '{mode}'."}

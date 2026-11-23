"""Fit transverse widths in a homogeneous interval using physical z in mm."""
import math

import numpy as np


class UnresolvedCaustic(ValueError):
    def __init__(self, message, repeatability):
        super().__init__(message)
        self.repeatability = repeatability


def _waist(coefficients, origin, span, scale):
    a, b, c = coefficients
    if a <= 64 * np.finfo(float).eps:
        return None
    minimum = c - b * b / (4 * a)
    if minimum <= 64 * np.finfo(float).eps:
        return None
    values = (origin - span * b / (2 * a), scale * math.sqrt(minimum), scale * math.sqrt(a) / span)
    return values if all(math.isfinite(v) for v in values) else None


def _fit_radius(design, radius, initial):
    """Gauss-Newton on w rather than w², with a physical line search in scaled coordinates."""
    coeff = initial.copy()
    if _waist(coeff, 0., 1., 1.) is None:
        x = design[:, 1]
        if np.ptp(radius) < 1e-12:
            return None
        center = x[np.argmin(radius)]
        curvature = np.ptp(radius**2) / max(np.max(abs(x-center))**2, 1e-14)
        coeff = np.array([curvature, -2*curvature*center, min(radius)**2 + curvature*center**2])
    for _ in range(50):
        prediction = np.sqrt(design @ coeff)
        residual = prediction-radius
        jacobian = design/(2*prediction[:,None])
        step = np.linalg.lstsq(jacobian,-residual,rcond=None)[0]
        if np.linalg.norm(step) <= 1e-11 * max(1., np.linalg.norm(coeff)):
            return coeff
        cost = residual @ residual
        for power in range(22):
            candidate = coeff + step * (0.5**power)
            if _waist(candidate,0.,1.,1.) is not None:
                remaining = np.sqrt(design @ candidate)-radius
                if remaining @ remaining < cost:
                    coeff = candidate
                    break
        else:
            return coeff if np.linalg.norm(step) < 1e-8 * max(1.,np.linalg.norm(coeff)) else None
    return None


def _repeatability(positions, groups, counts, first, radii, fitted, scale):
    """Describe within-position variation in measured radii, independent of fit loss."""
    centered = radii - radii[first][groups]
    offsets = np.bincount(groups, weights=centered) / counts
    means = radii[first] + offsets
    sums = np.bincount(groups, weights=(centered - offsets[groups]) ** 2)
    repeated = np.flatnonzero(counts > 1)
    df = int(len(radii) - len(positions))
    # Equal weight for each observed station, even if one was sampled more often.
    within = np.mean(sums / counts)
    between = np.var(means)
    mean_error = np.mean((means - fitted) ** 2) if fitted is not None else None
    total = within + between
    fraction = float(within / total) if df and len(positions) > 1 and total > 0 else None
    return {
        "degrees_of_freedom": df, "repeated_positions": int(len(repeated)),
        "pooled_within_sd_mm": float(scale * np.sqrt(sums.sum() / df)) if df else None,
        "station_mean_fit_rms_mm": float(scale * np.sqrt(mean_error)) if mean_error is not None else None,
        "r_squared_station_mean_radius": float(1 - mean_error/between) if mean_error is not None and between > 0 else None,
        "r_squared_individual_radius": float(1 - (within+mean_error)/total) if mean_error is not None and total > 0 else None,
        "within_fraction_of_radius_variation": fraction,
        "stations": [{"z_mm": float(positions[i]), "count": int(counts[i]),
                      "mean_radius_mm": float(scale * means[i]),
                      "sample_sd_mm": float(scale * np.sqrt(sums[i] / (counts[i] - 1)))} for i in repeated],
    }


def fit_caustic(z_mm, radius_mm, wavelength_nm=None, medium_n=1.0, *, loss="radius"):
    z, w = np.asarray(z_mm, dtype=float), np.asarray(radius_mm, dtype=float)
    if z.ndim != 1 or not z.size or w.shape != z.shape or not np.isfinite(z).all() or not np.isfinite(w).all() or np.any(w <= 0):
        raise ValueError("位置和半径须逐行配对，数值须有限，半径须大于零。")
    if not all(v is None or math.isfinite(v) and v > 0 for v in (wavelength_nm, medium_n)):
        raise ValueError("已填写的真空波长和介质折射率须为有限正数。")
    positions, first, groups, counts = np.unique(z, return_index=True, return_inverse=True, return_counts=True)
    scale = float(np.max(w))
    def unresolved(message):
        return UnresolvedCaustic(message, _repeatability(positions, groups, counts, first, w/scale, None, scale))
    if positions.size < 5:
        raise unresolved(f"目前有 {positions.size} 个不同位置。请在同一均匀传播段测到至少 5 个不同位置，重复读数可保留。")
    origin = float(positions[0] / 2 + positions[-1] / 2)
    span = float(positions[-1] / 2 - positions[0] / 2)
    x = (positions - origin) / span
    design = np.column_stack((x*x, x, np.ones_like(x)))
    y = np.bincount(groups, weights=(w/scale)**2) / counts
    quadratic, _, rank, _ = np.linalg.lstsq(design, y, rcond=None)
    mean_radius = np.bincount(groups, weights=w/scale) / counts
    if loss not in {"radius","squared_radius"}:
        raise ValueError("未知的拟合误差定义。")
    coeff = _fit_radius(design, mean_radius, quadratic) if loss == "radius" else quadratic
    params = _waist(coeff, origin, span, scale) if coeff is not None else None
    if rank < 3 or params is None:
        raise unresolved("这些读数未解析出正的束腰和发散角。请检查单位，并补测宽度变化更明显的位置。")
    z0, w0, theta = params
    zr = w0 / theta
    product = w0 * theta
    bpp = 1000 * product
    missing = [key for key, value in (("wavelength_nm", wavelength_nm), ("medium_n", medium_n)) if value is None]
    def propagation_factor(area):
        if missing:
            return None
        wavelength_mm = wavelength_nm * 1e-6
        if wavelength_mm == 0:
            raise ValueError("真空波长超出可表示范围，请核对单位。")
        return math.pi * medium_n * area / wavelength_mm
    m2 = propagation_factor(product)
    predicted = np.hypot(w0, theta * (z-z0))
    if not np.isfinite(predicted).all() or not all(math.isfinite(v) and v > 0 for v in (bpp, zr)) or (m2 is not None and not math.isfinite(m2)):
        raise ValueError("计算超出数值范围，请核对单位。")
    variation = float(np.sum((y-np.mean(y))**2))
    r2 = 1 - float(np.sum((y-design@coeff)**2))/variation if variation else None
    # Omitting an entire station (including repeat captures) tests leverage, not measurement uncertainty.
    q, _ = np.linalg.qr(design, mode="reduced")
    inverse = np.linalg.pinv(design)
    sensitivities = []
    stations = np.arange(len(x))
    if loss == "radius" and len(stations) > 64:
        stations = np.unique(np.r_[np.linspace(0,len(x)-1,48,dtype=int), np.argmax(np.sum(q*q,axis=1)),
                                   np.argmax(np.abs(np.sqrt(design@coeff)-mean_radius))])
    for i in stations:
        row = design[i]
        remaining = 1 - q[i]@q[i]
        if remaining > 1e-10:
            alternate = quadratic - inverse[:, i] * ((y[i]-row@quadratic) / remaining)
            if loss == "radius":
                retained = np.arange(len(x)) != i
                alternate = _fit_radius(design[retained],mean_radius[retained],alternate)
            solution = _waist(alternate, origin, span, scale) if alternate is not None else None
            if solution is not None:
                sensitivities.append((float(positions[i]), *solution))
    bracketed = bool(positions[0] < z0 < positions[-1])
    warnings, next_steps, limits = [], [], {}
    all_fields = ("waist_z_mm", "waist_radius_mm", "divergence_half_angle_rad", "bpp_mm_mrad", "m2", "geometry")
    def caution(message, fields=all_fields):
        warnings.append(message)
        for field in fields:
            limits.setdefault(field, []).append(message)
    if not bracketed:
        caution("束腰在测量区间之外，位置来自外推。")
        next_steps.append("在同一均匀光路上扩展测量范围，直到观察到光斑先缩小再增大。")
    if not np.any(np.abs(positions - z0) <= zr):
        caution("束腰附近缺少测点，最小半径依赖外推。", ("waist_radius_mm","bpp_mm_mrad","m2","geometry"))
        next_steps.append("在预测束腰附近补一个位置，校验最小半径。")
    if not np.any(np.abs(positions - z0) >= 2*zr):
        caution("现有跨度对远离束腰后的发散约束较弱。", ("divergence_half_angle_rad","bpp_mm_mrad","m2","geometry"))
        next_steps.append("在该均匀段可达的较远位置测量光斑，核对发散斜率。")
    if m2 is not None and m2 < 1 - 1e-8:
        caution("推得的传播因子小于 1，请核查直径口径、波长和横向标定。", ("waist_radius_mm","divergence_half_angle_rad","bpp_mm_mrad","m2","geometry"))
    if r2 is None or r2 < .98:
        caution("半径平方与抛物线的吻合较弱，请检查逐点残差和原始光斑。")
    if len(sensitivities) < len(stations):
        caution("去掉一个测量位置就可能显著改变结果，请复测对拟合影响大的位置。")
    omission_ranges = {}
    for key, label, index, nominal, tolerance, affected in (
        ("waist_z_mm", "束腰位置", 1, z0, .25*zr, ("waist_z_mm","geometry")),
        ("waist_radius_mm", "束腰半径", 2, w0, .05*w0, ("waist_radius_mm","geometry")),
        ("divergence_half_angle_rad", "发散半角", 3, theta, .05*theta, ("divergence_half_angle_rad","geometry")),
        ("bpp_mm_mrad", "束腰半径与发散半角的乘积", None, bpp, .05*bpp, ("bpp_mm_mrad","m2")),
    ):
        if not sensitivities:
            break
        estimates = [(v[index] if index is not None else 1000*(v[2]*v[3]), v[0]) for v in sensitivities]
        low, high = min(estimates), max(estimates)
        omission_ranges[key] = {"min":low[0], "max":high[0], "omitted_at_min_mm":low[1], "omitted_at_max_mm":high[1]}
        shift, station = max((abs(value-nominal), station) for value, station in estimates)
        if shift > tolerance:
            threshold = "四分之一等效瑞利长度" if index == 1 else "5%"
            caution(f"去掉 z = {station:g} mm 的测量位置后，{label}变化超过{threshold}。", affected)
    quadratic_params = _waist(quadratic,origin,span,scale)
    quadratic_product = quadratic_params[1]*quadratic_params[2] if quadratic_params else None
    quadratic_m2 = propagation_factor(quadratic_product) if quadratic_product is not None else None
    if loss == "radius" and quadratic_params is not None:
        alt_z, alt_w, alt_theta = quadratic_params
        if abs(alt_z-z0) > .25*zr:
            caution("改变拟合误差口径后束腰位置移动超过四分之一等效瑞利长度。", ("waist_z_mm","geometry"))
        for alt, value, key, label in ((alt_w,w0,"waist_radius_mm","束腰半径"),(alt_theta,theta,"divergence_half_angle_rad","发散半角")):
            if abs(alt/value-1) > .05:
                caution(f"改变拟合误差口径后{label}变化超过 5%。", (key,"geometry"))
        if abs(quadratic_product/product-1) > .05:
            caution("改变拟合误差口径后，束腰半径与发散半角的乘积变化超过 5%。", ("bpp_mm_mrad","m2"))
    repeats = _repeatability(positions, groups, counts, first, w/scale, predicted[first]/scale, scale)
    prediction_warnings = []
    fraction = repeats["within_fraction_of_radius_variation"]
    # A diagnostic on individual widths. It does not invalidate fitted means.
    if fraction is not None and fraction > .02:
        message = f"各位置等权时，同位置读数的离散占半径总变差的 {fraction:.1%}。预测曲线描述中心趋势，单次光斑可能明显偏离。"
        prediction_warnings.append(message)
        warnings.append(message)
        next_steps.append("检查同位置各次读数的采集顺序、光源工况和相机设置，再用独立读数检验预测。")
    fit_warnings = list(warnings)
    if missing:
        names = {"wavelength_nm":"真空波长", "medium_n":"该波长下的介质折射率"}
        needed = "和".join(names[key] for key in missing)
        warnings.append("M² 待定，缺少" + needed + "。束腰和几何发散由宽度测量独立拟合。")
    curve_z = np.linspace(positions[0], positions[-1], 121)
    return {"waist_z_mm": float(z0), "waist_radius_mm": w0, "divergence_half_angle_rad": theta, "m2": m2,
            "bpp_mm_mrad": bpp, "missing_for_m2": missing, "fit_warnings": fit_warnings,
            "output_limits": {key:" ".join(messages) for key,messages in limits.items()},
            "rayleigh_equivalent_mm": zr, "r_squared_w2": r2, "warnings": warnings, "next_steps": next_steps,
            "quality": "partial" if warnings else "completed", "distinct_positions": int(positions.size),
            "bracketed": bracketed, "leave_one_position_out_z_mm": [omission_ranges["waist_z_mm"][k] for k in ("min","max")] if sensitivities else None,
            "leave_one_position_out": omission_ranges, "repeatability": repeats, "prediction_warnings": prediction_warnings,
            "loss": loss, "squared_radius_m2": quadratic_m2, "tested_omissions": len(stations),
            "valid_omissions": len(sensitivities), "radius_rms_mm": float(np.linalg.norm((w-predicted)/scale) / math.sqrt(len(w)) * scale),
            "data": {"z_mm": z.tolist(), "radius_mm": w.tolist(), "fit_radius_mm": predicted.tolist(),
                     "curve_z_mm": curve_z.tolist(), "curve_radius_mm": np.hypot(w0, theta*(curve_z-z0)).tolist()},
            "scope": "沿固定横向轴，在折射率恒定的无光焦度区间拟合。不同位置等权，重复测量按所选误差口径求均值。传播因子的二阶矩解释要求相应实测口径。"}

"""CW unsaturated modal POWER gain and round-trip balance in absorbed pump watts."""
from bisect import bisect_right
import math

from .beam import gaussian_product_density
from .constants import C_LIGHT, H_PLANCK


def weighted_gaussian_area(length, optical_depth, axes):
    """Integrate the transverse intensity product against normalized Beer deposition on [0,L]."""
    from scipy.integrate import quad

    knots = {0., 1.}
    for scale in (1, 10, 40):
        knots.add(scale / optical_depth)
    for pair in axes:
        for width, x, y, center, slope in pair:
            for offset in (-y, 0, y):
                knots.add((-x + offset) / length)
        a, b = pair
        if a[4] != b[4]:
            cross = (b[3] - a[3]) / (a[4] - b[4])
            if math.isfinite(cross):
                at_cross = [w * (math.hypot(qx + cross, qy) / math.hypot(qx, qy)) for w,qx,qy,_,_ in pair]
                span = math.hypot(*at_cross) / abs(a[4] - b[4])
                for offset in (-span, 0, span):
                    knots.add((cross + offset) / length)
    def integrand(t):
        z = length * t
        product = 1.
        for pair in axes:
            radii = [w * (math.hypot(x + z, y) / math.hypot(x, y)) for w,x,y,_,_ in pair]
            first, second = pair
            delta = (second[3] - first[3]) + z * (second[4] - first[4])
            product *= gaussian_product_density(*radii, delta)
        return product * (optical_depth / -math.expm1(-optical_depth)) * math.exp(-optical_depth*t)

    # Waist, center-crossing and absorption scales partition the integral even for displaced tight foci.
    result = quad(integrand, 0., 1., points=sorted(t for t in knots if 0 < t < 1),
                  epsabs=0., epsrel=1e-8, limit=160, full_output=1)
    density, error = result[:2]
    if (len(result) != 3 or not math.isfinite(density) or density <= 0 or not math.isfinite(1 / density)
            or error > 1e-7 * density):
        raise ValueError("重叠积分过小或误差未收敛，请核对束心、焦点与吸收尺度。")
    return 1 / density, error / density


def _curve(args, pump, log_gain, method):
    return {"pump_nm": args.pump_nm, "signal_nm": args.signal_nm, "absorbed_w": pump,
            "log_gain": log_gain, "method": method, "basis": args.basis}


def four_level_gain(args):
    # Effective area includes overlap throughout the pumped volume. Length is already integrated.
    try:
        photon_j = H_PLANCK * C_LIGHT / (args.pump_nm * 1e-9)
        slope = (args.emission_cm2 / args.area_mm2) * 100 * (args.lifetime_us * 1e-6) * args.pump_yield / photon_j
        endpoint = slope * args.max_absorbed_w
    except (ZeroDivisionError, OverflowError) as exc:
        raise ValueError("四能级模型参数超出数值范围，请核对单位。") from exc
    if not math.isfinite(endpoint) or (args.pump_yield > 0 and endpoint == 0):
        raise ValueError("增益超出数值范围，请核对截面、有效面积和泵浦功率。")
    return {"status": "completed", "log_gain_per_w": slope, "curve": _curve(
        args, [0., args.max_absorbed_w], [0., endpoint], "ideal_four_level"),
        "summary": "给定工况内的单程小信号模增益。曲线纵轴为功率增益的自然对数。",
        "assumptions": ["采用稳态四能级近似。受激耗尽及再吸收忽略，模式、泵浦沉积分布、截面及寿命在给定范围内固定。"]}


def measured_gain(args):
    return {"status": "completed", "curve": _curve(args, args.absorbed_w, [math.log(r) for r in args.power_ratio],
                                                   "weak_probe"),
            "summary": "在实测吸收泵浦功率范围内，对单程弱探针功率增益取自然对数并分段线性插值。",
            "assumptions": ["增益沿用探针方向、空间模式、偏振、温度和测量参考面。探针包含的无源损耗应从其余往返损耗中扣除。"]}


def _margin(gain, traversals, survival):
    if survival == 0:
        return None  # zero return, logarithm is minus infinity
    try:
        added, loss = traversals * gain, math.log(survival)
    except OverflowError as exc:
        raise ValueError("通过次数与增益的乘积超出数值范围。") from exc
    net = added + loss
    if not math.isfinite(net):
        raise ValueError("往返增益超出数值范围，请核对对数增益及通过次数。")
    return 0. if abs(net) <= 16 * max(math.ulp(added), math.ulp(loss)) else net


def _balance_bands(pump, low, high):
    """Intersect each linear segment with low margin <= 0 <= high margin; merge shared knots."""
    bands = []
    for i, (p0, p1) in enumerate(zip(pump, pump[1:])):
        start, stop = p0, p1
        # Only a positive survival gives an upper margin. Zero lower survival leaves no lower restriction.
        if high[i] is None:
            continue
        for margins, sign in ((high, 1), (low, -1)):
            if margins[i] is None:
                continue
            a, b = sign * margins[i], sign * margins[i + 1]
            if max(a, b) < 0:
                break
            if min(a, b) < 0:
                scale = max(abs(a), abs(b))
                fraction = (a / scale) / (a / scale - b / scale)
                cross = p0 if a == 0 else p1 if b == 0 else p0 + fraction * (p1 - p0)
                if a < 0:
                    start = max(start, cross)
                else:
                    stop = min(stop, cross)
        else:
            if start <= stop:
                if bands and bands[-1]["stop_w"] == start:
                    bands[-1]["stop_w"] = stop
                else:
                    bands.append({"start_w": start, "stop_w": stop})
    return bands


def _interpolate(x, xs, ys):
    index = bisect_right(xs, x) - 1
    if x == xs[index]:
        return ys[index]
    f = (x - xs[index]) / (xs[index + 1] - xs[index])
    return (1 - f) * ys[index] + f * ys[index + 1]


def roundtrip_balance(args):
    curve = args.curve
    pump, gains = curve.absorbed_w, curve.log_gain
    s_low, s_high = args.survival
    low = [_margin(g, args.traversals, s_low) for g in gains]
    high = [_margin(g, args.traversals, s_high) for g in gains]
    bands = _balance_bands(pump, low, high)
    summary = (f"在 {pump[0]:g} 至 {pump[-1]:g} W 吸收泵浦范围内，有 {len(bands)} 处功率增益与损耗相等的点或区间。"
               if bands else "给定泵浦范围和损耗范围内没有增益与损耗相等的点。")
    if s_high == 0:
        summary = "所给往返无源保留比例为零，此路径没有返回光功率。"
    edges = sorted({b[key] for b in bands for key in ("start_w", "stop_w")})
    chart = {"absorbed_w": pump, "balance": [0.] * len(pump), "edges_w": edges, "zero_at_edges": [0.] * len(edges)}
    if s_low == s_high and s_high > 0:
        chart["net"] = high
    else:
        if s_low > 0:
            chart["lower"] = low
        if s_high > 0:
            chart["upper"] = high
    result = {"status": "completed", "summary": summary, "balance_bands": bands, "curve_plot": chart,
              "domain_w": [pump[0], pump[-1]], "pump_nm": curve.pump_nm, "signal_nm": curve.signal_nm,
              "assumptions": ["判据针对指定信号模式与等效通过次数，只判断微弱信号的往返增长。实际振荡还取决于谐振、模式竞争和饱和。"]}
    if s_low == 0:
        result["assumptions"].append("保留比例为零对应完全无返回，其对数为负无穷；曲线图只绘制有限的对数倍率。")
    if args.operating_w is None:
        return result
    a, b = args.operating_w
    if a < pump[0] or b > pump[-1]:
        result.update(status="partial", operating_state="outside_domain", operating_label="运行点超出增益曲线适用范围",
                      message=f"运行区间 {a:g}–{b:g} W 有部分超出曲线范围。请提供覆盖该区间的增益依据。")
        return result
    sample_w = sorted({a, b, *(p for p in pump if a < p < b)})
    values = [_interpolate(p, pump, gains) for p in sample_w]
    if s_high > 0:
        bounds = [s_high] if s_low in (0, s_high) else [s_low, s_high]
        chart["operating_w"] = sample_w * len(bounds)
        chart["operating_log"] = [_margin(g, args.traversals, s) for s in bounds for g in values]
    worst, best = _margin(min(values), args.traversals, s_low), _margin(max(values), args.traversals, s_high)
    if best is None or best < 0:
        state, label = "below", "微弱信号往返衰减"
    elif worst is not None and worst > 0:
        state, label = "above", "微弱信号往返增长"
    elif worst == best == 0:
        state, label = "balanced", "微弱信号往返恰好平衡"
    else:
        state, label = "conditional", "范围触及或跨越平衡边界"
    result.update(operating_state=state, operating_label=label, operating_margin=[worst, best],
                  message=f"在 {a:g} 至 {b:g} W 的吸收泵浦区间，{label}。")
    return result

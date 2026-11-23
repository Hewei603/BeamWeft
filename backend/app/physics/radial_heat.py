"""Axially averaged steady radial conduction for an insulated-ended cylinder."""
from bisect import bisect_left
import math


def _shell_integral(r, outer, at_r, density):
    """Integral of enclosed heat fraction / radius from r to outer in one uniform shell."""
    if r == 0:
        return .5 * density * outer**2
    t = (outer-r)/r
    log_ratio = math.log1p(t)
    # t + t²/2 - log1p(t) starts at order t². Keep precision for thin rings.
    area_term = ((outer-r)**2 * (1 - t/3 + t*t/4) if t < 1e-4 else
                 .5 * (outer-r)*(outer+r) - r*r*log_ratio)
    return at_r*log_ratio + density*area_term


HEAT_INPUTS = ("absorbed_w", "heat_fraction")
RADIAL_INPUTS = (*HEAT_INPUTS, "length_mm", "radius_mm", "conductivity", "zone_edges_mm", "relative_density")
CONTACT_INPUTS = (*HEAT_INPUTS, "length_mm", "radius_mm", "side_conductance")
HEAT_REQUIREMENTS = {"heat_w": HEAT_INPUTS, "axis_to_surface_k": RADIAL_INPUTS,
                     "surface_to_sink_k": CONTACT_INPUTS, "axis_to_sink_k": (*RADIAL_INPUTS, "side_conductance")}


def _radial_profile(args, heat):
    edges = [v/args.radius_mm for v in args.zone_edges_mm]
    peak = max(args.relative_density)
    density = [v/peak for v in args.relative_density]
    if edges[-1] < 1:
        edges.append(1.)
        density.append(0.)
    starts = [0., *edges[:-1]]
    areas = [(b-a)*(b+a) for a,b in zip(starts,edges)]
    norm = math.fsum(area*q for area,q in zip(areas,density))
    if not norm or any(a>=b for a,b in zip(starts,edges)):
        raise ValueError("环区尺度超出了可分辨的数值范围，请核对半径和单位。")
    density = [q/norm for q in density]
    enclosed, integrals = [], []
    fraction = 0.
    for a,b,q,area in zip(starts,edges,density,areas):
        enclosed.append(fraction)
        integrals.append(_shell_integral(a,b,fraction,q))
        fraction += q*area
    outside = [0.] * len(edges)
    for i in range(len(edges)-2,-1,-1):
        outside[i] = outside[i+1] + integrals[i+1]
    length_m = args.length_mm/1000
    radial_conductance = 2*math.pi*args.conductivity*length_m
    if not 0 < radial_conductance < math.inf:
        raise ValueError("热导率和圆柱长度的组合超出数值范围，请核对参数和单位。")
    factor = 1/radial_conductance
    # Include every boundary and samples within every zone, even with a narrow central spot.
    points = {0.,1.,*edges, *(i/100 for i in range(101))}
    for a,b in zip(starts,edges):
        points.update(a+(b-a)*i/8 for i in range(1,8))
    profile = {"radius_mm": [], "low_k": [], "high_k": []}
    for r in sorted(points):
        i = bisect_left(edges,r)
        at_r = enclosed[i] + density[i]*(r-starts[i])*(r+starts[i])
        resistance = factor * (outside[i] + _shell_integral(r,edges[i],at_r,density[i]))
        if not math.isfinite(resistance * heat[1]):
            raise ValueError("该热源的径向温差超出数值范围，请核对热源半径、功率和单位。")
        profile["radius_mm"].append(r * args.radius_mm)
        profile["low_k"].append(resistance*heat[0])
        profile["high_k"].append(resistance*heat[1])
    return profile


def radial_heating(args):
    heat = [p*f for p,f in zip(args.absorbed_w,args.heat_fraction)]
    output = {"heat_w": heat, "assumptions": [args.basis],
              "summary":"热功率上下界来自体吸收功率与热转化比例。温差分别以侧壁和热沉为参考。"}
    if all(getattr(args, key) is not None for key in RADIAL_INPUTS):
        profile = _radial_profile(args, heat)
        output.update(profile=profile, axis_to_surface_k=[profile["low_k"][0],profile["high_k"][0]])
        output["assumptions"].append("稳态、端面绝热、均匀圆柱、常数径向热导率，热源绕轴对称。曲线给出沿长度平均的径向温差。")
    if all(getattr(args, key) is not None for key in CONTACT_INPUTS):
        length_m = args.length_mm/1000
        area_m2 = 2*math.pi*args.radius_mm/1000*length_m
        conductance = args.side_conductance*area_m2
        if not 0 < conductance < math.inf:
            raise ValueError("侧壁面积与换热系数的组合超出数值范围，请核对单位。")
        offset = [p/conductance for p in heat]
        if not math.isfinite(offset[1]):
            raise ValueError("热沉与轴心之间的温差超出可表示范围。")
        output["surface_to_sink_k"] = offset
        output["assumptions"].append("全部热流通过圆柱侧壁传给均温热沉，换热系数在侧壁均匀。")
    if "axis_to_surface_k" in output and "surface_to_sink_k" in output:
        center, offset = output["axis_to_surface_k"], output["surface_to_sink_k"]
        output["axis_to_sink_k"] = [a+b for a,b in zip(center,offset)]
        if not math.isfinite(output["axis_to_sink_k"][1]):
            raise ValueError("轴心到热沉的温差超出可表示范围。")
    output["status"] = "completed" if "axis_to_sink_k" in output else "partial"
    return output

"""Passive forward optical power, with separate bulk absorption and unassigned loss."""
import math
from typing import Literal

from pydantic import Field, model_validator

from .optics import StrictInput


class Band(StrictInput):
    low: float = Field(ge=0)
    high: float | None = Field(default=None, ge=0, description="Omit for a single value.")
    basis: Literal["assumed", "datasheet", "measured", "calculated"] = "assumed"
    source: str = Field(default="", max_length=600, description="Citation, instrument file and row, or assumption.")

    @model_validator(mode="after")
    def ordered(self):
        if self.high is not None and self.high < self.low:
            raise ValueError("区间上限须大于等于下限。")
        if self.basis != "assumed" and not self.source.strip():
            raise ValueError("请填写参数来源，如资料页、文件与读数位置。")
        return self

    def bounds(self):
        return self.low, self.low if self.high is None else self.high


class Fraction(Band):
    low: float = Field(ge=0, le=1)
    high: float | None = Field(default=None, ge=0, le=1)


class NetLoss(StrictInput):
    mode: Literal["net"]
    retained: Fraction | None = Field(default=None, description="Fraction of incident power retained in the selected forward path, across the entire stage.")


class BulkLoss(StrictInput):
    mode: Literal["bulk"]
    entry: Fraction | None = None
    exit: Fraction | None = None
    alpha_per_mm: Band | None = Field(default=None, description="Napierian intensity/power absorption coefficient, per mm.")
    internal_t: Fraction | None = Field(default=None, description="Bulk internal transmittance at reference thickness, excludes surface losses.")
    reference_mm: float | None = Field(default=None, gt=0, le=1e6)
    internal_loss_is_absorption: bool = Field(default=False, description="Attribute internal transmission loss to absorption only when scattering and other loss can be neglected.")

    @model_validator(mode="after")
    def independent_absorption(self):
        if self.alpha_per_mm is not None and (self.internal_t is not None or self.reference_mm is not None):
            raise ValueError("体吸收请选择吸收系数或参考厚度内透过率，二者描述同一项。")
        return self


class PowerStage(StrictInput):
    scope: Literal["arm", "host"]
    element_uid: str = Field(min_length=1, max_length=100)
    law: NetLoss | BulkLoss = Field(discriminator="mode")


class PathPower(StrictInput):
    wavelength_nm: float | None = Field(default=None, gt=0, le=1e6, description="Wavelength for all budget inputs.")
    input_w: Band | None = None
    stages: list[PowerStage] = Field(default_factory=list, max_length=160)

    @model_validator(mode="after")
    def unique(self):
        keys = {(s.scope,s.element_uid) for s in self.stages}
        if len(keys) != len(self.stages):
            raise ValueError("同一元件在这条支路中只能采用一套能量参数。")
        return self


def _mul(a, b):
    return a[0]*b[0], a[1]*b[1]


def _complement(pair):
    return 1-pair[1], 1-pair[0]


def _bounds(value, label, notes):
    if value is None:
        notes.append(label)
        return (0., 1.)
    return value.bounds()


def optical_depth_bounds(law, length):
    if law.alpha_per_mm is not None:
        low, high = law.alpha_per_mm.bounds()
        return low * length, high * length
    elif law.internal_t is not None and law.reference_mm is not None:
        low, high = law.internal_t.bounds()
        def depth(t):
            if t == 0:
                return math.inf
            if t == 1:
                return 0.
            return (-math.log(t)*length)/law.reference_mm
        return depth(high), depth(low)
    return None


def _bulk_factors(law, length, notes):
    if length == 0:
        return (1.,1.), (0.,0.)
    optical_depth = optical_depth_bounds(law, length)
    if optical_depth is None:
        notes.append("体吸收系数，或参考厚度及内透过率")
        return (0.,1.), (0.,1.)
    lo, hi = optical_depth
    return (math.exp(-hi),math.exp(-lo)), (-math.expm1(-lo),-math.expm1(-hi))


def _metrics(band, input_power):
    return {"fraction": list(band), "watts": list(_mul(band,input_power)) if input_power is not None else None}


def partial_stage_transmission(row, distance, cross_entry, cross_exit):
    """Forward fraction between two points in a stage, conditional on incident power."""
    raw = row.get("law") or {}
    if raw.get("mode") != "bulk":
        if not cross_entry and not cross_exit and distance == 0:
            return (1.,1.)
        law = NetLoss.model_validate(raw) if raw else None
        retained = _bounds(law.retained if law else None, "", [])
        return retained if cross_entry and cross_exit else (retained[0],1.)
    law = BulkLoss.model_validate(raw)
    fraction, _ = _bulk_factors(law,distance,[])
    if cross_entry:
        fraction = _mul(fraction,_bounds(law.entry,"",[]))
    if cross_exit:
        fraction = _mul(fraction,_bounds(law.exit,"",[]))
    return fraction

def _unassigned(entry, survival, removed, exit_fraction, is_absorption, downstream):
    """Include all losses that the model cannot attribute to absorption."""
    exit_loss = _complement(exit_fraction)
    after_body = (exit_loss[0] + exit_fraction[1] * downstream[0],
                  exit_loss[1] + exit_fraction[0] * downstream[1])
    if is_absorption:
        internal = _mul(survival, after_body)
    else:
        # The same material cannot simultaneously remove light and transmit it onward.
        internal = (removed[0] + survival[1] * after_body[0],
                    removed[1] + survival[0] * after_body[1])
    return (1 - entry[1] + entry[1] * internal[0], 1 - entry[0] + entry[0] * internal[1])


def analyze_path_power(config, *, path_uid, name, wavelength_nm, arm, route, route_issue, planes):
    base = {"path_uid":path_uid, "name":name, "wavelength_nm":wavelength_nm, "rows":[], "planes":[]}
    if wavelength_nm is None or config.wavelength_nm is None or not math.isclose(config.wavelength_nm,wavelength_nm,rel_tol=1e-12):
        return {**base, "status":"needs_input", "message":"请核对能量参数适用的波长与入射支路波长。"}
    power = config.input_w.bounds() if config.input_w is not None else None
    laws = {(s.scope,s.element_uid):s.law for s in config.stages}
    z = route[0][1] - sum(el.length_mm for el in arm) if route else None
    coords = []
    for el in arm:
        coords.append(("arm", el, z))
        if z is not None:
            z += el.length_mm
    coords.extend(("host", el, z) for el, z, _ in route)
    if not coords:
        return {**base, "status":"needs_input", "message":route_issue or "请先接入要计算的元件。"}
    inside = {}
    for plane in planes:
        inside.setdefault(plane.target_uid,[]).append(plane)
    passing = (1.,1.)
    factors = []
    missing = []
    for scope, el, z in coords:
        law = laws.pop((scope,el.uid),None)
        notes = []
        incoming = passing
        length = el.length_mm
        # Each physical interface is counted once in the chosen single-pass path.
        if isinstance(law,BulkLoss):
            entry = _bounds(law.entry,"入面保留比例",notes)
            exit_fraction = _bounds(law.exit,"出面保留比例",notes)
            bulk, absorbed = _bulk_factors(law,length,notes)
            is_absorption = law.alpha_per_mm is not None or law.internal_loss_is_absorption
        else:
            retained = _bounds(law.retained if law else None,"整件沿本支路的功率保留比例",notes)
            entry, exit_fraction, bulk, absorbed = retained,(1.,1.),(1.,1.),(0.,0.)
            is_absorption = False
        through = _mul(_mul(entry,bulk),exit_fraction)
        passing = _mul(incoming,through)
        local_other = _unassigned(entry,bulk,absorbed,exit_fraction,is_absorption,(0.,0.))
        row = {"scope":scope, "uid":el.uid,"name":el.name, "z_mm":z, "length_mm":length,
               "incoming":_metrics(incoming,power), "outgoing":_metrics(passing,power),
               "bulk_absorption":_metrics(_mul(incoming,_mul(entry,absorbed)),power) if is_absorption else None,
               "unassigned":_metrics(_mul(incoming,local_other),power), "missing":notes,
               "law":law.model_dump(exclude_none=True) if law else None}
        base["rows"].append(row)
        factors.append((entry,bulk,absorbed,exit_fraction,is_absorption))
        missing.extend(f"{el.name} · {note}" for note in notes)
        if scope == "host" and el.kind == "space":
            for plane in inside.get(el.uid,[]):
                if plane.depth_mm > length:
                    continue
                if isinstance(law,BulkLoss):
                    before, deposited = _bulk_factors(law,plane.depth_mm,[])
                    amount = _mul(incoming,_mul(entry,before))
                    local_absorbed = _metrics(_mul(incoming,_mul(entry,deposited)),power) if is_absorption else None
                    note = "取介质内侧。入口已计入入面损耗，末端尚未计出面损耗。"
                else:
                    amount = (passing[0],incoming[1])
                    local_absorbed = None
                    note = "整件保留率仅约束边界，内部位置给出无增益条件下的界限。"
                base["planes"].append({"plane_uid":plane.uid,"z_mm":z+plane.depth_mm,"forward":_metrics(amount,power),
                                       "absorbed_in_segment":local_absorbed,"note":note})
    # Backward recursion preserves the complement relationship between absorption and transmission.
    total_abs, total_other = (0.,0.), (0.,0.)
    for entry, bulk, absorbed, exit_fraction, is_absorption in reversed(factors):
        if is_absorption:
            total_abs = (entry[0]*(absorbed[0]+bulk[1]*exit_fraction[0]*total_abs[0]),
                         entry[1]*(absorbed[1]+bulk[0]*exit_fraction[1]*total_abs[1]))
        else:
            total_abs = _mul(_mul(entry,bulk),_mul(exit_fraction,total_abs))
        total_other = _unassigned(entry,bulk,absorbed,exit_fraction,is_absorption,total_other)
    if power is None:
        missing.insert(0,"源面光功率")
    if route_issue:
        missing.append(route_issue)
    if laws:
        missing.append("存在未接入当前光路的旧能量参数，请核对元件。")
    summary = {"outgoing":_metrics(passing,power),"modeled_bulk_absorption":_metrics(total_abs,power),
               "unassigned":_metrics(total_other,power)}
    aperture_note = (" 本预算使用填写的保留率，几何孔径损失须包含在对应参数中。" if any(el.clear_aperture for _,el,_ in coords) else "")
    return {**base,"status":"partial" if missing else "completed","missing":missing,"route_complete":not bool(route_issue),
            "input_w":power,"input_spec":config.input_w.model_dump(exclude_none=True) if config.input_w else None,
            "summary":summary,"unused_stages":[{"scope":s,"element_uid":uid} for s,uid in laws],
            "scope":"单色、单程、被动线性传输。累计体吸收只统计采用体吸收模型的段。其余移出所选支路的功率单列。区间按各项可独立变化计算。"+aperture_note}

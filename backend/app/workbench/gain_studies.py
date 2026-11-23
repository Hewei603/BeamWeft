"""Two independent sources of a modal gain law, shared by manual and Agent calculations."""
import math
from typing import Annotated

from pydantic import Field, model_validator

from ..physics.cw_gain import four_level_gain, measured_gain, roundtrip_balance, weighted_gaussian_area
from ..physics.measurement_data import POWER_SCALE
from ..physics.ray_overlap import weighted_ray_area
from ..study_api import Condition, ConditionMatch, Port, RowOutputs, StrictInput, Study
from .mode_overlap import _scope_warnings
from .observation import AXES, ObservationPlane, OpticalLayout, analyze_sections
from .optics import analyze_system
from .path_power import BulkLoss, optical_depth_bounds
from .project_outputs import ABSORBED, LAYOUT, MODEL_REVISION, PUMP_SITE, SITE_CONDITION, WAVELENGTH, at_wavelength, sampling_site
from .ray_source import RAY_BUNDLE, RayBundle
from .ray_study import weighted_rays_at_plane

Positive = Annotated[float, Field(gt=0)]
Nonnegative = Annotated[float, Field(ge=0)]
Share = Annotated[float, Field(ge=0, le=1)]
Wavelength = Annotated[float, Field(gt=0, le=1e6)]
Note = Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]


class GainConditions(StrictInput):
    pump_nm: Wavelength = Field(title="泵浦波长 / nm")
    signal_nm: Wavelength = Field(title="信号波长 / nm")
    basis: Note = Field(title="增益工况与依据", description="注明空间模式、偏振、温度、有效范围和数据来源，区分实测与假设。")


class FourLevel(GainConditions):
    emission_cm2: Positive = Field(title="受激发射截面 / cm²", description="对应所选信号波长、偏振与温度。")
    lifetime_us: Positive = Field(title="有效上能级寿命 / µs")
    pump_yield: Share = Field(title="吸收光子至上能级的量子产额", description="每个已吸收泵浦光子产生的上能级激发数，此模型取 0–1。")
    area_mm2: Positive = Field(title="体积加权有效重叠面积 / mm²",
        description="其倒数为泵浦与信号归一化横向强度乘积的面积积分，再沿介质按吸收泵浦分布加权。仅在重叠沿程恒定时可用单截面值。")
    max_absorbed_w: Positive = Field(title="模型适用的最大吸收泵浦功率 / W")


class SpatialOverlap(StrictInput):
    layout: OpticalLayout = Field(title="工程光路模型")
    signal_uid: str = Field(default="primary", min_length=1, max_length=100, title="信号光路标识",
        description="primary 表示主路单程光束或正向腔基模，也可填写已定义的另一支路标识。",
        json_schema_extra={"x-laserclaw-input":"path-uid"})
    pump_bundle: RayBundle | None = Field(default=None, exclude_if=lambda v: v is None,
        title="泵浦发射面上的加权光线",
        description="可引用按功率采样的非相干光线。位置与折射率乘斜率均取所选泵浦支路发射面的工程 T/S 坐标，包含实际偏移和指向。留空使用工程内的高斯分布。")
    pump_uid: str | None = Field(default=None, min_length=1, max_length=100, exclude_if=lambda v: v is None,
        title="泵浦光路标识", description="有光线输入时指定其发射光路。使用工程高斯分布时留空可比较各入射光路。",
        json_schema_extra={"x-laserclaw-input":"path-uid"})

    @model_validator(mode="after")
    def distinct_paths(self):
        if self.pump_bundle is not None and self.pump_uid is None:
            raise ValueError("请选择这组光线发射时所用的泵浦光路，核对入口参考面与 T/S 方向。")
        if self.pump_uid == self.signal_uid:
            raise ValueError("泵浦和信号需要两条独立登记的光路。")
        return self


def _beam_axis(beam, axis):
    state = beam.get("field_axes", {}).get(axis, {})
    radius, q = beam.get("radii_mm", {}).get(axis), state.get("q_mm")
    if beam.get("width_basis", {}).get(axis) != "gaussian":
        raise ValueError(beam.get("notes",{}).get(axis) or "需要明确的高斯横向强度分布，仅凭 M² 无法求出重叠。")
    if (not radius or not q or q[1] <= 0 or not beam["index"]
            or state.get("center_mm") is None or state.get("reduced_angle") is None):
        raise ValueError(beam["notes"].get(axis) or "请补充源面波前、介质折射率和束心指向，或核对腔模是否稳定。")
    return radius, q[0], q[1], state["center_mm"], state["reduced_angle"] / beam["index"]


def _ray_overlap(geometry, uid, plane_uid, source, length, depth, signal_axes, row, warnings):
    state, index, transport = weighted_rays_at_plane(geometry, uid, plane_uid, source)
    row.update(distribution="加权非相干光线", source_plane=source.plane, source_basis=source.basis,
               source_quality=source.quality, quality=transport["quality"], medium_n=index,
               paraxial_sine_slope_difference=transport["paraxial_sine_slope_difference"])
    warnings.extend(transport["warnings"])
    if transport["quality"] == "limited":
        raise ValueError("光线来源或沿途传输已超出适用条件。请核对来源记录和近轴范围后再计算面积。")
    if transport["quality"] == "conditional":
        warnings.append(f"「{row['pump']}」的光线分布或光路采用条件假设。面积沿用这组输入和配准依据：{source.basis}")
    return weighted_ray_area(length, depth, signal_axes, state.rays, index, state.split)


def spatial_overlap(args):
    layout = args.layout
    if not layout.system.elements:
        return {"status":"needs_input", "message":"需要包含均匀介质的顺序光路。"}
    paths = {p.uid: p for p in layout.incident_paths}
    if args.signal_uid != "primary" and args.signal_uid not in paths:
        raise ValueError("所选信号光束不在这个工程中，请核对光路标识。")
    budgets = [(p.uid, p.name, p.wavelength_nm, p.power_budget) for p in paths.values()]
    if layout.system.mode == "single_pass":
        budgets.append(("primary","主光路",layout.system.wavelength_nm,layout.single_pass_power))
    if args.pump_uid is not None:
        budgets = [b for b in budgets if b[0] == args.pump_uid]
        if not budgets:
            raise ValueError("请选择工程中已有的入射支路或单程主光路作为泵浦。")
    stages = [(uid, name, wl, budget, stage) for uid, name, wl, budget in budgets if budget and uid != args.signal_uid
              for stage in budget.stages if stage.scope == "host" and isinstance(stage.law, BulkLoss)]
    if not stages:
        return {"status":"needs_input", "message":"请在与信号光束相连通的介质段上，给另一条光路指定体吸收系数或有吸收归因的内透过率。"}
    elements = {e.uid: e for e in layout.system.elements if e.kind == "space"}
    ids = list(dict.fromkeys(stage.element_uid for *_, stage in stages))
    probes = [ObservationPlane(uid=f"sample-{i}", name="均匀介质入口", target_uid=uid, depth_mm=0) for i, uid in enumerate(ids)]
    entrances = {p.target_uid: p.uid for p in probes}
    geometry = layout.model_copy(update={"observation_planes": probes, "single_pass_power": None,
        "incident_paths": [p.model_copy(update={"power_budget":None}) for p in paths.values()]})
    result = analyze_sections(geometry, analyze_system(layout.system, profile=False), include_q=True)
    sampled = {s["target_uid"]: {b["uid"]: b for b in s["beams"]} for s in result["planes"]}
    rows, warnings = [], []
    for uid, name, wave, budget, stage in stages:
        host = elements.get(stage.element_uid)
        if wave is None and args.pump_bundle is not None:
            wave = args.pump_bundle.wavelength_nm
        row = {"pump_uid": uid, "pump": name, "signal_uid": args.signal_uid, "element_uid": stage.element_uid,
               "element": host.name if host else stage.element_uid, "pump_nm": wave, "site":sampling_site(uid, "host", stage.element_uid)}
        rows.append(row)
        try:
            beams = sampled.get(stage.element_uid, {})
            if not host or not host.length_mm or args.signal_uid not in beams:
                raise ValueError("两束光需在同一正长度均匀介质内连通。")
            if wave is None or budget.wavelength_nm is None or not math.isclose(wave, budget.wavelength_nm, rel_tol=1e-12):
                raise ValueError("泵浦光路与该段吸收数据的波长需一致。")
            if stage.law.alpha_per_mm is None and not stage.law.internal_loss_is_absorption:
                raise ValueError("需说明内透过损耗中可归于体吸收的部分。")
            depth = optical_depth_bounds(stage.law, host.length_mm)
            if not depth or depth[0] != depth[1] or not math.isfinite(depth[0]) or depth[0] <= 0:
                raise ValueError("此计算需要单一、有限、正的体吸收系数。吸收有区间时可分别建立工况。")
            signal = beams[args.signal_uid]
            signal_axes = [_beam_axis(signal, axis) for axis in AXES]
            row.update(signal_nm=signal["wavelength_nm"], length_mm=host.length_mm, optical_depth=depth[0])
            if args.pump_bundle is not None:
                row.update(_ray_overlap(geometry, uid, entrances[stage.element_uid], args.pump_bundle,
                                        host.length_mm, depth[0], signal_axes, row, warnings))
                warnings.extend(_scope_warnings(geometry, None, (args.signal_uid,), beams))
            else:
                if uid not in beams:
                    raise ValueError("该泵浦光路未到达信号所在的介质段。")
                axes = [(_beam_axis(beams[uid], axis), s) for axis, s in zip(AXES, signal_axes)]
                area, error = weighted_gaussian_area(host.length_mm, depth[0], axes)
                row.update(area_mm2=area, relative_quadrature_error=error, distribution="工程高斯分布")
                warnings.extend(_scope_warnings(geometry, None, (uid, args.signal_uid), beams))
            row["status"] = "completed"
        except FloatingPointError:
            row.update(status="needs_input", message="光线位置、斜率或传输矩阵超出数值范围。请核对光路尺度与输入单位。")
        except (ValueError, ArithmeticError) as exc:
            row.update(status="needs_input", message=str(exc))
    good = sum(r["status"] == "completed" for r in rows)
    scope = (["泵浦采用给定的非相干光线权重。发射面坐标中已包含偏心与指向，经耦合元件和入口孔径到达各均匀段。每段按到达的几何权重归一化，均匀表面和前段的标量功率损耗需另计。信号采用归一化高斯强度。",
              "Beer 吸收以共向近轴的轴向距离计算，忽略饱和、热致变化及泵浦衍射。积分误差只反映纵向数值求积。"]
             if args.pump_bundle is not None else ["采用共向、共面、可分离的近轴高斯强度与均匀介质内的指数吸收，忽略孔径截断。"])
    return {"status":"completed" if good == len(rows) else "partial" if good else "needs_input", "rows":rows,
            "summary":f"在 {len(rows)} 组泵浦与样品中，{good} 组获得了沿程吸收加权的模式重叠面积。",
            "warnings":list(dict.fromkeys(warnings)), "assumptions":scope}


def _check_samples(pump, values):
    if len(pump) != len(values) or any(a >= b for a, b in zip(pump, pump[1:])):
        raise ValueError("吸收泵浦功率与增益数据须一一对应，泵浦功率严格递增。升降扫或不同工况请分开建曲线。")


class ProbeGain(GainConditions):
    absorbed_w: list[Nonnegative] = Field(min_length=2, max_length=4096, title="实测吸收泵浦功率 / W")
    power_ratio: list[Positive] = Field(min_length=2, max_length=4096, title="弱探针单程功率增益倍数",
        description="同一模式与参考面下，探针出射功率除以入射功率。填写线性功率比，并注明已计入哪些无源损耗。")

    @model_validator(mode="after")
    def ordered(self):
        _check_samples(self.absorbed_w, self.power_ratio)
        return self


class ModalGain(GainConditions):
    absorbed_w: list[Nonnegative] = Field(min_length=2, max_length=4096)
    log_gain: list[float] = Field(min_length=2, max_length=4096, description="Natural logarithm of single-pass modal power gain.")
    method: Note

    @model_validator(mode="after")
    def ordered(self):
        _check_samples(self.absorbed_w, self.log_gain)
        return self


class Roundtrip(StrictInput):
    curve: ModalGain = Field(title="单程小信号增益曲线")
    signal_nm: Wavelength = Field(title="往返信号波长 / nm")
    traversals: int = Field(ge=1, title="每次往返的等效通过次数",
        description="各次通过须具有相同的小信号模增益。按实际路径填写。")
    survival: tuple[Share, Share] = Field(title="其余往返无源功率保留比例",
        description="包含输出耦合及其他未计入所用增益曲线的损耗。指定单值或上下界。")
    operating_w: tuple[Nonnegative, Nonnegative] | None = Field(default=None, title="运行点吸收泵浦功率 / W",
        description="可引用工程中相应增益段实际吸收的泵浦功率。")
    basis: Note = Field(title="往返路径与损耗依据", description="注明待判断的模式、通过方向、保留比例的来源，及增益曲线已经包含的损耗。")

    @model_validator(mode="after")
    def common_mode(self):
        if not math.isclose(self.signal_nm, self.curve.signal_nm, rel_tol=1e-12):
            raise ValueError(f"小信号增益对应 {self.curve.signal_nm:g} nm，往返信号为 {self.signal_nm:g} nm。请选取同一信号波长的曲线。")
        if self.survival[0] > self.survival[1] or self.operating_w and self.operating_w[0] > self.operating_w[1]:
            raise ValueError("无源保留比例和运行点吸收功率的上下界须按升序填写。")
        return self


GAIN = Port(quantity="laser.single-pass-modal-log-power-gain.v1", label="单程小信号模增益曲线",
    unit="ln-power-ratio.vs-absorbed-W.v1", shape="state", scale=1,
    registration_label="增益样品、模式及测量工况与本次往返的对应关系")
RETENTION = Port(quantity="optics.roundtrip-passive-power-survival", label="往返无源功率保留比例", unit="1", scale=1, shape="interval")
AREA = Port(quantity="laser.absorption-weighted-modal-overlap-area", label="吸收加权有效重叠面积",
    unit="mm²", scale=1e-6, conditions={
        "optics.pump-vacuum-wavelength.nm": Condition(path="pump_nm", label="泵浦波长 / nm"),
        "optics.signal-vacuum-wavelength.nm": Condition(path="signal_nm", label="信号波长 / nm")})
gain_chart = ("吸收泵浦功率 / W", "ln(单程功率增益)", [("模增益", "curve.absorbed_w", "curve.log_gain", "lines+markers")])

GAIN_STUDIES = (
    Study("absorbed_mode_area", "沿增益段计算泵浦与信号重叠", "1",
        "用工程高斯泵浦或已配准的加权非相干光线，计算沿均匀吸收介质与高斯信号模式的有效重叠面积。按泵浦支路与介质段保留结果。",
        SpatialOverlap, spatial_overlap, input_ports={"layout": LAYOUT, "pump_bundle": RAY_BUNDLE}, live_layout="layout",
        ignores_diagram_positions=True,
        table=("rows", {"pump":"泵浦光路", "element":"介质段", "pump_nm":"泵浦 / nm", "signal_nm":"信号 / nm",
                        "distribution":"泵浦分布", "area_mm2":"有效面积 / mm²",
                        "entrance_weight":"入口几何权重 / 初始发射", "message":"待补条件"}),
        row_outputs={"rows": RowOutputs(columns={"area_mm2":AREA.model_copy(update={
            "conditions":{**AREA.conditions, PUMP_SITE:SITE_CONDITION}, "inherit_conditions_from":"layout"})},
            coordinates={"pump":"泵浦","element":"样品"}, identity_fields=("pump_uid","element_uid","signal_uid"), available_on_partial=True)}),
    Study("four_level_modal_gain", "稳态四能级小信号模增益", "1",
        "给定有效发射截面、寿命、体积加权重叠面积及量子产额，从已吸收的泵浦光子数求指定模式的单程小信号增益。",
        FourLevel, four_level_gain, {"log_gain_per_w": "单程对数功率增益斜率 / W⁻¹"}, chart=gain_chart,
        input_ports={"pump_nm": WAVELENGTH, "signal_nm": WAVELENGTH, "area_mm2":AREA},
        output_ports={"curve": GAIN.model_copy(update={"inherit_conditions_from":"area_mm2"})}),
    Study("measured_modal_gain", "实测弱探针增益曲线", "1",
        "对同一工况的吸收泵浦功率和单程弱探针功率比建立分段曲线，保留非单调响应。曲线仅在采样区间内使用。",
        ProbeGain, measured_gain, chart=gain_chart,
        input_ports={"pump_nm": WAVELENGTH, "signal_nm": WAVELENGTH,
                     "absorbed_w": at_wavelength(ABSORBED.model_copy(update={"shape": "series", "alternate_units": POWER_SCALE}), "pump_nm"),
                     "power_ratio": at_wavelength(Port(quantity="laser.weak-probe-power-ratio", label="探针单程功率比",
                                                       unit="1", scale=1, shape="series"), "signal_nm")},
        output_ports={"curve": GAIN}, paired_inputs=(("absorbed_w", "power_ratio"),)),
    Study("cw_roundtrip_balance", "连续波小信号往返平衡", "1",
        "把单程模增益、等效通过次数与其余往返无源保留比例合成。给出所选泵浦范围内全部平衡点或区间，判断指定运行点的增长或衰减。",
        Roundtrip, roundtrip_balance, {"operating_label": "运行点判定"},
        table=("balance_bands", {"start_w": "平衡起点 / W", "stop_w": "平衡终点 / W"}),
        chart=("吸收泵浦功率 / W", "ln(往返功率倍率)", [
            ("净增益", "curve_plot.absorbed_w", "curve_plot.net", "lines"),
            ("净增益下界", "curve_plot.absorbed_w", "curve_plot.lower", "lines+markers"),
            ("净增益上界", "curve_plot.absorbed_w", "curve_plot.upper", "lines+markers"),
            ("平衡线", "curve_plot.absorbed_w", "curve_plot.balance", "lines"),
            ("平衡点及区间边界", "curve_plot.edges_w", "curve_plot.zero_at_edges", "markers"),
            ("指定运行范围", "curve_plot.operating_w", "curve_plot.operating_log", "markers")]),
        input_ports={"curve": GAIN, "signal_nm": WAVELENGTH, "operating_w": at_wavelength(ABSORBED, "curve.pump_nm"),
                     "survival": RETENTION},
        condition_matches=(ConditionMatch(anchor="curve", other="operating_w", keys={
            MODEL_REVISION:"工程版本", PUMP_SITE:"泵浦支路与样品段"}),)),
)

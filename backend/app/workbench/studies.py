"""Registration, discovery and case history for deterministic, explicit-input scientific studies."""
import hashlib
import json
import logging
import re
from collections import Counter
from contextlib import ExitStack, contextmanager
from importlib.metadata import entry_points

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import AgentTask, GeneratedContent
from ..solver_receipt import solver_cache_key, solver_receipt
from ..version import provider_display_name
from pydantic import BaseModel, Field, TypeAdapter, model_validator

from ..study_api import Condition, MissingStudyInput, OriginalFile, Port, PublishedPort, ResultReference, RowOutputs, StrictInput, Study
from ..physics.caustic import UnresolvedCaustic, fit_caustic
from .beam_holdout import HoldoutStations, assess_holdout, partition_stations
from ..physics.laser_metrics import fit_power_curve
from ..physics.measurement_data import LENGTH_UNITS, POWER_SCALE
from ..physics.radial_heat import HEAT_REQUIREMENTS, radial_heating
from ..physics.toolkit import run_coating_tmm, run_phase_match
from .contracts import RadialHeating, CausticData, Coating, PhaseMatch, PowerCurve
from .gain_studies import GAIN_STUDIES
from .image_study import IMAGE_MOMENTS
from .centroid_comparison import CENTER_COMPARISON
from .map_comparison import MAP_COMPARISON
from .mode_overlap import SECTION_MODE_STUDY
from .transverse_registration import REGISTRATION_FIT
from .focal_study import LENS_FOCAL_STUDY
from .beam_predictions import CausticPrediction, MeasuredCaustic, project_caustic
from .standard_devices import STANDARD_DEVICES
from .wave_study import FIELD_MAP_STUDY, WAVE_STUDY
from .ray_source import GENERATE_RAY_SOURCE
from .ray_study import RAY_STUDY
from .ray_tables import RAY_TABLE_STUDY
from .spatial_ports import SPATIAL_PORT_STUDY
from .observation import OpticalLayout, analyze_sections
from .network import NetworkStudy, evaluate_network
from .optics import ParaxialField, PropagationStudySystem, analyze_system
from .beam_source import PARAXIAL_BEAM, medium_beam_output, transport_beam
from .medium_beam import MEDIUM_STUDY, register_fitted_plane
from .layout_prefix import select_prefix_endpoint
from .table_inputs import FileColumnReference, resolve_table_inputs, table_group
from .grid_export import grid_digest, grid_summary, grid_view, native_grid_access, write_grid_file
from .measurement_sources import case_original_snapshot
from .project_outputs import ABSORBED, LENGTH, OUTGOING_POWER, WAVELENGTH, FrozenProjectOutputs, ProjectReference, at_wavelength

InputSource = ResultReference | FileColumnReference | ProjectReference
SOURCE = TypeAdapter(InputSource)
logger = logging.getLogger(__name__)


def study_solver_receipt(study, provider, *, inputs=None, devices=(), scope=None):
    hooks = tuple(hook for hook in (study.inputs, study.saved_outputs, study.native_grid) if hook is not None)
    fields = ("input_ports", "output_ports", "row_outputs", "condition_matches", "output_requirements",
              "paired_inputs", "registration_inputs", "text_column_inputs", "element_law", "layout_prefix_inputs")
    definition = study.presentation(provider)
    contract = {key: definition[key] for key in fields if key in definition}
    return solver_receipt(study.name, provider, study.version, compute=study.compute, related=hooks,
                          inputs=inputs, devices=devices, contract=contract, scope=scope)


def installed_study_receipt(definition, saved, registry):
    name = definition.get("name") if isinstance(definition, dict) else None
    entry = registry.get(name) if isinstance(name, str) else None
    if not entry:
        return None
    # A legacy record can name an installed method without identifying the devices in its old calculation.
    if (isinstance(saved, dict) and saved.get("schema") == 1 and not saved.get("scope")
            and isinstance(saved.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", saved["sha256"])
            and (saved.get("method"), saved.get("provider"), saved.get("version"))
                == (name, definition.get("provider"), definition.get("version"))):
        devices = saved.get("devices", [])
        if isinstance(devices, list) and all(isinstance(device, dict) and all(
                isinstance(device.get(key), str) and device[key] for key in ("method", "provider", "version"))
                for device in devices):
            return study_solver_receipt(*entry, devices=devices)
    return study_solver_receipt(*entry, scope="method_only")


class StudyFailure(RuntimeError):
    """An installed implementation failed after its inputs were validated."""


class StudyInvocation(StrictInput):
    version: str
    provider: str
    inputs: dict
    connections: dict[str, InputSource] = Field(default_factory=dict)
    reuse_inputs_from: int | None = Field(default=None, gt=0, description="Use saved inputs and references from the same method and provider, or a declared compatible earlier version. Leave inputs and connections empty.")

    @model_validator(mode="after")
    def separate_saved_inputs(self):
        if self.reuse_inputs_from is not None and (self.inputs or self.connections):
            raise ValueError("复用历史输入时，输入和引用均取自所选记录。要修改条件，请另开一次计算。")
        return self


class ConnectedStudy(StudyInvocation):
    study: str

    @model_validator(mode="after")
    def require_source(self):
        if not self.connections and self.reuse_inputs_from is None:
            raise ValueError("请选择已存输入记录或提供至少一个输入引用。")
        return self


FIELD = Port(quantity="optics.centered-paraxial-field.ts-air.v1", label="同轴双平面光束状态",
             unit="air.aligned-forward-ts.v1", scale=1, shape="state")
POWER_SERIES = Port(quantity="optics.power", label="光功率", unit="W", scale=1, shape="series",
                    alternate_units=POWER_SCALE)
POSITION = Port(quantity="optics.axial-position", label="轴向位置", unit="mm", scale=.001, shape="series", alternate_units=LENGTH_UNITS)
RADIUS = Port(quantity="optics.beam-radius", label="光束半径", unit="mm", scale=.001, shape="series", alternate_units=LENGTH_UNITS)
CAUSTIC_STATE = Port(quantity="optics.measured-caustic.v1", label="实测焦散几何模型", unit="axial-mm.radius-mm.half-rad.v1",
                     scale=1, shape="state", registration_label="预测位置与原测量的零点、正方向、横向轴及均匀介质怎样对应")
PREDICTED_RADIUS = RADIUS.model_copy(update={"quantity":"optics.predicted-beam-radius","label":"预测光束半径"})
BEAM_FIT_PORTS = {
    "geometry": CAUSTIC_STATE,
    "registered_beam": medium_beam_output("registered_beam"),
    "waist_z_mm": Port(quantity="optics.fitted-waist-coordinate", label="原测量坐标中的束腰位置", unit="mm", scale=.001),
    "waist_radius_mm": Port(quantity="optics.fitted-waist-radius", label="按实测宽度口径拟合的束腰半径", unit="mm", scale=.001),
    "divergence_half_angle_rad": Port(quantity="optics.fitted-divergence-half-angle", label="几何发散半角", unit="rad", scale=1),
    "bpp_mm_mrad": Port(quantity="optics.fitted-radius-angle-product", label="束腰半径与发散半角乘积", unit="mm·mrad", scale=1e-6),
    "m2": Port(quantity="optics.width-based-propagation-factor", label="按实测宽度口径换算的 M²", unit="1", scale=1),
}


def _propagate(args):
    output = analyze_system(args, stamp=False)
    if args.mode != "single_pass":
        return output
    source = args.primary_source()
    warnings = list(dict.fromkeys([*getattr(source, "model_warnings", ()), *output.get("model_warnings", ())]))
    if warnings:
        output["model_warnings"] = warnings
    if output["intensity_model"] == "unclipped_reference":
        return output
    if args.source or args.input_beam or args.input_field or args.input_radius_mm is not None or args.input_curvature_mm is not None:
        try:
            beam = transport_beam(source, args.wavelength_nm, {name: plane["matrix"] for name, plane in output["planes"].items()},
                                  model_warnings=warnings)
        except (ArithmeticError, ValueError):
            output.setdefault("model_warnings", []).append("累计传输矩阵或光束状态超出可核实的数值范围，无法提供逐轴出射状态。")
        else:
            ray_known = beam.launch_ray and any(value is not None for value in beam.launch_ray.model_dump().values())
            if (beam.origins or ray_known or any(axis.radius_mm is not None or axis.wavefront != "unknown"
                                                 for axis in (beam.tangential, beam.sagittal))):
                output["exit_beam"] = beam.model_dump(mode="json")
    t, s = source.tangential, source.axis("sagittal")
    legacy = (t.m2 == s.m2 and not getattr(source, "model_warnings", ())
              and not any(axis.fit_origin or axis.second_moment_radius or axis.equivalent_radius for axis in (t, s))
              and not (source.launch_ray and any(value != 0 for value in source.launch_ray.model_dump().values())))
    if output["status"] == "completed" and legacy:
        axes = {}
        for axis, plane in output["planes"].items():
            point = plane["output"]
            axes[axis] = {"real_mm": point["q_real_mm"], "imag_mm": point["q_imag_mm"]}
        output["exit_field"] = ParaxialField(frame="air.aligned-forward-ts.v1", wavelength_nm=args.wavelength_nm,
                                             beam_quality_m2=t.m2, **axes).model_dump()
    return output


FOCAL_DISTANCES = {
    f"planes.{axis}.collimated_focus_from_exit_mm": Port(
        quantity=f"optics.collimated-focus-from-exit.{axis}", label=f"{label}平行入射几何焦点距末面（向后为正）",
        unit="mm", scale=.001, alternate_units=LENGTH_UNITS, available_on_partial=True)
    for axis, label in (("tangential","切向"), ("sagittal","弧矢"))
}

END_RADII = {
    f"planes.{axis}.output.radius_mm": Port(
        quantity=f"optics.paraxial-exit-radius.{axis}", label=f"{label}末面光束半径", unit="mm", scale=.001,
        alternate_units=LENGTH_UNITS, available_on_partial=True,
        publish_when={"intensity_model": ("ideal_gaussian", "width_only")},
        conditions={"optics.width-basis": Condition(path=f"planes.{axis}.width_basis", label="束宽口径", value_type="text")},
        comparison_conditions=("optics.width-basis",))
    for axis, label in (("tangential", "切向 T"), ("sagittal", "弧矢 S"))
}


def _power(args):
    if len(args.pump_w) != len(args.output_w) or min(args.pump_w + args.output_w) < 0 or len(set(args.pump_w)) < 3:
        raise ValueError("功率数组必须等长、非负且至少包含三个不同泵浦功率。")
    fit = fit_power_curve(args.pump_w, args.output_w)
    data = {"pump_w": args.pump_w, "output_w": args.output_w}
    if line := fit.get("fit_line"):
        low, high = min(args.pump_w), max(args.pump_w)
        data["fit_pump_w"] = sorted({low, high, max(low, min(high, fit["threshold_pump"]))})
        data["fit_output_w"] = [max(0.0, line["a"] * p + line["b"]) for p in data["fit_pump_w"]]
    return {**fit, "pump_basis": args.pump_basis, "data_source": args.data_source, "data": data}


def _caustic(args):
    z, radius = args.z_mm, args.radius_mm
    held = None
    if args.holdout_positions_mm:
        readings = [{"input_index": i+1, "z_mm": at, "radius_mm": width, "raw_width": width}
                    for i, (at, width) in enumerate(zip(z, radius, strict=True))]
        if args.acquisition_ids is not None:
            for reading, group in zip(readings, args.acquisition_ids, strict=True):
                reading["acquisition_id"] = group
        training, held = partition_stations(readings, HoldoutStations(positions_mm=args.holdout_positions_mm))
        z = [r["z_mm"] for r in training]
        radius = [r["radius_mm"] for r in training]
    try:
        fit = fit_caustic(z, radius, args.wavelength_nm, args.medium_n, loss=args.loss)
    except UnresolvedCaustic as exc:
        if not exc.repeatability["repeated_positions"]:
            raise
        return {"status":"partial", "repeatability":exc.repeatability, "source":args.data_source,
                "warnings":[str(exc)], "output_limits":dict.fromkeys(BEAM_FIT_PORTS,str(exc)),
                "data":{"z_mm":z,"radius_mm":radius}, **({"holdout":held} if held else {})}
    if held:
        assess_holdout(held, fit, {"width_definition":"radius", "width_unit":"mm"})
        fit["holdout"] = held
        fit["data"]["held_z_mm"] = [p["z_mm"] for p in held["points"]]
        fit["data"]["held_radius_mm"] = [p["radius_mm"] for p in held["points"]]
    geometry = MeasuredCaustic(waist_radius_mm=fit["waist_radius_mm"], waist_z_mm=fit["waist_z_mm"],
                               divergence_half_angle_rad=fit["divergence_half_angle_rad"], sampled_range_mm=(min(z),max(z)),
                               measurement_basis=args.data_source, prediction_warnings=fit["prediction_warnings"])
    assumptions = [f"按真空波长 {args.wavelength_nm:g} nm 和介质折射率 {args.medium_n:g} 换算 M²。"] if fit["m2"] is not None else []
    if held:
        assumptions.extend([held["grouping_note"], held["uncertainty_note"]])
    register_fitted_plane(fit, geometry.sampled_range_mm, args)
    return {**fit, "status": fit["quality"], "source": args.data_source, "geometry":geometry.model_dump(),
            "warnings":fit["fit_warnings"], "assumptions":assumptions}


def _sections(args):
    result = analyze_sections(args, analyze_system(args.system, profile=False, stamp=False))
    plane_names = {plane.uid: plane.name for plane in args.observation_planes}
    beam_names = {"primary": "主光束", **{path.uid: path.name for path in args.incident_paths}}
    for plane in result["planes"]:
        plane["name"] = plane_names[plane["uid"]]
        for beam in plane["beams"]:
            beam["name"] = beam_names[beam["uid"]]
    return result


def _phase_match(args):
    result = run_phase_match(args.model_dump(exclude_none=True))
    if result.get("matched", 0) > 0:
        other_nm = args.lambda2_nm or args.lambda1_nm
        result["matched_wavelength_nm"] = 1 / (1 / args.lambda1_nm + 1 / other_nm)
    return result


BUILTINS = (
    *STANDARD_DEVICES,
    MEDIUM_STUDY,
    WAVE_STUDY,
    GENERATE_RAY_SOURCE, RAY_TABLE_STUDY, RAY_STUDY,
    FIELD_MAP_STUDY,
    LENS_FOCAL_STUDY,
    SPATIAL_PORT_STUDY,
    IMAGE_MOMENTS,
    CENTER_COMPARISON,
    MAP_COMPARISON,
    SECTION_MODE_STUDY,
    REGISTRATION_FIT,
    *GAIN_STUDIES,
    Study("solve_port_network", "光学端口网络", "3", "在同一波长以被动散射矩阵计算端口间的相干传播。每个端口对应一个模式，连接两端须核对空间与偏振基底。独立光源按功率相加。工程内可以引用顺序光路的出射功率。",
          NetworkStudy, lambda args: evaluate_network(args, args.wavelength_nm),
          input_ports={"wavelength_nm": WAVELENGTH},
          output_ports={"wavelength_nm": WAVELENGTH},
          row_outputs={"ports": RowOutputs(columns={"outgoing_w": at_wavelength(OUTGOING_POWER.model_copy(update={"label":"端口出射功率"}))},
                                          coordinates={"name":"元件", "port":"端口"}, identity_fields=("component","port"),
                                          available_on_partial=True)},
          table=("ports", {"name":"元件", "port":"端口", "incoming_w":"入射 / W", "outgoing_w":"出射 / W"})),
    Study("radial_heat_balance", "圆柱介质稳态径向温升", "2",
          "侧面冷却、端面绝热、均匀且径向热导率为常数的圆柱。输入该段实际体吸收的功率范围和热转化比例，计算沿长度平均的温差。",
          RadialHeating, radial_heating, {"heat_w":"热功率 / W","axis_to_surface_k":"轴心相对侧壁温升 / K",
                                      "surface_to_sink_k":"侧壁相对热沉温升 / K","axis_to_sink_k":"轴心相对热沉温升 / K"},
          input_ports={"absorbed_w":ABSORBED,"length_mm":LENGTH,
                       "heat_fraction":Port(quantity="thermal.fraction-of-absorption",label="热转化比例",unit="1",scale=1,shape="interval"),
                       "zone_edges_mm":Port(quantity="geometry.radial-position",label="环区外半径",unit="mm",scale=.001,shape="series",alternate_units=LENGTH_UNITS),
                       "relative_density":Port(quantity="thermal.relative-volume-heating",label="相对热源密度",unit="1",scale=1,shape="series")},
          paired_inputs=(("zone_edges_mm","relative_density"),),
          output_ports={"heat_w":Port(quantity="thermal.heat-flow",label="热功率",unit="W",scale=1,shape="interval"),
                        **{key:Port(quantity="thermal.temperature-difference",label=label,unit="K",scale=1,shape="interval")
                           for key,label in (("axis_to_surface_k","轴心相对侧壁温差"),("surface_to_sink_k","侧壁相对热沉温差"),
                                             ("axis_to_sink_k","轴心相对热沉温差"))}},
          output_requirements=HEAT_REQUIREMENTS,
          chart=("径向位置 / mm","相对侧壁温升 / K",[("下界","profile.radius_mm","profile.low_k","lines"),
                                                   ("上界","profile.radius_mm","profile.high_k","lines")])),
    Study("fit_power_curve", "泵浦与输出功率拟合", "2", "从同一工况的功率对估计阈值和斜率效率。标明泵浦口径和数据来源。", PowerCurve, _power,
          {"threshold_pump": "拟合阈值 / W", "slope_efficiency_pct": "斜率效率 / %", "model_r_squared": "整体拟合 R²"},
          chart=("泵浦功率 / W", "输出功率 / W", [("输入数据", "data.pump_w", "data.output_w", "markers"),
                                            ("分段拟合", "data.fit_pump_w", "data.fit_output_w", "lines")]),
          input_ports={"pump_w": POWER_SERIES, "output_w": POWER_SERIES}, paired_inputs=(("pump_w", "output_w"),)),
    Study("fit_beam_caustic", "实测光束焦散拟合", "5", "在同一均匀传播段用实测半径拟合束腰和发散。可指定留用的轴向位置，在这些位置核对预测误差。M² 还需要真空波长和对应折射率，二阶矩解释取决于宽度口径。", CausticData, _caustic,
          {"waist_z_mm": "束腰位置 / mm", "waist_radius_mm": "束腰半径 / mm", "divergence_half_angle_rad": "发散半角 / rad",
           "bpp_mm_mrad": "束腰半径 × 发散半角 / mm·mrad", "m2": "等效 M²",
           "repeatability.pooled_within_sd_mm":"同位置读数的合并标准差 / mm",
           "repeatability.station_mean_fit_rms_mm":"各训练位置平均半径与拟合的均方根差 / mm",
           "holdout.station_rms_radius_mm":"留出位置的半径均方根误差（位置等权） / mm"},
          table=("holdout.points", {"input_index":"输入序号", "acquisition_id":"采集标识", "z_mm":"位置 / mm", "radius_mm":"实测半径 / mm",
                                    "predicted_radius_mm":"预测半径 / mm", "residual_mm":"实测减预测 / mm", "extrapolated":"外推"}),
          table_input_rows={"z_mm":"input_index", "radius_mm":"input_index", "acquisition_ids":"input_index"},
          chart=("轴向位置 / mm", "光束半径 / mm", [("拟合用测点", "data.z_mm", "data.radius_mm", "markers"),
                                              ("留出测点", "data.held_z_mm", "data.held_radius_mm", "markers"),
                                              ("拟合", "data.curve_z_mm", "data.curve_radius_mm", "lines")]),
          accepts_inputs_from=(("LaserClaw", "4"),),
          input_ports={"z_mm": POSITION, "radius_mm": RADIUS, "wavelength_nm": WAVELENGTH},
          paired_inputs=(("z_mm", "radius_mm"), ("z_mm", "radius_mm", "acquisition_ids")),
          output_ports=BEAM_FIT_PORTS, output_requirements={**{k: ("z_mm","radius_mm","wavelength_nm","medium_n")
              if k == "m2" else ("z_mm","radius_mm") for k in BEAM_FIT_PORTS},
              "registered_beam": ("z_mm","radius_mm","wavelength_nm","medium_n","register_plane")}),
    Study("predict_caustic_widths", "由实测焦散预测光斑", "2", "沿原测量的横向轴、宽度口径和连续均匀介质，按拟合曲线预测指定位置的半径。区间外标为外推。", CausticPrediction, project_caustic,
          input_ports={"geometry": CAUSTIC_STATE, "positions_mm": POSITION},
          output_ports={"radius_mm":PREDICTED_RADIUS},
          row_outputs={"points":RowOutputs(columns={"radius_mm":PREDICTED_RADIUS.model_copy(update={"shape":"scalar"})},
                                           coordinates={"z_mm":"位置 / mm"},identity_fields=("z_mm",),where={"extrapolated":False},available_on_partial=True)},
          table=("points", {"z_mm":"位置 / mm", "radius_mm":"半径 / mm", "diameter_mm":"全宽 / mm", "extrapolated":"外推"})),
    Study("compute_phase_match", "非线性晶体相位匹配", "2", "按内置室温色散数据求倍频或和频的相位匹配角与走离。双轴晶体搜索主平面。", PhaseMatch,
          _phase_match, {"matched": "匹配解数", "matched_wavelength_nm": "有匹配解的和频波长 / nm"},
          table=("solutions", {"plane": "平面", "theta_deg": "θ / °", "phi_deg": "φ / °", "walkoff_mrad": "走离 / mrad", "confidence": "置信度"}),
          input_ports={"lambda1_nm": WAVELENGTH, "lambda2_nm": WAVELENGTH},
          output_ports={"matched_wavelength_nm": WAVELENGTH}),
    Study("compute_coating", "镀膜光谱", "2", "按给定的入射介质、基底及有序膜层求解反射与透射；也可采用实测曲线或标称模型。", Coating,
          lambda args: run_coating_tmm(args.config), accepts_inputs_from=(("LaserClaw", "1"),),
          table=("query_results", {"wavelength_nm": "波长 / nm", "R": "反射率 R", "T": "透射率 T",
                                   "A": "膜层吸收率 A", "reason": "适用范围"})),
    Study("analyze_optical_system", "近轴光路传播与腔模", "2", "计算双轴 ABCD 矩阵、单程平行入射几何焦点或腔本征模。几何传输可留空入射束径；光斑和输出光束状态需要入射场条件。末端参考面在空气中。", PropagationStudySystem, _propagate,
          {"stable_both_planes": "双轴稳定", "planes.tangential.output.radius_mm": "切向末端半径 / mm", "planes.sagittal.output.radius_mm": "弧矢末端半径 / mm",
           **{key: port.label+" / mm" for key, port in FOCAL_DISTANCES.items()}},
          chart=("位置 / mm", "光束半径 / mm", [(label, f"planes.{axis}.samples.*.z_mm", f"planes.{axis}.samples.*.radius_mm", "lines")
                                               for axis, label in (("tangential", "切向 T"), ("sagittal", "弧矢 S"))]),
          input_ports={"input_beam": PARAXIAL_BEAM, "input_field": FIELD, "wavelength_nm": WAVELENGTH},
          output_ports={"exit_beam": PARAXIAL_BEAM, "exit_field": FIELD, "wavelength_nm": WAVELENGTH, **FOCAL_DISTANCES, **END_RADII}, view="optics.paraxial-profile.v1"),
    Study("analyze_observation_planes", "光路与观察面", "2", "沿已定义的主光路与入射支路计算具名截面的光场。", OpticalLayout, _sections,
          table=("planes", {"name": "观察面", "z_mm": "位置 / mm", "beams.*.name": "光束",
                            "beams.*.wavelength_nm": "波长 / nm", "beams.*.radii_mm.tangential": "T 半径 / mm",
                            "beams.*.radii_mm.sagittal": "S 半径 / mm", "beams.*.notes.tangential": "T 待补条件",
                            "beams.*.notes.sagittal": "S 待补条件", "message": "定位说明"})),
)


def _installed_study_entries():
    # Both selections come from the same metadata snapshot. A distribution can advertise the
    # same export to old and new hosts during migration, while genuine name collisions stay visible.
    installed = entry_points()
    current = installed.select(group="beamweft.studies.v1")
    legacy = installed.select(group="laserclaw.studies.v1")

    def export(entry):
        return entry.dist, entry.name, entry.value

    current_exports = {export(entry) for entry in current}
    return [*current, *(entry for entry in legacy if export(entry) not in current_exports)]


def discover_studies(reserved=(), plugins=None):
    """Keep installed packages separate from user-supplied project files. Ambiguous names have no winner."""
    studies = {s.name: (s, "LaserClaw") for s in BUILTINS}
    issues = []
    plugins = _installed_study_entries() if plugins is None else list(plugins)
    counts = Counter(p.name for p in plugins)
    for plugin in sorted(plugins, key=lambda p: p.name):
        try:
            if plugin.name in reserved or plugin.name in studies or counts[plugin.name] != 1:
                raise ValueError("名称冲突")
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", plugin.name):
                raise ValueError("计算名称须为有效工具标识")
            study = plugin.load()
            if not isinstance(study, Study) or study.name != plugin.name or not callable(study.compute):
                raise ValueError("入口需要导出同名 Study 定义")
            if "purpose" in study.inputs.model_fields:
                raise ValueError("输入契约无效或占用了保留字段 purpose")
            if not all(isinstance(s, str) and s.strip() for s in (study.version, study.title, study.scope)):
                raise ValueError("缺少版本、名称或适用范围")
            provider = f"{plugin.dist.name} {plugin.dist.version}"
            json.dumps(study.describe(provider), allow_nan=False)
            studies[study.name] = (study, provider)
        except Exception as exc:
            issues.append({"name": plugin.name, "reason": str(exc)})
    return studies, issues


def _output_value(output, path):
    value = output
    for part in path.split("."):
        if isinstance(value, list) and part.isdecimal() and int(part) < len(value):
            value = value[int(part)]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None
    return value


def _assess_outputs(study, output, inputs):
    if not study.output_requirements or output.get("status", "completed") not in ("completed", "partial"):
        return {}
    limits = output.get("output_limits", {})
    if not isinstance(limits, dict) or limits.keys() - study.output_requirements.keys():
        raise ValueError("计算结果中的适用限制须指向本方法声明的输出。")
    assessments = {}
    for path, fields in study.output_requirements.items():
        missing = [key for key in fields if inputs.get(key) is None]
        value = _output_value(output, path)
        if missing:
            names = [study.inputs.model_fields[key].title or key for key in missing]
            if value is not None:
                raise ValueError(f"{path} 缺少{'、'.join(names)}，求解器却给出了数值。")
            assessments[path] = {"state": "needs_input", "missing_inputs": missing, "message": "需要补充" + "、".join(names) + "。"}
        elif path in limits:
            if not isinstance(limits[path], str) or not limits[path].strip():
                raise ValueError(f"{path} 需要说明模型或测量限制。")
            assessments[path] = {"state": "limited", "message": limits[path]}
        elif PublishedPort.describe(study.output_ports[path], value) is None:
            raise ValueError(f"{path} 已具备声明的输入，但没有返回符合接口的结果或适用限制。")
        else:
            assessments[path] = {"state": "available"}
    return assessments


def _available(study, output, sources=None, assessments=None):
    status = output.get("status", "completed")
    if status not in ("completed", "partial"):
        return {}
    available = {}
    def describe(port, value, context):
        offer = PublishedPort.describe(port, value, context)
        inherited = (sources or {}).get(port.inherit_conditions_from, {})
        if offer and inherited:
            offer.condition_values = {**inherited, **offer.condition_values}
        return offer
    for path, port in study.output_ports.items():
        assessment = (assessments or {}).get(path)
        if (assessment["state"] == "available" if assessment else status == "completed" or port.available_on_partial):
            offer = describe(port, _output_value(output, path), output)
            if offer is not None:
                available[path] = offer.model_dump(mode="json")
    for table, declaration in study.row_outputs.items():
        if status == "partial" and not declaration.available_on_partial:
            continue
        rows = _output_value(output, table)
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            if any(_output_value(row, field) is not expected for field, expected in declaration.where.items()):
                continue
            location = [f"第 {index + 1} 行"]
            for field, caption in declaration.coordinates.items():
                value = _output_value(row, field)
                if type(value) in (str, int, float) and value != "":
                    location.append(f"{caption} {value}")
            for column, port in declaration.columns.items():
                offer = describe(port, _output_value(row, column), {**output, **row})
                if offer is not None:
                    path = f"{table}.{index}.{column}"
                    if path in available:
                        raise ValueError(f"计算输出 {path} 对应多个物理量声明。")
                    available[path] = {**offer.model_dump(mode="json"), "label": " · ".join([offer.label, *location])}
    return available


class UnpublishedOutput(ValueError):
    """The selected study in this case did not publish this port."""


def _saved_study_output(db, case_id, reference):
    content = GeneratedContent.content
    query = db.query(GeneratedContent).filter_by(case_id=case_id, id=reference.run_id, content_type="scientific_study")
    path = reference.port
    parts = path.split(".")
    # Portable JSON paths for the common case. Plugin keys outside this subset still
    # use the original Python traversal, including numeric dictionary keys.
    if all(re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*|0|[1-9][0-9]{0,8}", part) for part in parts):
        published = content[("available_outputs", path)]
        selected = query.filter(published.as_string().is_not(None))
        if db.get_bind().dialect.name == "sqlite":
            location = '$."output"' + "".join(f'[{part}]' if part.isdecimal() else f'."{part}"' for part in parts)
            # Multi-path extraction preserves JSON numbers and booleans as distinct types.
            pair = selected.with_entities(func.json_extract(content, f'$."available_outputs"."{path}"', location)).scalar()
            declaration, value = json.loads(pair) if pair is not None else (None, None)
        else:
            row = selected.with_entities(published, content[("output", *parts)]).one_or_none()
            declaration, value = row or (None, None)
        if not declaration or value is not None or not any(part.isdecimal() for part in parts):
            return declaration, value
    offers = query.with_entities(content["available_outputs"]).scalar() or {}
    declaration = offers.get(path)
    if not declaration:
        return None, None
    output = query.with_entities(content["output"]).scalar()
    return declaration, _output_value(output, path)


def frozen_quantity(db, case_id, reference, projects=None):
    """Read the same case-scoped, immutable quantity for discovery and execution."""
    origin = None
    if isinstance(reference, ProjectReference):
        if projects is None:
            projects = {}
        if reference.revision_id not in projects:
            projects[reference.revision_id] = FrozenProjectOutputs(db, case_id, reference.revision_id)
        model = projects[reference.revision_id]
        item = model.get(reference.port)
        if item is None:
            raise ValueError(f"工程 #{reference.revision_id} 未提供 {reference.port}。请核对版本及所选物理量。")
        declaration, value = item["port"], item["value"]
        origin = {**{k: v for k, v in item.items() if k != "value" or declaration["shape"] != "state"},
                  "project_title": model.row["project"]["title"], "model_kind": model.row.get("model_kind", "saved")}
    else:
        declaration, value = _saved_study_output(db, case_id, reference)
        if not declaration:
            raise UnpublishedOutput(f"当前案例中的计算 #{reference.run_id} 未提供可用输出 {reference.port}。")
    port = PublishedPort.model_validate(declaration)
    # Legacy records may predate connects_as. Derive lossless shapes from their saved value.
    offer = PublishedPort.describe(port, value)
    if offer is None:
        raise ValueError(f"已存的{port.label}与其物理量声明不一致。")
    return offer, value, origin


def _source_references(study, raw, connections):
    refs = {k: SOURCE.validate_python(v) for k, v in (connections or {}).items()}
    for key, ref in refs.items():
        if isinstance(ref, FileColumnReference):
            continue
        basis = study.registration_inputs.get(key)
        if ref.registration_from is not None and ref.registration_from != basis:
            raise ValueError(f"{key} 的共用配准说明须来自该方法声明的文本输入。")
        if basis and (ref.registration_from or not ref.registration.strip()):
            text = raw.get(basis)
            if not isinstance(text, str) or not text.strip() or len(text) > 500:
                label = study.inputs.model_fields[basis].title or basis
                raise ValueError(f"请在「{label}」填写本次共同配准的依据（1 到 500 个字符），或为来源单独说明。")
            refs[key] = ref.model_copy(update={"registration":text, "registration_from":basis})
    return refs


def _resolve(db, case_id, study, raw, connections):
    inputs = dict(raw)
    tables = {}
    projects, origins, condition_sources = {}, {}, {}
    for field, reference in connections.items():
        target = study.input_ports.get(field)
        table_text = isinstance(reference, FileColumnReference) and field in study.text_column_inputs
        if (target is None and not table_text) or field in inputs:
            raise ValueError(f"{field} 需要一个已声明的输入接口，且只能选择填写值或引用结果中的一种。")
        if isinstance(reference, FileColumnReference):
            if target and (target.conditions or reference.condition_values):
                if reference.condition_values.keys() - target.conditions.keys() or not reference.condition_note.strip():
                    raise ValueError(f"{target.label}需要按输入声明提供测量工况，并注明其原件位置或依据。")
                condition_sources[field] = reference.condition_values
            tables[field] = reference
            continue
        source, value, origin = frozen_quantity(db, case_id, reference, projects)
        if origin is not None:
            origins[field] = origin
        inputs[field] = source.value_for(target, value, reference.registration)
        condition_sources[field] = source.condition_values
    values, evidence = resolve_table_inputs(db, case_id, study, tables)
    inputs.update(values)
    layout_origin = origins.get(study.live_layout, {})
    if study.layout_prefix_inputs and layout_origin.get("scope") == "layout_prefix":
        inputs = select_prefix_endpoint(study.layout_prefix_inputs, inputs, layout_origin["plane_uid"])
    for group in study.paired_inputs:
        from_file = [tables[k] for k in group if k in tables]
        if from_file and all(inputs.get(key) is not None for key in group):
            # Authored labels name the resolved rows in order, as in historical recipes.
            # Only a bound label introduces a source whose row alignment needs registration.
            sourced = [k for k in group if k in tables or k not in study.text_column_inputs]
            jointly_read = len(from_file) == len(sourced) and len({table_group(ref) for ref in from_file}) == 1
            if not jointly_read and not all(ref.registration.strip() for ref in from_file):
                raise ValueError(f"{'、'.join(group)} 使用了不同来源，请说明各行按什么对应，并按相同顺序提供记录。")
            if len({len(inputs[k]) for k in group if isinstance(inputs.get(k), list)}) > 1:
                raise ValueError(f"{'、'.join(group)} 的测点数不一致，请核对配对关系。")
    validated = study.inputs.model_validate(inputs)
    if condition_sources:
        resolved = validated.model_dump(mode="json")
        for key, values in condition_sources.items():
            study.input_ports[key].check_conditions(values, resolved)
    transfers = list(dict.fromkeys(note for origin in origins.values() for note in origin.get("scope_warnings", ())))
    for match in study.condition_matches:
        anchor, other = condition_sources.get(match.anchor, {}), condition_sources.get(match.other, {})
        differences = [f"{caption}（{anchor[key]} 与 {other[key]}）" for key, caption in match.keys.items()
                       if key in anchor and key in other and anchor[key] != other[key]]
        if differences:
            note = connections[match.other].registration.strip()
            issue = "两个输入来源的" + "、".join(differences) + "不同"
            if not note:
                raise ValueError(issue + "。请选取对应来源，或注明将此值用于另一工况的依据。")
            transfers.append(issue + "。本次跨工况引用依据：" + note)
    return validated, evidence, origins, condition_sources, transfers


def _original_inputs(value, input_path=()):
    if isinstance(value, OriginalFile):
        yield input_path, value
    elif isinstance(value, BaseModel) and type(value).__pydantic_root_model__:
        yield from _original_inputs(value.root, input_path)
    elif isinstance(value, BaseModel):
        for key in type(value).model_fields:
            yield from _original_inputs(getattr(value, key), (*input_path, key))
    elif isinstance(value, (list, tuple, dict)):
        members = ((str(k), v) for k, v in value.items()) if isinstance(value, dict) else enumerate(value)
        for key, item in members:
            if isinstance(item, (BaseModel, list, tuple, dict)):
                yield from _original_inputs(item, (*input_path, key))


def _bind_originals(value, db, case_id, stack):
    snapshots, receipts = {}, {}
    for input_path, original in _original_inputs(value):
        identifier = original.attachment_id
        if identifier not in snapshots:
            path, evidence = stack.enter_context(case_original_snapshot(db, case_id, identifier))
            snapshots[identifier] = path
            receipts[identifier] = {**evidence, "input_paths":[]}
        source = receipts[identifier]
        if original.sha256 and original.sha256 != source["sha256"]:
            raise ValueError("所选原件的 SHA-256 与计算指定的版本不符，请核对文件。")
        original.sha256 = source["sha256"]
        original._relative_path = source["path"]
        # Each input gets its own cursor, including two roles bound to the same exposure.
        original._stream = stack.enter_context(snapshots[identifier].open("rb"))
        source["input_paths"].append(list(input_path))
    return receipts


def compute_study(study, provider, validated, *, preview=False):
    try:
        compute = study.preview_compute if preview and study.preview_compute else study.compute
        output = compute(validated)
        if study.saved_outputs and not preview and isinstance(output,dict):
            extra = study.saved_outputs(validated, output)
            if set(extra) & set(output) or set(extra) - set(study.output_ports):
                raise ValueError("归档派生量须使用独立且已声明的输出接口。")
            output = {**output,**extra}
    except MissingStudyInput as exc:
        return {"status":"needs_input","message":str(exc)}
    except ValueError:
        raise
    except Exception as exc:
        logger.exception("Study %s (%s, v%s) failed", study.name, provider, study.version)
        raise StudyFailure(f"「{study.title}」在计算时中断（{provider_display_name(provider)}，v{study.version}，{type(exc).__name__}）。请核对输入与方法的适用范围；若重复出现，可用此版本信息报告问题。") from exc
    if not isinstance(output, dict) or "study_record" in output:
        raise ValueError("计算结果须为 JSON 对象，study_record 为保留字段。")
    try:
        return json.loads(json.dumps(output, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"「{study.title}」返回了无法保存的数值或对象。请检查求解器输出。") from exc


def preview_connected_study(db, case_id, study, provider, inputs, connections):
    """Resolve frozen inputs against the currently edited layout without creating any archive rows."""
    refs = _source_references(study, inputs, connections)
    model, _, _, _, transfers = _resolve(db, case_id, study, inputs, refs)
    with ExitStack() as files:
        if study.uses_original_files:
            _bind_originals(model, db, case_id, files)
        result = compute_study(study, provider, model, preview=True)
    if transfers:
        result["warnings"] = [*result.get("warnings", []), *transfers]
    return result


def recorded_study_inputs(record, *, resolved=None, varied=()):
    """Rebuild manual inputs without pinning defaults that were derived from a bound state."""
    inputs = record["inputs"] if resolved is None else resolved
    fields = set(record.get("authored_fields", inputs)) | set(varied)
    raw = {k: v for k, v in inputs.items() if k in fields and k not in record.get("connections", {})}
    # A third-party model can accept an input alias or unpack an alternate prescription.
    raw.update(record.get("authored_aliases", {}))
    return raw


def _prior_inputs(db, case_id, study, provider, run_id):
    content = GeneratedContent.content
    definition = content["definition"]
    row = db.query(definition["name"].as_string(), definition["provider"].as_string(), definition["version"].as_string(),
                   content["inputs"], content["connections"], content["authored_fields"], content["authored_aliases"], content["solver_receipt"]
                   ).filter(GeneratedContent.case_id == case_id, GeneratedContent.id == run_id,
                            GeneratedContent.content_type == "scientific_study").one_or_none()
    if row is None:
        raise ValueError("在当前工程中找不到要复用输入的计算记录。")
    name, owner, version, inputs, connections, fields, aliases, captured = row
    if name != study.name or ((owner, version) != (provider, study.version) and (owner, version) not in study.accepts_inputs_from):
        raise ValueError("当前方法未声明兼容这份记录的输入定义、单位和参考面。请核对历史输入后建立新的计算。")
    if not isinstance(inputs, dict):
        raise ValueError("历史记录缺少可复用的输入参数。可以继续查看已存输出，核对条件后另行计算。")
    if connections is not None and not isinstance(connections, dict):
        raise ValueError("原计算的来源绑定格式无效。可查看已存结果，重新选择输入后再算。")
    if (fields is not None and (not isinstance(fields, list) or not all(isinstance(key, str) for key in fields))
            or (aliases is not None and not isinstance(aliases, dict))):
        raise ValueError("历史记录中的手动输入信息不完整，无法还原原设置。请核对参数后另行计算。")
    record = {"inputs":inputs, "connections":connections or {}, "authored_aliases":aliases or {}}
    if fields is not None:
        record["authored_fields"] = fields
    origin = {"run_id":run_id, "name":name, "provider":owner, "version":version}
    if isinstance(captured, dict) and captured.get("sha256"):
        origin["solver_sha256"] = captured["sha256"]
    return record, origin


def _same_saved_values(old, new):
    if isinstance(old, dict):
        return isinstance(new, dict) and all(k in new and _same_saved_values(v, new[k]) for k, v in old.items())
    if isinstance(old, list):
        return isinstance(new, list) and len(old) == len(new) and all(_same_saved_values(a, b) for a, b in zip(old, new))
    return isinstance(old, bool) == isinstance(new, bool) and old == new


def run_study(bind, case_id, study, provider, args, task_id=None, *, connections=None, reuse_inputs_from=None):
    def response(row, execution):
        definition = {k: v for k, v in row.content["definition"].items() if k != "inputs"}
        receipt = {**row.content["output"], "study_record": {"id": row.id, **definition,
                "connections": row.content.get("connections", {}), "available_outputs": row.content.get("available_outputs", {}),
                "solver_receipt":row.content.get("solver_receipt"), "execution":execution}}
        if definition.get("native_grid") and receipt.get("native_grid_sha256"):
            receipt["study_record"]["native_grid_access"] = native_grid_access(case_id, row.id)
        if "input_origin" in row.content:
            receipt["study_record"]["input_origin"] = row.content["input_origin"]
        if row.content.get("originals"):
            receipt["study_record"]["originals"] = row.content["originals"]
        if row.content.get("project_evidence"):
            receipt["study_record"]["project_evidence"] = {key:{"location":ev["location"],**({"value":ev["value"]} if "value" in ev else {}),
                "incomplete":ev.get("incomplete",[])} for key,ev in row.content["project_evidence"].items()}
        if row.content.get("file_evidence"):
            receipt["study_record"]["file_evidence"] = [
                {"source": e["source"], "fields": e["fields"], "rows_used": len(e["row_numbers"]), "omitted": len(e["omitted_rows"])}
                for e in row.content["file_evidence"]]
        return receipt
    with Session(bind) as db, ExitStack() as files:
        raw = args if isinstance(args, dict) else args.model_dump(exclude_unset=True)
        prior = origin = None
        if reuse_inputs_from is not None:
            if raw or connections:
                raise ValueError("复用历史输入时不能同时替换参数或引用。")
            prior, origin = _prior_inputs(db, case_id, study, provider, reuse_inputs_from)
            raw, connections = recorded_study_inputs(prior), prior["connections"]
        refs = _source_references(study, raw, connections)
        recipe = {k: v.model_dump(exclude_defaults=True) for k, v in refs.items()}
        for key, ref in refs.items():
            if isinstance(ref, FileColumnReference):
                recipe[key]["table"] = ref.table.model_dump()
        validated, evidence, origins, conditions, transfers = _resolve(db, case_id, study, raw, refs)
        originals = {}
        if study.uses_original_files:
            originals = _bind_originals(validated, db, case_id, files)
        inputs = validated.model_dump(mode="json")
        captured = study_solver_receipt(study, provider, inputs=validated)
        identity = [study.name, provider, study.version, solver_cache_key(captured), inputs]
        authored = {}
        if prior is not None:
            different = [k for k,v in prior["inputs"].items() if k not in inputs or not _same_saved_values(v, inputs[k])]
            if different:
                raise ValueError("新方法解析出的输入与原记录不一致：" + "、".join(different) + "。请核对这些参数后另开一次计算。")
            if (captured.get("complete") and origin.get("solver_sha256") == captured["sha256"]
                    and _same_saved_values(inputs, prior["inputs"])):
                return response(db.get(GeneratedContent, reuse_inputs_from), "reused")
            authored["input_origin"] = origin
            identity.append(origin)
        if recipe:
            authored["authored_fields"] = sorted(raw)
            aliases = {k: v for k, v in raw.items() if k not in inputs}
            if aliases:
                authored["authored_aliases"] = aliases
            identity.extend((recipe, authored))
        key = "study:" + hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        query = db.query(GeneratedContent).filter_by(case_id=case_id, operation_key=key)
        existing = query.one_or_none()
        if existing:
            return response(existing, "reused")
        output = compute_study(study, provider, validated)
        if transfers:
            output = {**output, "warnings": [*output.get("warnings", []), *transfers]}
        if task_id is not None and db.query(AgentTask.status).filter_by(case_id=case_id, id=task_id).with_for_update().scalar() != "running":
            raise ValueError("任务已停止，本次计算结果没有写入。")
        try:
            assessments = _assess_outputs(study, output, inputs)
            if assessments:
                output["output_assessments"] = assessments
                if any(item["state"] != "available" for item in assessments.values()):
                    output["status"] = "partial"
            available = _available(study, output, conditions, assessments)
        except ValueError as exc:
            raise StudyFailure(f"「{study.title}」的输出与方法声明不一致（{provider_display_name(provider)}，v{study.version}）。{exc}") from exc
        record = GeneratedContent(case_id=case_id, content_type="scientific_study", operation_key=key,
                                  content={"summary": study.title, "definition": study.describe(provider), "solver_receipt":captured,
                                           "inputs": inputs, "output": output, **authored,
                                           "connections": recipe, "available_outputs": available,
                                           **({"originals": list(originals.values())} if originals else {}),
                                           **({"project_evidence": origins} if origins else {}),
                                           **({"file_evidence": evidence} if evidence else {})})
        db.add(record)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = query.one_or_none()
            if existing is None:
                raise
            return response(existing, "reused")
        return response(record, "computed")


@contextmanager
def _verified_native_grid(db, case_id, run_id, registry):
    """Reproduce a native grid using the archived resolved parameters and verified original bytes."""
    content = GeneratedContent.content
    record = db.query(content["definition"], content["inputs"], content["output"]["native_grid_sha256"].as_string(),
                      content["originals"], content["connections"], content["solver_receipt"]).filter_by(
                          case_id=case_id, id=run_id, content_type="scientific_study").one_or_none()
    if not record or not isinstance(record[0], dict) or not record[0].get("native_grid") or not record[2]:
        raise UnpublishedOutput("当前工程中找不到这份已登记全分辨率网格的计算。")
    definition, frozen, digest, originals, connections, saved = record
    installed = registry.get(definition["name"])
    archived_method = (definition["provider"], definition["version"])
    if not installed or not installed[0].native_grid or (
            (installed[1], installed[0].version) != archived_method and archived_method not in installed[0].accepts_inputs_from):
        raise ValueError(f"导出需要安装 {definition['name']}（{definition['provider']}，版本 {definition['version']}）或声明兼容其输入的版本。")
    study = installed[0]
    with ExitStack() as files:
        model = study.inputs.model_validate(frozen, by_name=True, by_alias=True)
        if study.uses_original_files:
            _bind_originals(model, db, case_id, files)
        used_inputs = model.model_dump(mode="json")
        if not _same_saved_values(frozen, used_inputs):
            raise ValueError("当前方法对存档输入的解析已改变。请核对参数后另行计算。")
        checked = study_solver_receipt(study, installed[1], inputs=model)
        try:
            result = study.native_grid(model)
            actual = grid_digest(result.x_edges_mm, result.y_edges_mm, result.cell_integrals, result.signal_unit)
            if actual != digest:
                raise ValueError("重算网格的校验值与已存计算不同。请核对求解器版本、运行环境与原件，再另存一次计算。")
        except ValueError:
            raise
        except Exception as exc:
            logger.exception("Native grid export failed for %s, run %s", study.name, run_id)
            raise StudyFailure(f"全分辨率导出中断（{study.title}，版本 {study.version}）。已有结果可从计算记录查看。") from exc
        yield result, digest, {
            "case_id": case_id, "run_id": run_id, "study": study.name, "provider": definition["provider"],
            "version": definition["version"], "inputs": frozen, "originals": originals or [],
            **({"verification_inputs":used_inputs} if used_inputs != frozen else {}),
            "saved_solver_receipt":saved, "verification_solver_receipt":checked, "verified_against_saved_grid":True,
            "connections": connections or {}, "cell_order": "row=y, column=x",
        }


def export_native_grid(db, case_id, run_id, registry, target):
    with _verified_native_grid(db, case_id, run_id, registry) as (grid, digest, provenance):
        write_grid_file(target, grid, digest, provenance)


def view_native_grid(db, case_id, run_id, registry, options):
    with _verified_native_grid(db, case_id, run_id, registry) as (grid, digest, _):
        return {"grid_sha256":digest, **grid_view(grid,options)}


def inspect_native_grid(bind, case_id, args, registry):
    with Session(bind) as db, _verified_native_grid(db, case_id, args.run_id, registry) as (grid, digest, source):
        return {"status":"completed","grid_sha256":digest,"verified_against_saved_grid":True,
                "saved_solver_receipt":source["saved_solver_receipt"], "verification_solver_receipt":source["verification_solver_receipt"],
                "originals":source["originals"], **native_grid_access(case_id,args.run_id), **grid_summary(grid,args.window)}


def study_history(db, case_id, before_id=None, *, name=None, input_key=None, revision_id=None, port=None):
    query = db.query(GeneratedContent).filter_by(case_id=case_id, content_type="scientific_study")
    if before_id is not None:
        query = query.filter(GeneratedContent.id < before_id)
    definition = GeneratedContent.content["definition"]
    if name is not None:
        query = query.filter(definition["name"].as_string()==name)
    if any(value is not None for value in (input_key,revision_id,port)):
        if not input_key or not revision_id or not port:
            raise ValueError("筛选某项工程输入时，请同时指定输入名、工程版本和输出端口。")
        source = GeneratedContent.content["connections"][input_key]
        query = query.filter(source["revision_id"].as_integer()==revision_id, source["port"].as_string()==port)
    rows = query.with_entities(GeneratedContent.id, definition["name"].as_string(), definition["title"].as_string(),
                               definition["version"].as_string(), GeneratedContent.content["available_outputs"]).order_by(GeneratedContent.id.desc()).limit(26).all()
    items = [dict(zip(("id", "name", "title", "version", "available_outputs"), row)) for row in rows[:25]]
    # Early interval declarations did not advertise whether their bounds coincided.
    # Only those rows need their frozen numeric output to display all compatible inputs.
    legacy = [item for item in items if any(p.get("shape") == "interval" and "connects_as" not in p
                                            for p in (item["available_outputs"] or {}).values())]
    if legacy:
        outputs = dict(query.with_entities(GeneratedContent.id, GeneratedContent.content["output"])
                       .filter(GeneratedContent.id.in_([item["id"] for item in legacy])).all())
        for item in legacy:
            item["available_outputs"] = {
                key: offer.model_dump(mode="json") for key, p in item["available_outputs"].items()
                if (offer := PublishedPort.describe(PublishedPort.model_validate(p), _output_value(outputs[item["id"]], key)))
                is not None}
    return {"items": items, "next_before_id": rows[24].id if len(rows) > 25 else None}

"""Map a versioned ordered light path into a coherent, single-pass field calculation."""

from typing import Annotated, Literal
import math

import numpy as np
from pydantic import Field

from ..physics.scalar_field import FieldGrid, GaussianAxis, display_raster, field_result, integrate_cells
from ..study_api import MissingStudyInput, Port, RasterView, StrictInput, Study
from .beam_source import missing_axis_inputs
from .intensity_map import INTENSITY_MAP, density_map
from .forward_path import forward_path
from .observation import OpticalLayout
from .optics import QuadraticSurface, UniformSection, model_wavelength_notes, paraxial_sections
from .project_outputs import LAYOUT
from .wave_state import COHERENT_FIELD, MAX_CHAIN_ELEMENTS, CoherentField, FieldPlane, FresnelElement, LaunchAxis


class WavePropagation(StrictInput):
    layout: OpticalLayout = Field(title="工程光路模型")
    input_field: CoherentField | None = Field(default=None, title="上游相干光场",
        description="可引用已保存的相干光场。其出射面与这段光路的入射面重合，使用相同的前向 T/S 坐标。"
                    "续算使用原始相位和已求值元件，可改变视场和采样。留空时使用所选光路自己的光源。")
    beam_uid: str = Field(
        default="primary",
        min_length=1,
        max_length=100,
        title="光路标识",
        description="primary 表示单程主光束，也可以填写工程中的入射支路标识。",
        json_schema_extra={"x-beamweft-input": "path-uid"},
    )
    plane_uid: Annotated[str, Field(min_length=1, max_length=100)] | None = Field(
        default=None,
        title="观察面标识",
        description="留空计算到所选光路末端。具名观察面使用该版本工程中登记的位置。",
        json_schema_extra={"x-beamweft-input": "plane-uid"},
    )
    window_mm: float = Field(
        default=4,
        gt=0,
        le=1e5,
        title="图像视场全宽 / mm",
        description="以展开光轴为中心的正方形区域，内部计算在双倍宽度上进行。",
    )
    samples: Literal[128, 256, 512] = Field(default=256, title="视场内每边采样数")


def coherent_axis(wavelength_nm, name, source, ray, prefix):
    missing = missing_axis_inputs(wavelength_nm, source)
    if missing:
        raise MissingStudyInput(f"「{name}」{prefix.upper()} 方向缺少" + "、".join(missing) + "。")
    if source.width_basis() != "gaussian":
        raise ValueError(f"「{name}」{prefix.upper()} 方向仅凭 M² 和光斑半径无法确定复振幅。此源模型需要已知波前的理想高斯光束。")
    center = getattr(ray, prefix + "_offset_mm") if ray else 0.0
    angle = getattr(ray, prefix + "_angle_mrad") if ray else 0.0
    if center is None or angle is None:
        raise MissingStudyInput(f"「{name}」{prefix.upper()} 方向的源面偏移或指向未确定，请填写测量值或明确采用同轴条件。")
    return GaussianAxis(
        source.radius_mm, source.curvature_mm if source.wavefront == "curved" else None, center, angle / 1000
    )


def coherent_axes(wavelength_nm, name, source):
    return (
        coherent_axis(wavelength_nm, name, source.tangential, source.launch_ray, "t"),
        coherent_axis(wavelength_nm, name, source.sagittal or source.tangential, source.launch_ray, "s"),
    )


def _path(args, element_exit=None):
    incoming = args.input_field
    route = forward_path(args.layout, args.beam_uid, args.plane_uid,
                         source_wavelength=incoming.wavelength_nm if incoming else None, element_exit=element_exit)
    if incoming:
        axes = incoming.tangential.axis(), incoming.sagittal.axis()
    else:
        axes = coherent_axes(route.wavelength_nm, route.name, route.source)
    return route.wavelength_nm, *axes, route.start_mm, route.elements


def _field_path(train, start, wl):
    path = []
    warnings = []
    z = start
    for element, index, length in train:
        if length and index is None:
            raise ValueError(f"请填写「{element.name}」在当前波长的折射率。")
        sections = paraxial_sections(element, wl, length=length, index=index)
        if sections is None:
            raise ValueError(f"「{element.name}」尚未提供支持的横向场传输模型。可选择位于它之前的观察面。")
        scope = None
        if element.prescription:
            transfer = element.prescription.evaluate("ray_matrix", wl, length)
            warnings.extend(f"「{element.name}」{note}" for note in transfer.get("warnings", []))
            scope = transfer["scope"]
        frozen = FresnelElement.capture(element, length, sections, scope)
        path.append((frozen, z, sections))
        z += length
    return path, warnings


def field_grid(width, cells, wl, t, s, path, offset):
    grid = FieldGrid(width, cells, wl / 1e6, t, s, offset)
    for element, z, sections in path:
        if element.clear_aperture:
            grid.aperture(element.clear_aperture, element.name, element.uid, z)
        for section in sections:
            if isinstance(section, UniformSection):
                grid.propagate(section.length_mm, section.index)
            elif isinstance(section, QuadraticSurface):
                grid.phase_surface(section.tangential, section.sagittal)
            else:
                raise ValueError(f"当前标量场方法无法传播「{element.name}」声明的物理传输。")
    return grid


def prepare_field(args, *, element_exit=None):
    wl, t, s, start, train = _path(args, element_exit)
    path, device_warnings = _field_path(train, start, wl)
    warnings = model_wavelength_notes([el for el, _, _ in train], wl) + device_warnings
    if args.input_field is not None:
        path = args.input_field.grid_path(start) + path
        warnings = list(dict.fromkeys([*args.input_field.model_notes, *warnings]))
    end = start + math.fsum(length for _, _, length in train)
    return wl, t, s, path, end, warnings


def _estimate_field(window, cells, wl, t, s, path):
    has_pupil = any(el.clear_aperture for el, _, _ in path)

    def estimate(resolution):
        return integrate_cells(lambda offset: field_grid(window,resolution,wl,t,s,path,offset),has_pupil)

    try:
        with np.errstate(over="raise", invalid="raise", divide="raise", under="ignore"):
            fine = estimate(cells)
            coarse = estimate(cells//2)
            return field_result(fine, coarse, window, cells), fine
    except ArithmeticError as exc:
        raise ValueError("参数超出标量场计算范围，请核对波长、孔径、光束与窗口的单位。") from exc


def _field_map(raster, state, warnings, quality):
    return density_map(raster,unit="原始入射功率比例",plane=state["plane"]["name"],
        wavelength_nm=state["wavelength_nm"],warnings=warnings,quality=quality,
        basis=f"单程标量相干场；对应已计算视场的 {len(raster['density'])} × {len(raster['density'])} 个积分单元，"
              "相对于原始入射总功率。图外功率未计入所示网格。")


def _archive_wave(_, result):
    if "grid" not in result:
        return {}
    return {"intensity_map":_field_map(result["grid"],result["exit_field"],result["warnings"],result["numerics"]["quality"])}


def propagate_field(args):
    wl, t, s, path, end, warnings = prepare_field(args)
    operations = [el for el, _, _ in path]
    if len(operations) > MAX_CHAIN_ELEMENTS:
        raise ValueError(f"这条光场传输链超过 {MAX_CHAIN_ELEMENTS} 个元件的计算上限。请合并物理等价的连续均匀段并保留孔面。")
    plane_name = next((p.name for p in args.layout.observation_planes if p.uid == args.plane_uid),
                      operations[-1].name + "出射面")
    state = CoherentField(schema_version=1, wavelength_nm=wl, tangential=LaunchAxis.from_axis(t),
        sagittal=LaunchAxis.from_axis(s), operations=operations, model_notes=warnings,
        plane=FieldPlane(beam_uid=args.beam_uid, plane_uid=args.plane_uid, name=plane_name, z_mm=end),
        distance_mm=math.fsum(op.length_mm for op in operations))
    has_pupil = any(el.clear_aperture for el, _, _ in path)
    result, _ = _estimate_field(args.window_mm,args.samples,wl,t,s,path)
    numerics = result["numerics"]
    numerics["quality"] = "limited" if result["warnings"] else ("conditional" if warnings else "usable")
    sampling = ("有孔径时，同一条光路在 T/S 各偏移 ±¼ 格的四个网格上独立传播，再对强度取平均。"
                if has_pupil else "整条光路在像素中心取样。")
    result.update(
        z_mm=end,
        wavelength_nm=wl,
        beam_uid=args.beam_uid,
        exit_field=state.model_dump(mode="json"),
        assumptions=[
            "标量、单色、相干、近轴单程传播。孔面为展开轴的垂直投影；透镜和反射面只施加理想二次相位。",
            "共享元件使用同一实物几何。固定屈光力按工程值建模，注册的波长模型按所选光束的波长计算。",
            "原始入射总功率取 1。续算保留前段的截光损失。图内显示相对原始入射功率的面密度，全场积分含图外填充区。",
            "沿对齐的 T/S 坐标传播横向复包络，省略公共纵向载波相位。平界面按无损处理，未计入镀膜、反射损耗、吸收或增益。",
            f"显示视场边长 {args.window_mm:g} mm，填充区边长 {numerics['padded_width_mm']:g} mm。"
            f"精、粗网格每边各 {numerics['fine_cells_per_padded_side']} 和 {numerics['coarse_cells_per_padded_side']} 点。"
            + sampling,
        ],
    )
    result["warnings"].extend(warnings)
    if result["warnings"]:
        result["status"] = "partial"
    return result


WAVE_STUDY = Study(
    "propagate_coherent_field",
    "相干光场传播",
    "2",
    "沿单程光路传播复振幅，计算有限孔径衍射。可从工程光源开始，或从已保存的相干场继续，在新视场和采样下重算整条链路。",
    WavePropagation,
    propagate_field,
    metrics={
        "z_mm": "观察位置 z / mm",
        "throughput": "全场通过功率 / 原始入射功率",
        "window_power": "图内功率 / 原始入射功率",
        "shape_change": "两种网格的相对强度差 L¹",
        "cell_mm": "精网格间距 / mm",
    },
    table=(
        "rows",
        {
            "element": "孔径所在元件",
            "z_mm": "孔面 z（本段坐标）/ mm",
            "before": "孔前 / 原始入射",
            "after": "孔后 / 原始入射",
            "blocked": "截去 / 原始入射",
        },
    ),
    raster=RasterView(
        x="grid.edges_mm",
        y="grid.edges_mm",
        z="grid.density",
        label="相对入射功率密度 / mm⁻²",
        x_label="切向 T / mm",
        y_label="弧矢 S / mm",
        equal_scale=True,
        power_db=True,
    ),
    input_ports={"layout": LAYOUT, "input_field": COHERENT_FIELD},
    output_ports={
        "throughput": Port(
            quantity="optics.coherent-clear-aperture-throughput",
            label="理想光路的孔径通过功率比例",
            unit="1",
            scale=1,
            inherit_conditions_from="layout",
        ),
        "exit_field": COHERENT_FIELD.model_copy(update={"available_on_partial": True}),
        "intensity_map":INTENSITY_MAP.model_copy(update={"available_on_partial":True}),
    },
    saved_outputs=_archive_wave,
    keywords=("aperture", "diffraction", "Fresnel", "光阑", "截光", "光斑", "光场", "衍射"),
    live_layout="layout",
    ignores_diagram_positions=True,
    layout_prefix_inputs={"path":"beam_uid","plane":"plane_uid"},
    accepts_inputs_from=(("BeamWeft", "1"),),
)


class FieldSampling(StrictInput):
    field: CoherentField = Field(title="已保存的相干光场", description="在来源记录自己的出射面取样。")
    window_mm: float = Field(default=4,gt=0,le=1e5,title="以光轴为中心的物方视场全宽 / mm")
    samples: Literal[128,256,512] = Field(default=256,title="输出图每边积分单元数")


def sample_coherent_intensity_map(args):
    field = args.field
    path = field.grid_path(field.plane.z_mm)
    result, fine = _estimate_field(args.window_mm,args.samples,field.wavelength_nm,field.tangential.axis(),
                                   field.sagittal.axis(),path)
    warnings = list(dict.fromkeys([*result["warnings"], *field.model_notes]))
    raster, _ = display_raster(fine.density,args.window_mm,args.samples,max_bins=args.samples)
    return {"status":"partial" if warnings else "completed","warnings":warnings,
            "intensity_map":_field_map(raster,field.model_dump(),warnings,
                "limited" if result["warnings"] else ("conditional" if field.model_notes else "usable")),"window_power":result["window_power"],
            "shape_change":result["shape_change"],"cell_mm":result["cell_mm"],"grid":result["grid"],
            "assumptions":["使用来源记录的完整入射场和传输顺序，在其出射面重算选定视场。两个网格给出数值收敛诊断。",
                           "归档光强图按指定单元积分，相对于原始入射总功率。显示图每边至多 128 个分箱。"]}


FIELD_MAP_STUDY = Study("sample_coherent_intensity_map","在已存光场取样光强图","1",
    "在已保存相干场自己的参考面和 T/S 轴上指定视场、取样分辨率，取得可以和相机或其他光学计算比较的积分光强网格。",
    FieldSampling,sample_coherent_intensity_map,input_ports={"field":COHERENT_FIELD},
    output_ports={"intensity_map":INTENSITY_MAP.model_copy(update={"available_on_partial":True})},
    metrics={"window_power":"视场内 / 原始入射功率","shape_change":"两种网格相对强度差 L¹","cell_mm":"积分格间距 / mm"},
    raster=WAVE_STUDY.raster,keywords=("相机","取样","预测","强度图","intensity map","detector","resample"))

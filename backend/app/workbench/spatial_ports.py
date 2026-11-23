"""A spatial detector on a versioned optical port network."""

from dataclasses import dataclass, field
import math
from typing import Literal

import numpy as np
from pydantic import Field
from scipy.fft import fft2

from ..physics.port_network import network_islands, phase
from ..physics.scalar_field import block_means, display_raster
from ..physics.spatial_network import SpatialNetwork
from ..study_api import RasterView, StrictInput, Study
from .component_origin import model_wavelength_notes
from .intensity_map import INTENSITY_MAP, density_map
from .network import Terminal, network_sources
from .observation import OpticalLayout, power_for_layout
from .project_outputs import LAYOUT, OUTGOING_POWER, at_wavelength
from .wave_study import WavePropagation, coherent_axes, field_grid, prepare_field


class SpatialDetector(StrictInput):
    layout: OpticalLayout = Field(title="工程光路模型")
    detector: Terminal = Field(
        title="探测端口",
        description="取该端口沿出射方向的场，在其参考平面采样。",
        json_schema_extra={"x-beamweft-input": "network-port"},
    )
    window_mm: float = Field(default=4, gt=0, le=1e5, title="探测视场全宽 / mm")
    samples: Literal[64, 128, 256, 512] = Field(default=128, title="视场内每边采样数",
        description="在保持探测视场的同时加密网格。可用分辨率受当前连通网络的内存和反馈求解开销约束。")


def _profile(layout, drive, wl, name):
    profile = drive.profile
    if profile.kind == "layout":
        args = WavePropagation(layout=layout, beam_uid=profile.beam_uid, plane_uid=profile.plane_uid)
        source_wl, t, s, path, _, notes = prepare_field(args, element_exit=profile.element_exit)
        if not math.isclose(source_wl, wl, rel_tol=1e-12):
            raise ValueError("横向光场来源与端口网络的波长不同，请核对光路和参考面。")
        return (t, s, path), notes
    axes = coherent_axes(wl, name, profile)
    return (*axes, []), []


@dataclass
class Estimate:
    density: np.ndarray
    external: dict = field(default_factory=dict)
    profile_power: dict = field(default_factory=dict)
    source_error: float = 0.0
    edge: float = 0.0
    alias: float = 0.0
    angles: float = 0.0
    feature_cells: float = math.inf


def _estimate(net, target, width, cells, groups, quadrature):
    result = Estimate(np.zeros((2 * cells, 2 * cells)))
    result.external = {key: 0.0 for i, key in enumerate(net.keys) if i not in net.connections}
    for offset in quadrature:
        for sources in groups.values():
            spectra = {}
            for drive, watts, (t, s, path) in sources:
                grid = field_grid(width, cells, net.wavelength_nm, t, s, path, offset)
                captured = grid.power()
                if captured < 1e-12:
                    raise ValueError("指定端口的光场在计算区内过弱，无法按交付功率归一化。请核对孔径、束心和视场。")
                key = drive.at.key()
                result.profile_power[key] = result.profile_power.get(key, 0.0) + captured / len(quadrature)
                result.source_error = max(result.source_error, abs(1 - grid.source_fraction))
                result.edge = max(result.edge, grid.edge_peak)
                result.alias = max(result.alias, grid.wrap_risk)
                result.angles = max(result.angles, grid.phase_risk)
                result.feature_cells = min(result.feature_cells, grid.min_feature_cells)
                scale = math.sqrt(watts / captured) * phase(drive.phase_deg or 0)
                spectra[key] = fft2(grid.field, norm="ortho") * scale
            watts = math.fsum(w for _, w, _ in sources)
            density, ports, edge, alias, angles = net.propagate(grid, spectra, target, watts)
            result.density += density / len(quadrature)
            for key, value in ports.items():
                result.external[key] = result.external.get(key, 0.0) + value / len(quadrature)
            result.edge = max(result.edge, edge)
            result.alias = max(result.alias, alias)
            result.angles = max(result.angles, angles)
    return result


def spatial_detector(args):
    layout, target = args.layout, args.detector.key()
    network = layout.network
    if network is None:
        return {"status": "needs_input", "message": "请在工程的端口光路中添加元件与入射光源。"}
    island = next((g for g in network_islands(network) if target[0] in g), set())
    components = [c for c in network.components if c.uid in island]
    if not any(c.uid == target[0] and target[1] in c.ports() for c in components):
        raise ValueError("这个探测端口在当前工程中不存在。请选择一个已登记端口。")
    links = [link for link in network.links if link.a.component in island]
    wl = network.wavelength_nm or layout.system.wavelength_nm
    budgets = (
        power_for_layout(layout) if any(d.path_output and d.at.component in island for d in network.drives) else []
    )
    drives, origins = network_sources(network, wl, budgets)
    groups, warnings = {}, model_wavelength_notes(components, wl)
    has_drive = False
    for d, w, issue in drives:
        if d.at.component not in island:
            continue
        has_drive = True
        if issue:
            return {"status": "needs_input", "message": issue}
        if not w:
            continue
        name = next(c.name for c in components if c.uid == d.at.component) + " / " + d.at.port
        if d.profile is None:
            return {"status": "needs_input", "message": f"请为「{name}」指定入射横向光场或工程参考面。"}
        model, notes = _profile(layout, d, wl, name)
        groups.setdefault(d.coherence_group or d.at.key(), []).append((d, w, model))
        warnings.extend(notes)
    if not has_drive:
        return {"status": "needs_input", "message": "请为所选端口的连通网络指定入射源与功率。"}
    launched = math.fsum(w for group in groups.values() for _, w, _ in group)
    active = [d.at.key() for group in groups.values() for d, _, _ in group]
    net = SpatialNetwork(components, links, wl, active)
    illuminated = {net.keys[i][0] for i in net.active}
    modal = [c.name for c in components if c.uid in illuminated and not c.acts_pointwise()]
    if modal:
        return {
            "status": "needs_input",
            "message": "、".join(modal)
            + " 的散射模型只描述匹配模式。请核对器件是否在照明区域内逐点均匀作用，或使用单模端口功率计算。",
        }
    net.check_size(args.samples, len(groups))
    for device in net.devices:
        warnings.extend(device.get("warnings", []))
    has_pupil = any(el.clear_aperture for group in groups.values() for _, _, (_, _, path) in group for el, _, _ in path)
    offsets = ((-0.25, -0.25), (0.25, -0.25), (-0.25, 0.25), (0.25, 0.25)) if has_pupil else ((0.0, 0.0),)
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise", under="ignore"):
            fine = _estimate(net, target, args.window_mm, args.samples, groups, offsets)
            coarse = _estimate(net, target, args.window_mm, args.samples // 2, groups, offsets)
    except (ArithmeticError, np.linalg.LinAlgError) as exc:
        raise ValueError("空间光场计算超出数值范围，请核对输入单位、光场尺度与反馈条件。") from exc
    dx = args.window_mm / args.samples
    detected = float(fine.density.sum() * dx**2)
    escaped = math.fsum(fine.external.values())
    l1_w = float(np.abs(block_means(fine.density, 2) - coarse.density).sum() * (2 * dx) ** 2)
    shape_change = l1_w / max(detected, launched * 1e-12) if launched else 0.0
    port_change = (
        max((abs(p - coarse.external[k]) for k, p in fine.external.items()), default=0.0) / launched
        if launched
        else 0.0
    )
    if escaped > launched * (1 + 1e-8):
        raise ValueError("空间端口的外部总出射功率超过入射功率，请核对矩阵或反馈条件。")
    model_notes = len(warnings)
    if fine.source_error > 0.001 or coarse.source_error > 0.005 or fine.feature_cells < 12:
        warnings.append("源场或孔径的网格采样不足，请核对束心、视场并增加采样。")
    if fine.edge > 0.003 or fine.alias > 0.01 or fine.angles > 0.005:
        warnings.append("场接近填充边界、横移或传播频谱超过采样范围，或近轴近似受限。请核对配准并改变视场和采样复算。")
    profile_change = max(
        (abs(p - coarse.profile_power[k]) / max(p, 1e-12) for k, p in fine.profile_power.items()), default=0.0
    )
    if (shape_change > 0.05 and l1_w > 1e-8 * launched) or port_change > 0.01 or profile_change > 0.02:
        warnings.append("双网格的强度图、端口功率或输入光场归一化尚未收敛。请增加采样并复核视场。")
    raster, window_w = display_raster(fine.density, args.window_mm, args.samples)
    return {
        "status": "partial" if warnings else "completed",
        "warnings": list(dict.fromkeys(warnings)),
        "detector": args.detector.model_dump(),
        "wavelength_nm": wl,
        "sampling": {"cells_per_axis": args.samples, "comparison_cells_per_axis": args.samples // 2,
                     "quality":"limited" if len(warnings)>model_notes else ("conditional" if model_notes else "usable"),
                     "window_mm": args.window_mm, "display_cells_per_axis": len(raster["edges_mm"]) - 1},
        "input_w": launched,
        "detected_w": detected,
        "window_w": window_w,
        "external_w": escaped,
        "removed_w": max(0.0, launched - escaped),
        "shape_change": shape_change,
        "grid_change_w": l1_w,
        "port_change": port_change,
        "edge_fraction": fine.edge,
        "alias_fraction": fine.alias,
        "phase_risk": fine.angles,
        "grid": raster,
        "ports": [
            {"component": k[0], "name": net.names[k[0]], "port": k[1], "outgoing_w": v}
            for k, v in fine.external.items()
        ],
        "origins": [o for o in origins if o["terminal"]["component"] in island],
        "assumptions": [
            "单色标量近轴模型。各端口采用已配准的平行展开 T/S 轴，器件的复散射系数在照明区域内逐点均匀作用，偏振由端口定义。",
            "同组复振幅相加，独立光源的强度相加。驱动功率是端口实际接收的功率，参考相位加在所选复包络上。引用上游光场时须自行登记到端口的载波相位。",
            "连接在均匀介质中传播并计入载波光程。横移按 a 至 b 定义，反向传播取反。图像单元显示平均功率密度。",
        ],
    }


def _archive_detector(_, result):
    if "grid" not in result:
        return {}
    target = result["detector"]
    return {"intensity_map":density_map(result["grid"],unit="W",plane=f"{target['component']} / {target['port']}",
        wavelength_nm=result["wavelength_nm"],warnings=result["warnings"],quality=result["sampling"]["quality"],
        basis="在选定端口出射平面的展开 T/S 坐标按面积积分。各单元的瓦数相加得到已保存视场内的功率。")}


SPATIAL_PORT_STUDY = Study(
    "spatial_port_field",
    "端口横向光场",
    "1",
    "用已配准横向复振幅计算线性被动网络的分束、传播、干涉和稳态反馈。在选定端口读取二维功率密度及外部功率收支。",
    SpatialDetector,
    spatial_detector,
    metrics={
        "detected_w": "该端口全场出射 / W",
        "window_w": "探测视场内 / W",
        "input_w": "连通网络入射 / W",
        "external_w": "外部总出射 / W",
        "removed_w": "网络损耗 / W",
        "shape_change": "双网格强度相对差 L¹",
    },
    table=("ports", {"name": "元件", "port": "外部端口", "outgoing_w": "出射功率 / W"}),
    raster=RasterView(
        x="grid.edges_mm",
        y="grid.edges_mm",
        z="grid.density",
        label="功率密度 / W·mm⁻²",
        x_label="切向 T / mm",
        y_label="弧矢 S / mm",
        equal_scale=True,
        power_db=True,
    ),
    input_ports={"layout": LAYOUT},
    output_ports={"detected_w": at_wavelength(OUTGOING_POWER).model_copy(update={"inherit_conditions_from": "layout"}),
                  "intensity_map":INTENSITY_MAP.model_copy(update={"available_on_partial":True})},
    saved_outputs=_archive_detector,
    live_layout="layout",
    ignores_diagram_positions=True,
    keywords=("干涉", "条纹", "横向剪切", "合束", "fringe", "spatial interference", "detector"),
)

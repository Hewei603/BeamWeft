"""Registered, forward paraxial propagation between explicit material-side reference planes."""
from pydantic import Field, model_validator

from ..study_api import StrictInput, Study
from .beam_predictions import fitted_curvature, predict_widths
from .beam_source import (MEDIUM_BEAM, PARAXIAL_BEAM, LaunchRay, MediumBeamState, ParaxialBeamState, SourceAxis,
                          compose_ray_matrices, medium_beam_output, ray_matrix_product, transport_beam)
from .optics import QuadraticSurface, UniformSection


def register_fitted_plane(fit, sampled_range, args):
    """Attach the measured axis, its width convention and the physical reference plane to a fit."""
    registration = args.register_plane
    if registration is None or fit["m2"] is None:
        return
    limit = fit["output_limits"].get("geometry") or fit["output_limits"].get("m2")
    if limit or not 1 - 1e-12 <= fit["m2"] <= 10000 or args.wavelength_nm > 1e6:
        fit["output_limits"]["registered_beam"] = limit or "传播因子或波长超出逐轴近轴传播契约。"
        return
    station = predict_widths(fit, [registration.z_mm], sampled_range)[0]
    curvature = fitted_curvature(fit, registration.z_mm)
    axes = {"tangential": SourceAxis(), "sagittal": SourceAxis()}
    axes[registration.axis] = SourceAxis(radius_mm=station["radius_mm"], curvature_mm=curvature,
        wavefront="waist" if curvature is None else "curved", m2=max(1.,fit["m2"]),
        second_moment_radius=registration.width_basis == "second_moment",
        equivalent_radius=registration.width_basis == "equivalent")
    notes = [f"实测焦散配准于 z={registration.z_mm:g} mm。{registration.note}", *fit["prediction_warnings"]]
    if station["extrapolated"]:
        notes.append("参考面在原测量区间外，依据同一均匀介质外推。")
    unknown_ray = LaunchRay(t_offset_mm=None, t_angle_mrad=None, s_offset_mm=None, s_angle_mrad=None)
    fit["registered_beam"] = MediumBeamState(frame=MEDIUM_BEAM.unit, medium_n=args.medium_n, wavelength_nm=args.wavelength_nm,
        **axes, launch_ray=unknown_ray, width_basis={registration.axis:registration.width_basis}, model_warnings=tuple(notes)).model_dump(mode="json")


class MaterialSection(StrictInput):
    length_mm: float = Field(ge=0, title="本段物理长度 / mm", description="沿当前介质向前传播，零长度可直接计算界面。")
    next_medium_n: float | None = Field(default=None, gt=0, title="末面透射侧折射率",
        description="留空即停在当前介质。填写时表示穿过末面，折射率应取自同一真空波长。")
    surface_radius_mm: float | None = Field(default=None, title="末面曲率半径 / mm",
        description="留空表示垂轴平面；球面曲率中心位于下游时填正值。")

    @model_validator(mode="after")
    def surface(self):
        if self.surface_radius_mm is not None and (self.next_medium_n is None or self.surface_radius_mm == 0):
            raise ValueError("球面折射需要透射侧折射率和非零带符号曲率半径。")
        return self


class MaterialPropagation(StrictInput):
    input_beam: MediumBeamState | None = Field(default=None, title="介质参考面光束")
    input_air: ParaxialBeamState | None = Field(default=None, title="空气参考面光束（v2）")
    sections: list[MaterialSection] = Field(min_length=1, max_length=300, title="顺序介质段与末面")

    @model_validator(mode="after")
    def single_reference(self):
        if (self.input_beam is None) == (self.input_air is None):
            raise ValueError("请选择一个入射参考面：介质光束或已有的空气光束。")
        return self


def propagate_sections(args):
    source = args.input_beam or args.input_air
    index = getattr(source, "medium_n", 1.)
    z = 0.
    transfer = ray_matrix_product(())
    points = []
    for segment in args.sections:
        transfer = compose_ray_matrices(UniformSection(segment.length_mm, index).matrix("tangential"), transfer)
        z += segment.length_mm
        if segment.next_medium_n is not None:
            power = (index - segment.next_medium_n) / segment.surface_radius_mm if segment.surface_radius_mm is not None else 0.
            transfer = compose_ray_matrices(QuadraticSurface(power, power).matrix("tangential"), transfer)
            index = segment.next_medium_n
        beam = transport_beam(source, source.wavelength_nm, {"tangential":transfer, "sagittal":transfer}, output_n=index)
        points.append({"z_mm":z, "medium_n":index, "radius_t_mm":beam.tangential.radius_mm, "radius_s_mm":beam.sagittal.radius_mm})
    partial = (any(axis.radius_mm is None or axis.m2 is None or axis.wavefront == "unknown" for axis in (beam.tangential, beam.sagittal))
               or (beam.launch_ray is not None and any(value is None for value in beam.launch_ray.model_dump().values())))
    output = {"status":"partial" if partial else "completed", "wavelength_nm":source.wavelength_nm,
              "points":points, "exit_beam":beam.model_dump(mode="json"), "medium_n":index}
    basis_compatible = all(beam.axis(axis).width_basis() == basis for axis, basis in beam.width_basis.items())
    if index == 1 and all(origin.medium_n == 1 for origin in beam.origins.values()) and basis_compatible:
        # A v2 receipt can only represent constraints originating at an air reference plane.
        air = beam.model_dump(exclude={"frame", "medium_n", "origins", "width_basis"})
        origins = {axis:origin.model_dump(exclude={"medium_n"}) for axis, origin in beam.origins.items()}
        output["exit_air"] = ParaxialBeamState(frame=PARAXIAL_BEAM.unit, **air, origins=origins).model_dump(mode="json")
    elif index == 1:
        output["warnings"] = ["末面在空气中。继续使用介质光束状态可保留旧空气接口无法表达的原始介质约束或实测宽度口径。"]
    return output


MEDIUM_STUDY = Study("propagate_paraxial_sections", "介质段传播与界面折射", "1",
    "从标定截面沿均匀、各向同性介质传播。每段可在末端穿过垂轴平面或共轴球面。显示透射侧的物理束宽、曲率与指向角。"
    "采用标量折射率和一阶近轴模型，适用于独立 T/S 轴；功率损耗、双折射走离和孔径衍射须另建模型。",
    MaterialPropagation, propagate_sections, {"medium_n":"末面折射率"},
    input_ports={"input_beam":MEDIUM_BEAM, "input_air":PARAXIAL_BEAM}, output_ports={"exit_beam":medium_beam_output("exit_beam"),"exit_air":PARAXIAL_BEAM},
    table=("points", {"z_mm":"累计距离 / mm","medium_n":"所在介质 n","radius_t_mm":"切向半径 / mm","radius_s_mm":"弧矢半径 / mm"}))

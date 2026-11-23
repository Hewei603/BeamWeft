"""Physical description of a laser source at its reference plane."""

import math
from typing import Literal

import numpy as np
from pydantic import Field, field_validator, model_validator

from ..physics import beam as B
from ..study_api import Condition, Port, StrictInput


def compose_ray_matrices(after, before):
    """Compose reduced ABCD matrices; recognize zeros obscured by local roundoff in a cancelling dot product."""
    def dot(a, b, c, d):
        first, second = float(a)*float(b), float(c)*float(d)
        value = first + second
        # Three binary64 roundings in the two products and their sum. An isolated
        # small physical coefficient cannot satisfy the opposite-sign condition.
        if (first < 0 < second or second < 0 < first) and math.isfinite(value):
            error = _DOT_ROUNDOFF * abs(first) + _DOT_ROUNDOFF * abs(second)
            if abs(value) <= error:
                return 0.
        return value

    (a, b), (c, d) = after
    (e, f), (g, h) = before
    return np.array([[dot(a, e, b, g), dot(a, f, b, h)], [dot(c, e, d, g), dot(c, f, d, h)]])


_DOT_ROUNDOFF = 3 * 2.**-53 / (1 - 3 * 2.**-53)


def ray_matrix_product(matrices):
    product = np.eye(2)
    for matrix in matrices:
        product = compose_ray_matrices(matrix, product)
    return product


class FitOrigin(StrictInput):
    study_id: int = Field(gt=0)
    study_key: str | None = Field(default=None, pattern=r"^fit-v1:[a-f0-9]{64}$", exclude_if=lambda value:value is None,
        description="Identity of the source mapping, original readings and fitted result. Older receipts may only store the case study ID.")
    measured_axis: Literal["X", "Y", "T", "S"]
    coordinate_mm: float
    registration: str = Field(min_length=1, max_length=800)
    width_basis: Literal["second_moment", "gaussian_equivalent"]
    wavelength_nm: float = Field(gt=0)
    medium_n: float | None = Field(default=None, gt=0, exclude_if=lambda value: value is None,
        description="实测介质在该真空波长处的折射率。早期工程的拟合源限定于空气。")
    radius_mm: float = Field(gt=0)
    curvature_mm: float | None
    m2: float = Field(gt=0)
    warnings: list[str] = Field(default_factory=list)


class SourceAxis(StrictInput):
    radius_mm: float | None = Field(default=None, gt=0)
    wavefront: Literal["unknown", "waist", "curved"] = "unknown"
    curvature_mm: float | None = None
    m2: float | None = Field(default=None, ge=1, le=10000)
    second_moment_radius: bool = Field(
        default=False, description="Source radius was explicitly specified as 2 sigma for an equivalent M² beam."
    )
    equivalent_radius: bool = Field(default=False, exclude_if=lambda value: not value,
        description="Radius from an effective source model; this width alone does not establish a Gaussian field.")
    fit_origin: FitOrigin | None = Field(default=None, exclude_if=lambda value: value is None,
        description="Archived width-fit parameters at their registered source plane. Current editable values may differ.")

    @model_validator(mode="after")
    def consistent_width_basis(self):
        if self.second_moment_radius and self.equivalent_radius:
            raise ValueError("同一轴的半径只能登记一种口径：二阶矩实测半径，或等效模型半径。")
        return self

    def width_basis(self):
        if self.second_moment_radius:
            return "second_moment"
        if self.equivalent_radius:
            return "equivalent"
        return "gaussian" if self.m2 == 1 and self.fit_origin is None else "equivalent"


class LaunchRay(StrictInput):
    """Chief ray at the source in air, relative to the unfolded nominal axis."""

    t_offset_mm: float | None = 0
    s_offset_mm: float | None = 0
    t_angle_mrad: float | None = 0
    s_angle_mrad: float | None = 0


class BeamSource(StrictInput):
    """Separable T/S source in air at one registered entrance plane."""
    tangential: SourceAxis
    sagittal: SourceAxis | None = Field(default=None, description="Omit to share the tangential source, or provide an independent S axis.")
    launch_ray: LaunchRay | None = Field(default=None, description="Omit for coaxial launch. Null coordinates inside a supplied ray are unknown.")

    def axis(self, name: Literal["tangential", "sagittal"]):
        return self.sagittal if name == "sagittal" and self.sagittal is not None else self.tangential


class AxisTransferOrigin(StrictInput):
    """An unresolved input at a previous air plane and its ray transfer to this plane."""
    axis: SourceAxis
    offset_mm: float | None
    angle_mrad: float | None
    matrix: tuple[tuple[float, float], tuple[float, float]]

    @field_validator("matrix")
    @classmethod
    def forward_matrix(cls, matrix):
        (a, b), (c, d) = matrix
        ad, bc = a*d, b*c
        determinant = ad - bc
        # Forward air-to-air maps use (x, nθ). Allow accumulated element tolerances
        # and cancellation error, but do not certify a numerically unresolved sign.
        rounding = 8 * math.ulp(max(abs(ad), abs(bc)))
        if (not math.isfinite(determinant) or determinant <= 0 or rounding >= .25
                or abs(determinant - 1) > 1e-6 + rounding):
            raise ValueError("逐轴来源需要可分辨的同向近轴 ABCD 矩阵，AD − BC 应为 1；请核对单位、轴向与数值范围。")
        return matrix

    def followed_by(self, matrix):
        product = compose_ray_matrices(matrix, self.matrix)
        return self.model_copy(update={"matrix": tuple(tuple(float(value) for value in row) for row in product)})

    def retains_hidden_constraints(self, current, ray, wavelength_nm):
        # At a conjugate plane (B=0), the local width and vergence encode every
        # supplied width constraint. A partially specified ray needs its origin
        # only when both local coordinates became unknown, retaining a correlation.
        hidden_width = (self.matrix[0][1] != 0 and missing_axis_inputs(wavelength_nm, current)
                        and (self.axis.radius_mm is not None or self.axis.wavefront != "unknown"))
        hidden_ray = ray == (None, None) and (self.offset_mm is None) != (self.angle_mrad is None)
        return bool(hidden_width or hidden_ray)

    def at_plane(self, wavelength_nm, *, plane_n=1.):
        """ABCD acts on q/n and (x,nθ); external radii of curvature and angles are physical."""
        source = self.axis
        source_n = getattr(self, "medium_n", 1.)
        (a, b), (c, d) = self.matrix
        radius, vergence = None, None
        if not missing_axis_inputs(wavelength_nm, source):
            try:
                effective = wavelength_nm * source.m2
                reduced_curvature = source.curvature_mm / source_n if source.wavefront == "curved" else None
                q = B.q_from_beam(source.radius_mm, reduced_curvature, effective)
                q = (a * q + b) / (c * q + d)
                if math.isfinite(q.real) and math.isfinite(q.imag) and q.imag > 0:
                    radius = B.w_from_q(q, effective)
                    vergence = (1 / q).real
            except (ArithmeticError, ValueError):
                pass
        elif b == 0 and a:
            # A conjugate plane images a known width irrespective of the unknown divergence.
            radius = abs(a) * source.radius_mm if source.radius_mm is not None else None
            if source.wavefront == "waist" and source.curvature_mm is None:
                vergence = c / a
            elif source.wavefront == "curved" and source.curvature_mm:
                vergence = (c + d * source_n / source.curvature_mm) / a
        if radius is not None and (not math.isfinite(radius) or radius <= 0):
            radius = None
        curvature = plane_n / vergence if vergence else None
        wavefront = "waist" if vergence == 0 else "curved" if curvature and math.isfinite(curvature) else "unknown"
        axis = source.model_copy(update={"radius_mm": radius, "curvature_mm": curvature if wavefront == "curved" else None,
                                         "wavefront": wavefront})
        center, reduced_angle = transfer_ray((self.offset_mm, self.angle_mrad * source_n / 1000 if self.angle_mrad is not None else None), self.matrix)
        return axis, (center, reduced_angle / plane_n if reduced_angle is not None else None)


class MediumAxisTransferOrigin(AxisTransferOrigin):
    """Physical input values in the stated medium; the matrix always acts in reduced coordinates."""
    medium_n: float = Field(gt=0, description="起始面在真空波长处的折射率。")


class ParaxialBeamState(BeamSource):
    """Separable paraxial widths and chief ray in an air-to-air, aligned forward frame."""
    frame: Literal["air.aligned-forward-ts.v2"]
    wavelength_nm: float = Field(gt=0, le=1e6)
    sagittal: SourceAxis
    model_warnings: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value,
                                           description="Validity limits inherited from upstream component models.")
    origins: dict[Literal["tangential", "sagittal"], AxisTransferOrigin] = Field(
        default_factory=dict, exclude_if=lambda value: not value,
        description="Original constraints and reduced ABCD transfer for partially known axes. A later conjugate plane can recover their known quantities.")

    @model_validator(mode="after")
    def consistent_origins(self):
        def agrees(left, right):
            if left is None or right is None:
                return left is right
            return math.isclose(left, right, rel_tol=1e-10, abs_tol=1e-14)

        for name, origin in self.origins.items():
            origin.forward_matrix(origin.matrix)
            expected, ray = origin.at_plane(self.wavelength_nm, plane_n=getattr(self, "medium_n", 1.))
            current = self.axis(name)
            if (current.model_dump(exclude={"radius_mm", "curvature_mm"}) != expected.model_dump(exclude={"radius_mm", "curvature_mm"})
                    or not agrees(current.radius_mm, expected.radius_mm) or not agrees(current.curvature_mm, expected.curvature_mm)
                    or not all(agrees(x, y) for x, y in zip(source_ray(self.launch_ray, name), ray))):
                raise ValueError(f"{name} 当前光束或束心与 origins 中的源面、传输矩阵不一致。要采用手填值，请移除该轴的 origins 记录。")
        return self


PARAXIAL_BEAM = Port(quantity="optics.paraxial-beam.ts-air.v2", label="逐轴近轴光束状态",
                     unit="air.aligned-forward-ts.v2", scale=1, shape="state", available_on_partial=True,
                     registration_label="发射与接收的空气参考面、横向原点、T/S 轴和传播正方向怎样重合")


class MediumBeamState(ParaxialBeamState):
    """Physical widths, centers, wavefront curvatures and slopes on a specified side of a plane."""
    frame: Literal["medium.aligned-forward-ts.v3"]
    medium_n: float = Field(gt=0, description="参考面所在均匀介质在该真空波长处的标量相位折射率。")
    width_basis: dict[Literal["tangential", "sagittal"], Literal["gaussian", "second_moment", "equivalent"]] = Field(
        default_factory=dict, exclude_if=lambda value: not value,
        description="实测等效宽度即使给出 M²≈1，也保留登记的口径。")
    origins: dict[Literal["tangential", "sagittal"], MediumAxisTransferOrigin] = Field(
        default_factory=dict, exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def validate_width_basis(self):
        for axis, basis in self.width_basis.items():
            derived = self.axis(axis).width_basis()
            if basis == "equivalent" and derived == "gaussian":
                # Older v3 receipts recorded equivalent widths only at the top level.
                setattr(self, axis, self.axis(axis).model_copy(update={"equivalent_radius": True}))
                if axis in self.origins and self.origins[axis].axis.width_basis() == "gaussian":
                    origin = self.origins[axis]
                    self.origins = {**self.origins, axis: origin.model_copy(update={
                        "axis": origin.axis.model_copy(update={"equivalent_radius": True})})}
            elif basis != derived:
                raise ValueError(f"{axis} 实测宽度口径与该轴的 M² 或二阶矩声明不一致。")
        return self


MEDIUM_BEAM = Port(quantity="optics.paraxial-beam.ts-medium.v3", label="介质内逐轴近轴光束状态",
                   unit="medium.aligned-forward-ts.v3", scale=1, shape="state", available_on_partial=True,
                   registration_label="参考面位于哪种介质和界面的哪一侧？请配准同一光束的横向原点、T/S 轴和正方向。")


def medium_beam_output(path=""):
    prefix = path + "." if path else ""
    return MEDIUM_BEAM.model_copy(update={"conditions":{
        "optics.vacuum-wavelength.nm": Condition(path=prefix+"wavelength_nm",label="真空波长 / nm"),
        "optics.reference-plane-index": Condition(path=prefix+"medium_n",label="参考面折射率 n")}})


def ray_component(a, x, b, p):
    if (a and x is None) or (b and p is None):
        return None
    value = float(a * (x or 0.) + b * (p or 0.))
    return value if math.isfinite(value) else None


def source_ray(ray, axis):
    if ray is None:
        return 0., 0.
    prefix = "t" if axis == "tangential" else "s"
    x = getattr(ray, prefix + "_offset_mm")
    angle = getattr(ray, prefix + "_angle_mrad")
    return x, angle / 1000 if angle is not None else None


def transfer_ray(ray, matrix):
    x, p = ray
    (a, b), (c, d) = matrix
    return ray_component(a, x, b, p), ray_component(c, x, d, p)


def axis_origin(source, name):
    if isinstance(source, ParaxialBeamState) and name in source.origins:
        return source.origins[name]
    offset, direction = source_ray(source.launch_ray, name)
    values = dict(axis=source.axis(name), offset_mm=offset, angle_mrad=direction * 1000 if direction is not None else None,
                  matrix=((1., 0.), (0., 1.)))
    return MediumAxisTransferOrigin(**values, medium_n=source.medium_n) if isinstance(source, MediumBeamState) else AxisTransferOrigin(**values)


def transport_beam(source, wavelength_nm, matrices, *, model_warnings=(), output_n=None):
    """Transport to a physical reference plane; callers of the v2 interface receive its air frame."""
    axes, rays, origins = {}, {}, {}
    warnings = tuple(dict.fromkeys((*getattr(source, "model_warnings", ()), *model_warnings)))
    for name, prefix in (("tangential", "t"), ("sagittal", "s")):
        origin = axis_origin(source, name).followed_by(matrices[name])
        origin.forward_matrix(origin.matrix)
        current, (center, direction) = origin.at_plane(wavelength_nm, plane_n=1. if output_n is None else output_n)
        axes[name] = current
        rays[prefix + "_offset_mm"] = center
        rays[prefix + "_angle_mrad"] = direction * 1000 if direction is not None else None
        if origin.retains_hidden_constraints(current, (center,direction), wavelength_nm):
            origins[name] = origin
    values = dict(wavelength_nm=wavelength_nm, **axes, model_warnings=warnings, launch_ray=LaunchRay(**rays) if source.launch_ray is not None else None)
    if output_n is not None:
        records = {name: {**origin.model_dump(), "medium_n": getattr(origin, "medium_n", 1.)} for name, origin in origins.items()}
        return MediumBeamState(frame=MEDIUM_BEAM.unit, medium_n=output_n, origins=records,
                               width_basis=getattr(source, "width_basis", {}), **values)
    return ParaxialBeamState(frame=PARAXIAL_BEAM.unit, origins=origins, **values)


def missing_axis_inputs(wavelength_nm, source):
    missing = []
    if wavelength_nm is None:
        missing.append("真空波长")
    if source.radius_mm is None:
        missing.append("输入半径")
    if source.m2 is None:
        missing.append("M²")
    if source.wavefront == "unknown" or (source.wavefront == "curved" and not source.curvature_mm):
        missing.append("输入波前曲率或明确的束腰假设")
    if source.wavefront == "waist" and source.curvature_mm is not None:
        missing.append("一致的波前条件（已选束腰，请清除有限曲率半径或改选已知曲率）")
    return missing


def missing_source_inputs(path, source):
    return missing_axis_inputs(path.wavelength_nm, source)

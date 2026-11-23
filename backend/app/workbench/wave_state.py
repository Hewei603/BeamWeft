"""Portable scalar envelopes, encoded by their analytic launch and resolved operators.

All phases are transverse envelope phases. A constant longitudinal carrier phase
is omitted. Intensities integrate to a fraction of the original launch power,
including after stops and planar index changes. The T/S axes are unfolded and
aligned throughout. This prescription does not represent an arbitrary measured
complex field, mutual coherence, or an absolute phase reference between sources.
"""

import math
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from ..physics.scalar_field import GaussianAxis
from ..study_api import Port, StrictInput
from ..version import provider_display_name
from .optics import ClearAperture, QuadraticSurface, UniformSection


COHERENT_FIELD = Port(
    quantity="optics.coherent-fresnel-field.v1",
    label="可续算的相干光场",
    unit="unfolded-ts-mm.fresnel.v1",
    scale=1,
    shape="state",
    registration_label="说明如何将来源出射面与本段入射面重合，并对齐展开光轴和 T/S 横轴",
)
MAX_CHAIN_ELEMENTS = 512


class LaunchAxis(StrictInput):
    radius_mm: float = Field(gt=0, description="原始入射场的 1/e² 强度半径。")
    curvature_mm: float | None = Field(description="空气等效入射波前曲率半径。null 明确表示平面波前。")
    center_mm: float
    reduced_slope: float = Field(description="原始入射横向相位斜率除以真空波数，相当于 nθ。")

    @field_validator("curvature_mm")
    @classmethod
    def finite_or_flat(cls, value):
        if value == 0:
            raise ValueError("有限波前曲率半径必须非零；平面波前用 null。")
        return value

    @classmethod
    def from_axis(cls, axis):
        return cls(radius_mm=axis.radius, curvature_mm=axis.curvature, center_mm=axis.center, reduced_slope=axis.slope)

    def axis(self):
        return GaussianAxis(self.radius_mm, self.curvature_mm, self.center_mm, self.reduced_slope)


class FresnelMedium(StrictInput):
    kind: Literal["medium"]
    length_mm: float = Field(ge=0, le=1e6)
    index: float = Field(ge=1, le=5)

    def section(self):
        return UniformSection(self.length_mm, self.index)


class FresnelPhase(StrictInput):
    kind: Literal["phase"]
    c_t_per_mm: float
    c_s_per_mm: float

    def section(self):
        return QuadraticSurface(self.c_t_per_mm, self.c_s_per_mm)


Section = Annotated[FresnelMedium | FresnelPhase, Field(discriminator="kind")]


class ResolvedLaw(StrictInput):
    """Identification of the original provider. Only the numerical sections are executed."""
    study: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    version: str = Field(min_length=1)
    scope: str


class FresnelElement(StrictInput):
    name: str = Field(min_length=1, max_length=100)
    uid: str = Field(max_length=100)
    length_mm: float = Field(ge=0, le=1e6)
    clear_aperture: ClearAperture | None = Field(default=None, exclude_if=lambda value: value is None)
    sections: list[Section] = Field(max_length=3)
    law: ResolvedLaw | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def length_matches(self):
        physical = math.fsum(step.length_mm for step in self.sections if isinstance(step, FresnelMedium))
        if not math.isclose(physical, self.length_mm, rel_tol=1e-12, abs_tol=0):
            raise ValueError("元件记录的物理长度与均匀传播段合计不符。")
        return self

    @classmethod
    def capture(cls, element, length, sections, law_scope=None):
        frozen = []
        for section in sections:
            if isinstance(section, UniformSection):
                if section.length_mm:
                    frozen.append(FresnelMedium(kind="medium", length_mm=section.length_mm, index=section.index))
            elif isinstance(section, QuadraticSurface):
                if section.tangential or section.sagittal:
                    frozen.append(FresnelPhase(kind="phase", c_t_per_mm=section.tangential, c_s_per_mm=section.sagittal))
            else:
                raise ValueError(f"当前标量场方法无法传播「{element.name}」声明的物理传输。")
        law = None
        if element.prescription is not None and law_scope is not None:
            prescription = element.prescription
            law = ResolvedLaw(study=prescription.study, provider=provider_display_name(prescription.provider),
                              version=prescription.version, scope=law_scope)
        return cls(name=element.name, uid=element.uid, length_mm=length, clear_aperture=element.clear_aperture,
                   sections=frozen, law=law)


class FieldPlane(StrictInput):
    beam_uid: str = Field(min_length=1, max_length=100)
    plane_uid: str | None = Field(max_length=100)
    name: str = Field(min_length=1, max_length=150)
    z_mm: float = Field(description="该段工程自身展开光轴坐标中的出射位置。")


class CoherentField(StrictInput):
    schema_version: Literal[1]
    wavelength_nm: float = Field(gt=0, le=1e6)
    tangential: LaunchAxis
    sagittal: LaunchAxis
    operations: list[FresnelElement] = Field(max_length=MAX_CHAIN_ELEMENTS,
        description="从原始入射面到当前出射面的实际传输顺序。孔径位于每个元件入口，随后执行已求值的传输段。")
    plane: FieldPlane
    distance_mm: float = Field(ge=0, description="从原始入射面起累计走过的物理长度。")
    model_notes: list[str] = Field(description="按原始物理模型保留的限制或波长适用范围。网格误差在每次传播时重估。")

    @model_validator(mode="after")
    def distance_matches(self):
        if not math.isclose(self.distance_mm, math.fsum(op.length_mm for op in self.operations), rel_tol=1e-12):
            raise ValueError("累计距离与保存的光场传播记录不一致。")
        return self

    def grid_path(self, stage_start):
        """Rebase upstream aperture reports into the consuming stage's local coordinates."""
        z = stage_start - self.distance_mm
        path = []
        for op in self.operations:
            path.append((op, z, tuple(step.section() for step in op.sections)))
            z += op.length_mm
        return path

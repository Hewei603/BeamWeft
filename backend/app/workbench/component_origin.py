"""Provenance shared by instances of reusable optical components."""

import math

from pydantic import Field

from ..study_api import StrictInput


class ComponentOrigin(StrictInput):
    model_revision_id: int = Field(gt=0)
    version: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=100)
    instance_uid: str = Field(min_length=1, max_length=100)
    ordinal: int = Field(ge=1)
    wavelength_nm: float = Field(gt=0, le=1e6)
    note: str = Field(default="", max_length=1200)


def model_wavelength_notes(elements, wavelength_nm):
    origins = {}
    for element in elements:
        origin = element.component_origin
        spectral = getattr(element, "index_reference", None)
        if spectral is not None and element.kind == "space" and spectral.wavelength_nm == wavelength_nm:
            continue
        if origin and (wavelength_nm is None or not math.isclose(origin.wavelength_nm, wavelength_nm, rel_tol=1e-12)):
            origins[origin.model_revision_id] = origin
    current = "尚未填写" if wavelength_nm is None else f"{wavelength_nm:g} nm"
    return [
        f"「{origin.name}」v{origin.version} 的来源波长为 {origin.wavelength_nm:g} nm，"
        f"当前光路波长为{current}。计算沿用工程内填写的参数，请核对色散与新波长下的标定依据。"
        for origin in origins.values()
    ]

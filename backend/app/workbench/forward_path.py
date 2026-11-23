"""Resolve one pass of an ordered project in the experiment's registered coordinates."""

import math
from dataclasses import dataclass

from .beam_source import BeamSource
from .observation import host_positions, host_route, locate_planes
from .optics import OpticalElement


@dataclass
class ForwardPath:
    wavelength_nm: float | None
    source: BeamSource
    name: str
    start_mm: float
    elements: list[tuple[OpticalElement, float | None, float]]


def forward_path(layout, beam_uid, plane_uid=None, *, source_wavelength=None, element_exit=None):
    """The terminal distance may cut a homogeneous segment. Its entrance stop still applies."""
    anchors = host_positions(layout.system)
    plane = None
    if plane_uid:
        found = locate_planes(layout, anchors).get(plane_uid)
        if not found:
            raise ValueError("当前工程中找不到该观察面，请核对观察面标识。")
        plane, issue = found
        if issue:
            raise ValueError(issue)
    if beam_uid == "primary":
        system = layout.system
        if system.mode != "single_pass" or not system.elements:
            raise ValueError("请选单程主光路或独立入射支路。腔本征模依赖往返传播。")
        if system.wavelength_nm is None and source_wavelength is not None:
            system = type(system).model_validate({**system.model_dump(), "wavelength_nm": source_wavelength})
        name, source, wavelength = "主光束", system.primary_source(), system.wavelength_nm
        start, train, issue = 0.0, [], None
        route = [(el, el.n) for el in system.elements]
    else:
        arm = next((p for p in layout.incident_paths if p.uid == beam_uid), None)
        if arm is None:
            raise ValueError("工程中找不到所选入射支路，请核对光路标识。")
        if arm.wavelength_nm is None and source_wavelength is not None:
            arm = type(arm).model_validate({**arm.model_dump(), "wavelength_nm": source_wavelength})
        name, source, wavelength = arm.name, arm, arm.wavelength_nm
        host, issue = host_route(arm, anchors, layout.system.wavelength_nm)
        if not host and not (element_exit and element_exit.scope == "arm"):
            raise ValueError(issue)
        if not host and arm.target_uid not in anchors:
            raise ValueError(issue or "入射支路缺少工程定位元件。")
        start = anchors[arm.target_uid][2] - math.fsum(el.length_mm for el in arm.elements)
        train = [(el, el.n, el.length_mm) for el in arm.elements]
        route = [(el, n) for el, _, n in host]
    if (
        wavelength is not None
        and source_wavelength is not None
        and not math.isclose(wavelength, source_wavelength, rel_tol=1e-12)
    ):
        raise ValueError(
            f"上游为 {source_wavelength:g} nm，本段「{name}」为 {wavelength:g} nm。请核对真空波长。"
        )
    path = ForwardPath(wavelength or source_wavelength, source, name, start, train)
    if element_exit and element_exit.scope == "arm":
        for i, (el, _, _) in enumerate(train):
            if el.uid == element_exit.element_uid:
                path.elements = train[: i + 1]
                return path
        raise ValueError("所选入射支路中找不到引用的元件出射面。")
    for el, index in route:
        if plane and el.uid == plane.target_uid:
            train.append((el, index, plane.depth_mm))
            break
        train.append((el, index, el.length_mm))
        if element_exit and el.uid == element_exit.element_uid:
            break
    else:
        if issue or plane or element_exit:
            raise ValueError(issue or "所选光路未到达该参考面。")
    return path

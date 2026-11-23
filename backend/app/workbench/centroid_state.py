"""Typed local-plane intensity observations and signed predictions from a frozen station."""

from typing import Annotated, Literal
from urllib.parse import quote

from pydantic import Field

from ..study_api import Port, StrictInput


DETECTOR_CENTROID = Port(
    quantity="measurement.transverse-centroid.v1",
    unit="local-xy.mm.v1",
    shape="state",
    scale=1,
    label="探测器坐标中的光强质心",
    registration_label="此观测的平面、光束及仪器坐标与标定的对应关系",
)
STATION_CENTROID = Port(
    quantity="optics.station-centroid.v1",
    unit="unfolded-ts.mm.v1",
    shape="state",
    scale=1,
    label="观察面在工程坐标中的预测束心",
    registration_label="该工程观察面与实测位置、光束及工况的对应关系",
)


class DetectorCentroid(StrictInput):
    x_mm: float = Field(title="本地横向质心 / mm")
    y_mm: float = Field(title="本地纵向质心 / mm")
    basis: str = Field(min_length=1, max_length=4000, pattern=r"\S", title="采集及坐标标定依据")
    quality: Literal["unassessed", "usable", "conditional", "limited"] = "unassessed"
    window_coverage: Literal["unverified", "contains_beam", "truncated"] = "unverified"
    wavelength_nm: float | None = Field(default=None, gt=0)
    plane: str | None = None
    warnings: list[str] = Field(default_factory=list)
    pixel_center: tuple[float, float] | None = None
    pitch_um: tuple[Annotated[float, Field(gt=0)], Annotated[float, Field(gt=0)]] | None = None
    covariance_mm2: tuple[tuple[float, float], tuple[float, float]] | None = Field(
        default=None,
        title="选区光强分布的空间协方差 / mm²",
        description="描述光斑的空间展宽。质心测量的不确定度需独立评估。",
    )
    window: tuple[int, int, int, int] | None = None
    frame: int | None = Field(default=None, gt=0)
    attachment_id: int | None = Field(default=None, gt=0)


class StationCentroid(StrictInput):
    plane_uid: str = Field(min_length=1, max_length=100)
    beam_uid: str = Field(min_length=1, max_length=100)
    z_mm: float | None = None
    wavelength_nm: float | None = Field(default=None, gt=0)
    t_mm: float | None = Field(default=None, title="工程切向束心 / mm")
    s_mm: float | None = Field(default=None, title="工程弧矢束心 / mm")
    launch_basis: Literal["specified", "reference_axis", "ideal_on_axis", "unrecorded"]
    notes: dict[str, str] = Field(default_factory=dict)


def station_centers(project, analysis, only_port=None):
    """Publish the saved readings, including physical limits that prevent a comparison."""
    planes = {p["uid"]: p["name"] for p in project.get("observation_planes", ())}
    paths = {p["uid"]: p for p in project.get("incident_paths", ())}
    system = project["system"]
    for section in (analysis.get("sections") or {}).get("planes", ()):
        site = section["uid"]
        if site not in planes or section.get("message"):
            continue
        for beam in section.get("beams", ()):
            uid = beam["uid"]
            key = f"station-center-v1/{quote(site, safe='')}/{quote(uid, safe='')}/center"
            if only_port is not None and key != only_port:
                continue
            if uid == "primary":
                source = system.get("source") or {}
                name = "主光束" if system["mode"] == "single_pass" else "腔内正向本征模"
            elif uid in paths:
                source = paths[uid]
                name = source["name"]
            else:
                continue
            basis = beam.get("launch_basis")
            if basis not in ("specified", "reference_axis", "ideal_on_axis"):
                basis = "unrecorded"
            notes, centers = {}, {}
            for axis, field in (("tangential", "t_mm"), ("sagittal", "s_mm")):
                center = beam.get("field_axes", {}).get(axis, {}).get("center_mm")
                reason = None
                if beam.get("registration_available") is False:
                    reason = "这条光路与此观察面尚未连通。"
                elif uid != "primary" and beam.get("registration_available") is not True:
                    reason = "原计算没有记录此支路与观察面的对应关系。"
                elif beam.get("width_basis", {}).get(axis) == "clipped":
                    reason = "有限孔径后的实物光强质心需要包含截光的场或光线分布。"
                elif uid == "primary" and system["mode"] != "single_pass" and not analysis.get("stable_both_planes"):
                    reason = "该腔没有双平面稳定的基模，缺少此束的实物质心预测。"
                elif basis == "unrecorded" or (source.get("launch_ray") is not None and basis != "specified"):
                    reason = "原计算没有记录所用的束心坐标依据。"
                elif center is None:
                    reason = "原计算未确定此方向的束心，请核对光源位置、指向和至观察面的传输。"
                elif (
                    uid == "primary"
                    and system["mode"] == "single_pass"
                    and not (
                        source
                        or system.get("input_radius_mm") is not None
                        or system.get("input_curvature_mm") is not None
                    )
                ):
                    reason = "原工程没有给出主光束的入射状态。"
                if reason:
                    notes[axis] = reason
                centers[field] = None if reason else center
            details = {
                "scope": "observation",
                "plane_uid": site,
                "path_uid": uid,
                "element_uid": section.get("target_uid"),
                "z_mm": section.get("z_mm"),
                "wavelength_nm": beam.get("wavelength_nm"),
            }
            yield (
                key,
                {
                    **centers,
                    "plane_uid": site,
                    "beam_uid": uid,
                    "launch_basis": basis,
                    "notes": notes,
                    "z_mm": details["z_mm"],
                    "wavelength_nm": details["wavelength_nm"],
                },
                f"{planes[site]} · {name}",
                details,
            )

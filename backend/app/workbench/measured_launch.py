"""Transfer a registered width fit to one source axis of an independent optical model."""
import math
from copy import deepcopy
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, model_validator

from .authoring import EditableSystem
from .beam_predictions import fitted_curvature, predictions
from .beam_source import FitOrigin, SourceAxis
from .beam_studies import beam_study_history
from .projects import read_project
from .study_inputs import SaveVariant, VariantLabel, save_variant


class MeasuredLaunch(VariantLabel):
    revision_id: int = Field(gt=0, description="Exact model to use for the trial.")
    study_id: int = Field(gt=0, description="Case-scoped original width fit, as listed by read_beam_studies.")
    axis: Literal["tangential", "sagittal"] = Field(description="Transverse model axis aligned with the measured axis.")
    source_z_mm: float = Field(description="Position of the model's source plane in the original fit coordinate, increasing downstream.")
    registration: str = Field(min_length=1, max_length=800, pattern=r"\S",
        description="Evidence for common light, transverse direction, forward axial frame and uninterrupted propagation up to the source plane.")
    normal_exit_to_air: bool = Field(default=False, exclude_if=lambda value: not value,
        description="确认标定坐标为垂轴平面出射界面：实测在介质内侧，工程入射源在同一界面空气侧。")
    path_uid: str | None = Field(default=None, min_length=1, max_length=100,
        description="Use primary for the source of a single-pass optical system, or an existing incident path UID.")
    entry_uid: str | None = Field(default=None, min_length=1, max_length=100, description="For a new probe: first shared ordered element.")
    exit_uid: str | None = Field(default=None, min_length=1, max_length=100, description="For a new probe: last shared ordered element, inclusive.")

    @model_validator(mode="after")
    def route(self):
        if self.path_uid:
            if self.entry_uid or self.exit_uid:
                raise ValueError("已有光束沿用其光路。建立独立探测光束时指定起止元件。")
        elif not self.entry_uid or not self.exit_uid:
            raise ValueError("请指定要更新的光束，或选择新光束经过的首尾元件。")
        return self


def fitted_axis(study, request):
    fit, meta = study.get("fit"), study["metadata"]
    if not fit:
        raise ValueError("请选择生成束腰和发散角的原始拟合。")
    index = meta.get("medium_n")
    if index is None or (index != 1 and not request.normal_exit_to_air):
        raise ValueError("工程入射源定义在空气中。请登记已知折射率，并明确实测介质到空气的垂轴平面出射界面。")
    if fit.get("m2") is None or not meta.get("wavelength_nm"):
        raise ValueError("宽度拟合可以单独查看。写入光源需要实测真空波长，补全后重新拟合。")
    m2 = fit["m2"]
    if m2 < 1 - 64 * math.ulp(1.) or m2 > 10000:
        raise ValueError("拟合的 M² 超出本传播模型的物理或数值范围。请核对直径口径、波长、介质和标定。")
    sample = predictions(study, [request.source_z_mm])[0]
    curvature = fitted_curvature(fit, request.source_z_mm)
    warnings = list(fit.get("warnings", []))
    if sample["extrapolated"]:
        warnings.append("源面位于拟合测点之外，输入依赖同一均匀传播段的外推。")
    origin = FitOrigin(study_id=study["id"], study_key=study.get("study_key"), measured_axis=meta["axis"], coordinate_mm=request.source_z_mm,
                       registration=request.registration, width_basis=meta["width_basis"], wavelength_nm=meta["wavelength_nm"],
                       radius_mm=sample["radius_mm"], curvature_mm=curvature, medium_n=index if index != 1 else None, m2=m2, warnings=warnings)
    return SourceAxis(radius_mm=origin.radius_mm, wavefront="waist" if curvature is None else "curved",
                      curvature_mm=curvature/index if curvature is not None else None,
                      m2=max(1., m2), second_moment_radius=origin.width_basis == "second_moment", fit_origin=origin)


def bind_measured_launch(db, case_id, request):
    revision = read_project(db, case_id, request.revision_id)
    study = beam_study_history(db, case_id, request.study_id)
    value = fitted_axis(study, request)
    project = deepcopy(revision["project"])
    wavelength = value.fit_origin.wavelength_nm
    paths = project.setdefault("incident_paths", [])
    elements = project["system"]["elements"]
    anchors = {}
    z = 0.
    for i, element in enumerate(elements):
        anchors[element["uid"]] = (i, z)
        z += element["length_mm"]
    if request.path_uid == "primary":
        system = project["system"]
        if system["mode"] != "single_pass" or not elements:
            raise ValueError("主光源需要单程传播光路。谐振腔的本征模由腔求解器确定，可将实测光束接入独立支路。")
        source = EditableSystem.model_validate(system).primary_source().model_dump()
        for key in ("input_radius_mm", "input_curvature_mm", "beam_quality_m2"):
            system.pop(key, None)
        system["source"] = source
        wavelength_owner, source_z, path_uid = system, 0., "primary"
    elif request.path_uid:
        path = next((p for p in paths if p["uid"] == request.path_uid), None)
        if path is None or path["target_uid"] not in anchors:
            raise ValueError("当前版本中没有这条已定位的入射支路。")
        source_z = anchors[path["target_uid"]][1] - sum(el["length_mm"] for el in path["elements"])
        source, wavelength_owner, path_uid = path, path, path["uid"]
    else:
        if request.entry_uid not in anchors or request.exit_uid not in anchors or anchors[request.entry_uid][0] > anchors[request.exit_uid][0]:
            raise ValueError("请选择工程中沿传播方向排列的首尾元件。")
        source_z = anchors[request.entry_uid][1]
        uid = str(uuid5(NAMESPACE_URL, "laserclaw:measured-launch:" + request.model_dump_json()))
        path = {"uid":uid, "name":f"实测入射束 · 拟合 #{study['id']}", "target_uid":request.entry_uid,
                "through_uid":request.exit_uid, "tangential":{}, "sagittal":{},
                "launch_ray":{"t_offset_mm":None,"s_offset_mm":None,"t_angle_mrad":None,"s_angle_mrad":None}}
        paths.append(path)
        source, wavelength_owner, path_uid = path, path, uid
    previous_wavelength = wavelength_owner.get("wavelength_nm")
    if previous_wavelength is not None and not math.isclose(previous_wavelength, wavelength, rel_tol=1e-12):
        raise ValueError("拟合波长与所选光束不同。请核对测量工况，或另建对应波长的光束。")
    if (study["source"].get("revision_id") == request.revision_id
            and not math.isclose(source_z, request.source_z_mm, rel_tol=1e-12, abs_tol=1e-9)):
        raise ValueError(f"本工程登记的测点共用展开零点，所选光源位于 z={source_z:g} mm。")
    wavelength_owner["wavelength_nm"] = wavelength
    if source.get("sagittal") is None:
        source["sagittal"] = deepcopy(source["tangential"])
    source[request.axis] = value.model_dump()
    base = revision.get("base_revision_id") if revision.get("model_kind") == "draft" else request.revision_id
    saved = save_variant(db, case_id, SaveVariant(project=project, source_revision_id=request.revision_id,
                         base_revision_id=base, variant_name=request.variant_name or revision.get("variant_name") or f"实测入射 · 拟合 #{study['id']}"))
    return {**saved, "launch_path_uid":path_uid, "launch_axis":request.axis,
            "fit_origin":value.fit_origin.model_dump()}

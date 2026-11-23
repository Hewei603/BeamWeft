"""Register transcribed observations against immutable model sections and case originals."""
import hashlib
import json
import math
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy.exc import IntegrityError

from ..models import Attachment, GeneratedContent
from .optics import StrictInput
from .authoring import EditableLayout
from .observation import LayoutSystem, host_positions, host_route, locate_planes, retains_source_width
from .projects import read_project


class BeamReading(StrictInput):
    revision_id: int = Field(gt=0)
    plane_uid: str = Field(min_length=1, max_length=100)
    beam_uid: str = Field(min_length=1, max_length=100)
    attachment_id: int = Field(gt=0)
    locator: str = Field(min_length=1, max_length=500, description="Sheet/cells, image region or instrument record identifier.")
    conditions: str = Field(min_length=1, max_length=1000, description="Experimental run and relevant acquisition conditions.")
    wavelength_nm: float | None = Field(default=None, gt=0)
    definition: Literal["radius_1e2", "diameter_1e2", "fwhm_diameter", "d4sigma_diameter", "unknown"]
    axes_aligned: bool = Field(default=False, description="The source axes were explicitly registered to model T/S at the stated plane.")
    unit: Literal["um", "mm"]
    tangential: float | None = Field(default=None, gt=0)
    sagittal: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def have_data(self):
        if self.tangential is None and self.sagittal is None:
            raise ValueError("至少填写一个方向的实测宽度。")
        if not self.locator.strip() or not self.conditions.strip():
            raise ValueError("请填写能定位原读数的位置及实验工况。")
        return self


WIDTH_FACTORS = {"radius_1e2": 1., "diameter_1e2": 2., "d4sigma_diameter": 2.,
                 "fwhm_diameter": math.sqrt(2 * math.log(2))}


def instrument_width(beam, axis, definition, unit):
    """Return only a propagated observable in the instrument convention."""
    if beam.get("radius_origin", {}).get(axis) == "source_input":
        return None, "此处半径由光源输入指定，缺少独立传播预测。"
    if axis not in beam["radii_mm"]:
        return None, beam.get("notes", {}).get(axis, "该方向缺少模型结果。")
    basis = beam.get("width_basis", {}).get(axis)
    if basis != "gaussian" and not (basis == "second_moment" and definition == "d4sigma_diameter"):
        return None, "等效 M² 包络需要明确的二阶矩输入与 D4σ 读数才能作差。"
    width = beam["radii_mm"][axis] * WIDTH_FACTORS[definition] * (1000 if unit == "um" else 1)
    if not math.isfinite(width):
        return None, "此方向的模型宽度超出可表示范围，请核对光路及单位。"
    return width, None


def width_comparison(reading, beam):
    """Predict the instrument's observable. Never infer a measured profile shape from its FWHM."""
    definition = reading["definition"]
    message = None
    if reading["wavelength_nm"] is None and beam["wavelength_nm"] is None:
        message = "需确认实测与模型的波长。"
    elif beam["wavelength_nm"] is None:
        message = f"仪器记录为 {reading['wavelength_nm']:g} nm，模型中这条光束的波长待补。"
    elif reading["wavelength_nm"] is None:
        message = f"模型波长为 {beam['wavelength_nm']:g} nm，需从原件确认实测波长。"
    elif not math.isclose(reading["wavelength_nm"], beam["wavelength_nm"], rel_tol=1e-9):
        message = f"实测与模型波长不同。仪器为 {reading['wavelength_nm']:g} nm，模型为 {beam['wavelength_nm']:g} nm。"
    elif not reading["axes_aligned"]:
        message = "仪器轴尚未与模型 T/S 配准。"
    elif definition not in WIDTH_FACTORS:
        message = "实测宽度定义未知。"
    widths, notes = {}, {}
    for axis in ("tangential", "sagittal"):
        if reading.get(axis) is None:
            continue
        widths[axis] = {"measured":reading[axis]}
        if message:
            continue
        prediction, issue = instrument_width(beam, axis, definition, reading["unit"])
        if issue:
            notes[axis] = issue
        else:
            widths[axis].update(predicted=prediction, residual=reading[axis]-prediction,
                                model_width_basis=beam["width_basis"][axis])
    results = sum("residual" in item for item in widths.values())
    status = "needs_input" if not results else "completed" if results == len(widths) else "partial"
    return {"status":status, "widths":widths, "axis_notes":notes, "message":message}


def registration_site(revision, plane_uid, beam_uid):
    """Locate an acquisition in frozen geometry, including revisions saved without field predictions."""
    section = next((s for s in revision["analysis"].get("sections", {}).get("planes", []) if s["uid"] == plane_uid), {})
    beam = next((b for b in section.get("beams", []) if b["uid"] == beam_uid), None)
    if beam and beam.get("registration_available") is False:
        raise ValueError("此光束的已登记光路未经过所选观察面。")
    coordinates = ("target_uid","depth_mm","z_mm")
    if beam and all(section.get(key) is not None for key in coordinates) and (
        beam_uid == "primary" or beam.get("registration_available") or beam.get("radii_mm") or beam.get("field_axes")
    ):
        return {key:section[key] for key in coordinates}, beam
    project = revision["project"]
    layout = EditableLayout.model_validate({k:v for k,v in project.items() if k in EditableLayout.model_fields})
    anchors = host_positions(layout.system)
    plane, issue = locate_planes(layout, anchors).get(plane_uid, (None, "此工程版本没有指定的观察面。"))
    if plane is None:
        raise ValueError(issue)
    wavelength = layout.system.wavelength_nm
    if beam_uid != "primary":
        path = next((p for p in layout.incident_paths if p.uid == beam_uid), None)
        if path is None:
            raise ValueError("此工程版本中找不到指定的光束。")
        route, _ = host_route(path, anchors, wavelength)
        if not any(el.uid == plane.target_uid for el, _, _ in route):
            raise ValueError("此光束的已登记光路未经过所选观察面。")
        wavelength = path.wavelength_nm
    z = anchors[plane.target_uid][2] + plane.depth_mm
    if beam is None:
        beam = {"wavelength_nm":wavelength,"radii_mm":{}, "radius_origin":{},
                "notes":dict.fromkeys(("tangential","sagittal"), "登记版本尚无该光束在此处的宽度预测。")}
    return {"target_uid":plane.target_uid, "depth_mm":plane.depth_mm, "z_mm":z}, beam


def register_reading(db, case_id, args, origin):
    raw = args.model_dump()
    key = "beam:" + hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    def prior():
        return db.query(GeneratedContent).filter_by(case_id=case_id, operation_key=key).one_or_none()
    previous = prior()
    if previous:
        return {"id": previous.id, **previous.content}
    revision = read_project(db, case_id, args.revision_id)
    section, beam = registration_site(revision, args.plane_uid, args.beam_uid)
    attachment = db.get(Attachment, args.attachment_id)
    if attachment is None or attachment.case_id != case_id or not Path(attachment.filepath).is_file():
        raise ValueError("实验原件需已归档于本案例并可读取。")
    with open(attachment.filepath, "rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if attachment.content_hash and digest != attachment.content_hash:
        raise ValueError("原件校验值与归档时不一致，请核对原文件。")
    if (args.beam_uid == "primary" and "radius_origin" not in beam and section["z_mm"] == 0
            and revision["project"]["system"]["mode"] == "single_pass"):
        system = LayoutSystem.model_validate(revision["project"]["system"])
        index = next(i for i,el in enumerate(system.elements) if el.uid == section["target_uid"])
        beam = {**beam, "radius_origin":{axis:"source_input" for axis in beam["radii_mm"]
                                         if retains_source_width(system.elements[:index],axis,system.wavelength_nm)}}
    compared = width_comparison(raw, beam)
    scale = 0.001 if args.unit == "um" else 1.0
    radii, differences = {}, {}
    if args.definition in {"radius_1e2", "diameter_1e2", "d4sigma_diameter"}:
        divisor = 1 if args.definition == "radius_1e2" else 2
        for axis, item in compared["widths"].items():
            radii[axis] = item["measured"] * scale / divisor
            if "residual" in item:
                differences[axis] = item["residual"] * scale / divisor
    data = {**raw, **section, "transcribed_by": origin,
            "source_path": attachment.relative_path or attachment.filename, "source_sha256": digest,
            "radii_mm": radii, "prediction_mm": beam["radii_mm"], "measured_minus_model_mm": differences,
            "native_widths":compared["widths"], "axis_notes":compared["axis_notes"], "comparison_note":compared["message"]}
    row = GeneratedContent(case_id=case_id, content_type="beam_observation", content=data, operation_key=key)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        previous = prior()
        if previous:
            return {"id": previous.id, **previous.content}
        raise
    return {"id": row.id, **data}


def readings_for_revision(db, case_id, revision_id):
    read_project(db, case_id, revision_id)
    rows = db.query(GeneratedContent).filter_by(case_id=case_id, content_type="beam_observation").filter(
        GeneratedContent.content["revision_id"].as_integer() == revision_id).order_by(GeneratedContent.id).all()
    return [{"id": row.id, **row.content} for row in rows]

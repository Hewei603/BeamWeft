"""Compare a single archived reading with optical models in the detector's reference frame."""

import hashlib
import json
import math
import re
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import AgentTask, GeneratedContent
from .beam_readings import registration_site, width_comparison
from .beam_studies import fit_evidence_key
from .detector_position import DetectorPositionBound, apply_position_bound, common_offsets
from .measurement_sources import case_original
from .authoring import EditableLayout
from .observation import ObservationPlane, analyze_sections, detector_segment, host_positions
from .optics import StrictInput
from .projects import ReadProject, read_project


DataRole = Literal["unspecified", "held_out", "model_input", "different_configuration"]


class ReadingQuery(ReadProject):
    all_revisions: bool = Field(
        default=False,
        description="Search every version of this case. If a revision_id is also supplied it identifies the starting model, not a filter.",
    )
    before_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def query_scope(self):
        if self.before_id and not self.all_revisions:
            raise ValueError("按页读取需要选择全部版本。")
        return self


class WidthBounds(StrictInput):
    tangential: float | None = Field(default=None, ge=0)
    sagittal: float | None = Field(default=None, ge=0)
    source: str = Field(
        min_length=1,
        max_length=500,
        pattern=r"\S",
        description="Documented absolute bounds on measured widths in the original unit and definition. Do not insert a standard deviation as a hard bound.",
    )

    @model_validator(mode="after")
    def at_least_one_axis(self):
        if self.tangential is None and self.sagittal is None:
            raise ValueError("请至少提供一个方向的测量绝对误差界限。")
        return self


class CompareBeam(StrictInput):
    reading_id: int = Field(gt=0)
    target_beam_uid: str | None = Field(default=None, min_length=1, max_length=100,
        description="When beam identities differ, choose the corresponding beam in the target. Document the transfer in registration_note.")
    revision_id: int | None = Field(
        default=None,
        gt=0,
        description="Target project or immutable study. Default for the Agent is the question's bound model.",
    )
    position_reference: Literal["fixed_z", "element_depth"]
    position_mm: float | None = Field(
        default=None,
        description="Optional documented detector coordinate: unfolded z from the shared origin or depth from the source element entrance, according to position_reference. Omit to use the original registered position.",
    )
    registration_note: str = Field(
        min_length=1,
        max_length=800,
        pattern=r"\S",
        description="Evidence for shared physical datum or moving mount, beam, axis orientation, and relevant run conditions. Ask if the mapping is unknown.",
    )
    data_role: DataRole = Field(
        default="unspecified",
        description="Relationship to the target: unspecified, independent held-out observation of the same configuration, model input, or a known different experimental configuration for prediction only.",
    )
    reference_data_role: DataRole | None = Field(default=None, description=(
        "Relationship to the reading's registration model. A different model can use the same reading "
        "for calibration even when it is held out from the target. Omit if unknown."))
    width_bounds: WidthBounds | None = None
    position_bound: DetectorPositionBound | None = None


def observation_index(db, case_id, before=None):
    query = db.query(GeneratedContent).filter_by(case_id=case_id, content_type="beam_observation")
    if before is not None:
        query = query.filter(GeneratedContent.id < before)
    rows = query.order_by(GeneratedContent.id.desc()).limit(51).all()
    keys = (
        "revision_id",
        "plane_uid",
        "target_uid",
        "z_mm",
        "beam_uid",
        "source_path",
        "locator",
        "conditions",
        "wavelength_nm",
        "definition",
        "unit",
        "tangential",
        "sagittal",
        "axes_aligned",
    )
    return {
        "scope": "all_case_revisions",
        "readings": [{"id": row.id, **{k: row.content.get(k) for k in keys}} for row in rows[:50]],
        "next_before_id": rows[49].id if len(rows) > 50 else None,
    }


def comparison_history(db, case_id, revision_id, before=None, *, kind="beam_comparison", all_versions=False):
    if revision_id is not None:
        read_project(db, case_id, revision_id)
    query = db.query(GeneratedContent).filter_by(case_id=case_id, content_type=kind)
    if not all_versions:
        scope = GeneratedContent.content["revision_id"].as_integer() == revision_id
        if kind == "beam_prediction_comparison":
            scope = scope | (GeneratedContent.content["reference_revision_id"].as_integer() == revision_id)
        query = query.filter(scope)
    if before:
        query = query.filter(GeneratedContent.id < before)
    rows = query.order_by(GeneratedContent.id.desc()).limit(26).all()
    return {
        "items": [{"id": row.id, **row.content} for row in rows[:25]],
        "next_before_id": rows[24].id if len(rows) > 25 else None,
    }


def _sample(revision, reading, source_mode, args, position, side=None, target_beam_uid=None):
    project = revision["project"]
    beam_uid = target_beam_uid or reading["beam_uid"]
    path = next((p for p in project.get("incident_paths", []) if p["uid"] == beam_uid), None)
    sample = {
        "revision_id": revision["revision_id"],
        "model_kind": revision.get("model_kind", "saved"),
        "title": project["title"],
        "beam_uid": beam_uid, "beam_name": path["name"] if path else "主光束" if beam_uid == "primary" else beam_uid,
        "status": "needs_input",
        "widths": {},
        "axis_notes": {},
    }
    if reading["beam_uid"] == beam_uid == "primary" and project["system"]["mode"] != source_mode:
        return {**sample, "message": "两个版本的主光束类型不同，请确认是否为同一条可比较的传播路径。"}
    paths = [path] if path else []
    if beam_uid != "primary" and not paths:
        return {**sample, "message": "原读数对应的入射支路在此版本中不存在。"}
    layout = EditableLayout(system=project["system"], incident_paths=[{**p, "power_budget": None} for p in paths])
    anchors = host_positions(layout.system)
    try:
        el, start, depth = detector_segment(anchors, reading["target_uid"], args.position_reference, position, side)
    except ValueError as exc:
        return {**sample, "message": str(exc)}
    plane = ObservationPlane(uid="detector-sample", name="实际测量截面", target_uid=el.uid, depth_mm=depth)
    result = analyze_sections(layout.model_copy(update={"observation_planes": [plane]}), revision["analysis"],
                              include_q=args.position_bound is not None)
    section = result["planes"][0]
    sample.update(z_mm=start + depth, target_uid=el.uid, element_name=el.name, depth_mm=depth)
    beam = next((b for b in section["beams"] if b["uid"] == beam_uid), None)
    if not beam:
        return {**sample, "message": "目标光束的已建模传播区间未覆盖探测器。"}
    compared = width_comparison(reading, beam)
    if args.position_bound:
        for axis, width in compared["widths"].items():
            if "predicted" in width:
                bound = getattr(args.width_bounds, axis, None)
                measured = None if bound is None else [max(0.0, reading[axis]-bound), reading[axis]+bound]
                width.update(apply_position_bound(layout, anchors, section, beam, axis,
                                                  args.position_reference, args.position_bound, width["predicted"], measured))
                if width.get("position_issue"):
                    compared["status"] = "partial"
        offsets = [width.get("compatible_offsets_mm") for width in compared["widths"].values()]
        if len(offsets) == 2 and all(item is not None for item in offsets):
            compared["common_offsets_mm"] = common_offsets(*offsets)
    return {**sample, **compared, "model_warnings": result.get("model_warnings", [])}


def agent_compare_reading(bind, case_id, args, revision_id, task_id):
    with Session(bind) as db:
        return compare_reading(db, case_id, args, revision_id, task_id=task_id)


def _training_overlap(content, original):
    """A matching archived cell proves reuse. Excluding it needs complete training provenance."""
    source, metadata = content.get("source"), content.get("metadata")
    if not isinstance(source, dict) or not isinstance(metadata, dict) or not isinstance(metadata.get("files"), list):
        return "unknown"
    original_digest = original.content.get("source_sha256")
    if not isinstance(original_digest, str) or not re.fullmatch("[0-9a-fA-F]{64}", original_digest):
        return "unknown"
    original_digest = original_digest.lower()
    digests = {}
    for item in metadata["files"]:
        if not isinstance(item, dict) or type(item.get("attachment_id")) is not int:
            return "unknown"
        aid, digest = item["attachment_id"], item.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch("[0-9a-fA-F]{64}", digest):
            continue
        if aid in digests and digests[aid] != digest.lower():
            return "unknown"
        digests[aid] = digest.lower()
    if source.get("kind") == "table":
        digest = digests.get(source["attachment_id"]) if type(source.get("attachment_id")) is int else None
        # The worksheet address of a transcribed reading can be free text.
        return "distinct" if digest and digest != original_digest else "unknown"
    points, expected = content.get("points"), source.get("reading_ids")
    if source.get("kind") != "registered" or not isinstance(points, list) or not points:
        return "unknown"
    locator = original.content.get("locator")
    traced = set()
    ambiguous = False
    for point in points:
        if not isinstance(point, dict) or type(point.get("reading_id")) is not int or type(point.get("attachment_id")) is not int:
            ambiguous = True
            continue
        digest = digests.get(point["attachment_id"])
        where = point.get("locator")
        if not digest or not isinstance(where, str) or not where.strip():
            ambiguous = True
            continue
        if digest == original_digest and isinstance(locator, str) and where.strip() == locator.strip():
            return "reused"
        # Distinct free-text labels can name the same instrument frame.
        if digest == original_digest or point["reading_id"] == original.id:
            ambiguous = True
        traced.add(point["reading_id"])
    duplicates = content.get("duplicate_readings", [])
    if isinstance(duplicates, list):
        traced.update(item["reading_id"] for item in duplicates if isinstance(item, dict)
                      and type(item.get("reading_id")) is int and type(item.get("counted_reading_id")) is int
                      and item["counted_reading_id"] in traced)
    if (ambiguous or not isinstance(expected, list) or not expected or
            not all(type(rid) is int and rid in traced for rid in expected)):
        return "unknown"
    return "distinct"


def _fit_overlap(db, case_id, target, uid, original, axis=None):
    project = target["project"]
    path = (project["system"].get("source") or {}) if uid == "primary" else next(
        (p for p in project.get("incident_paths", []) if p["uid"] == uid), {})
    reading = original.content
    known, uncertain = set(), set()
    for direction in ((axis,) if axis is not None else ("tangential", "sagittal")):
        state = path.get(direction) or path.get("tangential") or {}
        origin = state.get("fit_origin")
        if not origin or reading.get(direction) is None:
            continue
        fit_id = origin["study_id"]
        row = db.get(GeneratedContent, fit_id)
        if (not row or row.case_id != case_id or row.content_type != "beam_caustic_study" or
                not origin.get("study_key") or not isinstance(row.content, dict)
                or fit_evidence_key(row.content) != origin["study_key"]):
            uncertain.add(fit_id)
            continue
        overlap = _training_overlap(row.content, original)
        if overlap == "unknown":
            uncertain.add(fit_id)
        elif overlap == "reused":
            known.add(fit_id)
    return sorted(known), sorted(uncertain - known)


def model_data_use(db, case_id, revision, beam_uid, original, declared, axis=None):
    known, uncertain = _fit_overlap(db, case_id, revision, beam_uid, original, axis)
    role = declared
    if declared in {"held_out", "unspecified"}:
        if known:
            role = "model_input"
        elif uncertain:
            role = "unspecified"
    return {"data_role": role, "declared_data_role": declared, "fit_input_ids": known, "unverified_fit_ids": uncertain}


def _with_data_use(sample, evidence):
    sample = {**sample, "evidence": evidence}
    if evidence["data_role"] == "different_configuration":
        hidden = {"residual", "measured", "compatible_offsets_mm"}
        sample["widths"] = {axis: {k: v for k, v in width.items() if k not in hidden}
                            for axis, width in sample["widths"].items()}
        sample.pop("common_offsets_mm", None)
        sample["comparison_scope"] = "工况不同，仅列出模型的仪器预测。"
    return sample


def compare_reading(db, case_id, args, revision_id, *, task_id=None):
    original = db.get(GeneratedContent, args.reading_id)
    if not original or original.case_id != case_id or original.content_type != "beam_observation":
        raise ValueError("案例中找不到指定的光斑实测记录。")
    if args.revision_id and revision_id != args.revision_id:
        raise ValueError("目标版本不一致，请重新选择待检验的光路。")
    if revision_id is None:
        raise ValueError("请先选择一个工程版本或试算快照。")
    reading = original.content
    if args.width_bounds and any(
        getattr(args.width_bounds, axis) is not None and reading.get(axis) is None
        for axis in ("tangential", "sagittal")
    ):
        raise ValueError("测量误差界限需对应实际有读数的方向。")
    source = read_project(db, case_id, reading["revision_id"])
    target = read_project(db, case_id, revision_id)
    with case_original(db, case_id, reading["attachment_id"]) as (_, evidence):
        if evidence["sha256"] != reading["source_sha256"]:
            raise ValueError("原件与光斑登记时的校验值不同。")
    recipe = {**args.model_dump(exclude={"width_bounds"}), "revision_id": revision_id}
    if args.target_beam_uid in (None, reading["beam_uid"]):
        recipe.pop("target_beam_uid", None)
    if args.position_bound is None:
        recipe.pop("position_bound", None)
    if args.width_bounds:
        recipe["width_bounds"] = args.width_bounds.model_dump(exclude_none=True)
    target_uid = args.target_beam_uid or reading["beam_uid"]
    same_model = revision_id == reading["revision_id"] and target_uid == reading["beam_uid"]
    target_role = args.data_role
    if same_model and args.reference_data_role not in (None, "unspecified", target_role):
        if target_role != "unspecified":
            raise ValueError("参照与目标是同一版的同一光束，请为这条读数选择一致的用途。")
        target_role = args.reference_data_role
    target_use = model_data_use(db, case_id, target, target_uid, original, target_role)
    ref_use = target_use if same_model else model_data_use(
        db, case_id, source, reading["beam_uid"], original, args.reference_data_role or "unspecified")
    key = (
        "beam_compare:"
        + hashlib.sha256(
            json.dumps([2, recipe, evidence["sha256"], ref_use, target_use], ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
    )

    def previous():
        return db.query(GeneratedContent).filter_by(case_id=case_id, operation_key=key).one_or_none()

    prior = previous()
    if prior:
        return {"id": prior.id, **prior.content}
    plane = next((p for p in source["project"].get("observation_planes", []) if p["uid"] == reading["plane_uid"]), None)
    if not plane:
        raise ValueError("原版本中缺少这条读数的观察面。")
    position = args.position_mm
    if position is None:
        if args.position_reference == "fixed_z":
            position = reading["z_mm"]
        elif plane.get("depth_mm") is not None:
            position = plane["depth_mm"]
        elif reading.get("depth_mm") is not None:
            position = reading["depth_mm"]
        else:
            original_section, _ = registration_site(source, plane["uid"], reading["beam_uid"])
            position = original_section["depth_mm"]
    mode = source["project"]["system"]["mode"]
    side = plane.get("boundary_side") if args.position_reference == "fixed_z" else None
    original_model = _sample(source, reading, mode, args, position, side)
    candidate = original_model if same_model else _sample(target, reading, mode, args, position, side, target_uid)
    original_model = _with_data_use(original_model, ref_use)
    candidate = _with_data_use(candidate, target_use)
    intervals = {}
    if args.width_bounds:
        for axis in ("tangential", "sagittal"):
            bound = getattr(args.width_bounds, axis)
            if bound is None:
                continue
            measured = reading[axis]
            interval = {"lower": max(0.0, measured - bound), "upper": measured + bound}
            if not math.isfinite(interval["upper"]):
                raise ValueError("宽度和误差界限超出可表示范围，请核对数值与单位。")
            for label, prediction in (("reference", original_model), ("target", candidate)):
                width = prediction["widths"].get(axis, {})
                if "residual" not in width:
                    continue
                if args.position_bound is None:
                    interval[label + "_inside"] = interval["lower"] <= width["predicted"] <= interval["upper"]
                elif "compatible_offsets_mm" in width:
                    interval[label + "_compatible"] = bool(width["compatible_offsets_mm"])
            intervals[axis] = interval
    native = {
        k: reading.get(k)
        for k in ("wavelength_nm", "unit", "definition", "axes_aligned", "tangential", "sagittal", "beam_uid", "locator", "conditions")
    }
    data = {
        **recipe,
        "status": candidate["status"],
        "source_revision_id": source["revision_id"],
        "source_plane_uid": plane["uid"],
        "registered_z_mm": reading["z_mm"],
        "used_position_mm": position,
        "source": evidence,
        "reading": native,
        "reference_prediction": original_model,
        "target_prediction": candidate,
        "measurement_intervals": intervals,
    }
    known, uncertain = target_use["fit_input_ids"], target_use["unverified_fit_ids"]
    if known or uncertain:
        data["fit_input_ids"], data["unverified_fit_ids"] = known, uncertain
    if target_use["data_role"] != args.data_role:
        data.update(declared_data_role=args.data_role, data_role=target_use["data_role"])
    return store_comparison(db, case_id, "beam_comparison", "detector-width-v2", key, data, task_id)


def store_comparison(db, case_id, kind, version, key, data, task_id):
    if task_id is not None:
        state = db.query(AgentTask.status).filter_by(case_id=case_id, id=task_id).with_for_update().scalar()
        if state != "running":
            raise ValueError("任务已停止，本次光斑对照未写入。")
    row = GeneratedContent(
        case_id=case_id,
        content_type=kind,
        content=data,
        operation_key=key,
        prompt_version=version,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        prior = db.query(GeneratedContent).filter_by(case_id=case_id, operation_key=key).one_or_none()
        if prior:
            return {"id": prior.id, **prior.content}
        raise
    return {"id": row.id, **data}

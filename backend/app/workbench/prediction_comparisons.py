"""Compare fixed hypotheses on the same detector observable before taking a measurement."""

import hashlib
import json
import math
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import GeneratedContent
from ..solver_receipt import solver_cache_key, solver_receipt
from .authoring import EditableLayout, EditableSystem, EditablePath, analyze_editable
from .beam_comparisons import store_comparison
from .beam_readings import instrument_width
from .detector_position import DetectorPositionBound, apply_position_bound, clearance
from .observation import (
    FixedObservationPlane,
    ObservationPlane,
    analyze_sections,
    host_positions,
)
from .optics import StrictInput
from .projects import OpticalProject, read_project, read_project_inputs
from .study_inputs import WorkingDraft, capture_snapshot, snapshot_operation_key


class PredictionComparison(StrictInput):
    reference_revision_id: int = Field(gt=0, description="First saved model or study snapshot in this case.")
    revision_id: int | None = Field(
        default=None, gt=0, description="Second model. Defaults to the model bound to the question."
    )
    beam_uid: str = Field(default="primary", min_length=1, max_length=100)
    region: Literal["host", "incident_train"] = "host"
    axis: Literal["tangential", "sagittal"]
    definition: Literal["radius_1e2", "diameter_1e2", "fwhm_diameter", "d4sigma_diameter"]
    unit: Literal["um", "mm"]
    position_reference: Literal["fixed_z", "element_depth"] = "fixed_z"
    element_uid: str | None = Field(default=None, min_length=1, max_length=100)
    boundary_side: Literal["upstream", "downstream"] | None = None
    positions_mm: list[float] = Field(
        min_length=1,
        max_length=48,
        description="Experimentally accessible candidates. Fixed z uses the common unfolded main-path origin, including on the incident train.",
    )
    registration_note: str = Field(
        min_length=1,
        max_length=800,
        pattern=r"\S",
        description="Basis for common coordinates, accessible planes, detector axis, and unchanged measurement conditions.",
    )
    absolute_width_bound: float | None = Field(
        default=None,
        ge=0,
        description="Instrument absolute error bound per outcome in the requested width convention and unit. Omit if unknown.",
    )
    bound_source: str | None = Field(default=None, max_length=500)
    position_bound: DetectorPositionBound | None = None

    @model_validator(mode="after")
    def comparable_request(self):
        if len(set(self.positions_mm)) != len(self.positions_mm):
            raise ValueError("候选测点坐标重复。")
        if (self.position_reference == "element_depth") != bool(self.element_uid):
            raise ValueError("按元件深度比较时须指定定位元件，按公共坐标比较时无需指定。")
        if self.position_reference == "element_depth" and self.boundary_side:
            raise ValueError("元件深度已经指定了取样侧。")
        if self.region == "incident_train" and self.beam_uid == "primary":
            raise ValueError("接入前的测点需要选择具体入射支路。")
        if (self.absolute_width_bound is not None) != bool((self.bound_source or "").strip()):
            raise ValueError("测量绝对误差界限与其依据需同时提供。")
        return self


class DraftPredictionComparison(StrictInput):
    working_draft: WorkingDraft
    comparison: PredictionComparison


def compare_working_predictions(db, case_id, request, draft_key):
    if request.comparison.revision_id is not None:
        raise ValueError("草稿本身是待检验模型，请只指定参照版本。")
    read_project_inputs(db, case_id, [request.comparison.reference_revision_id])
    draft_key = snapshot_operation_key(db, case_id, request.working_draft, "width_draft_v2:", draft_key)
    query = db.query(GeneratedContent.id).filter_by(case_id=case_id, content_type="optical_snapshot", operation_key=draft_key)
    snapshot_id = query.scalar()
    if snapshot_id is None:
        try:
            snapshot = capture_snapshot(db, case_id, request.working_draft, "比较测点时的画布草稿", operation_key=draft_key)
            snapshot_id = snapshot["revision_id"]
        except IntegrityError:
            db.rollback()
            snapshot_id = query.scalar()
            if snapshot_id is None:
                raise
    return compare_predictions(db, case_id, request.comparison, snapshot_id)


def _sample_model(revision, args):
    project = revision["project"]
    system = EditableSystem.model_validate(project["system"])
    analysis = revision["analysis"]
    sample_uid = args.beam_uid
    path = next((p for p in project.get("incident_paths", []) if p["uid"] == sample_uid), None)
    model = {
        "revision_id": revision["revision_id"],
        "kind": revision.get("model_kind", "saved"),
        **({"saved_analysis_solver_receipt": analysis["solver_receipt"]} if analysis.get("solver_receipt") else {}),
        **({"variant_name": revision["variant_name"]} if revision.get("variant_name") else {}),
        "title": project["title"],
        "mode": system.mode,
        "beam_name": path["name"] if path else ("主光束" if sample_uid == "primary" else sample_uid),
        "wavelength_nm": system.wavelength_nm if sample_uid == "primary" else path and path.get("wavelength_nm"),
    }
    if sample_uid != "primary" and not path:
        return model, [{"issue": "此版本中找不到所选入射支路。"}] * len(args.positions_mm)
    if not system.elements:
        return model, [{"issue": "此版本没有可定位该测点的顺序光路。"}] * len(args.positions_mm)
    origin = 0.0
    paths = [{**path, "power_budget": None}] if path else []
    if args.region == "incident_train":
        arm = EditablePath.model_validate(path)
        entrance = host_positions(system).get(arm.target_uid)
        length = sum(el.length_mm for el in arm.elements)
        if not entrance or (not arm.through_uid and entrance[1].kind != "space") or length <= 0:
            return model, [{"issue": "入射段缺少可定位的入口或有长度的传播段。"}] * len(args.positions_mm)
        origin = entrance[2] - length
        system = EditableSystem(mode="single_pass", wavelength_nm=arm.wavelength_nm, elements=arm.elements,
                                source=arm.model_dump(include={"tangential", "sagittal", "launch_ray"}))
        analysis = analyze_editable(EditableLayout(system=system), samples_per_segment=2)
        paths, sample_uid = [], "primary"

    planes, samples = [], [{} for _ in args.positions_mm]
    for i, pos in enumerate(args.positions_mm):
        names = {"uid": str(i), "name": "候选测点"}
        if args.position_reference == "fixed_z" and pos >= origin:
            planes.append(
                FixedObservationPlane(
                    **names, position_reference="fixed_z", z_mm=pos - origin, boundary_side=args.boundary_side
                )
            )
        elif args.position_reference == "element_depth" and 0 <= pos <= 1e6:
            planes.append(ObservationPlane(**names, target_uid=args.element_uid, depth_mm=pos))
        else:
            samples[i]["issue"] = "坐标在所选光路区段之外。"
    layout = EditableLayout(system=system.model_dump(), incident_paths=paths, observation_planes=planes)
    result = analyze_sections(layout, analysis, include_q=args.position_bound is not None)
    model["warnings"] = list(dict.fromkeys([*analysis.get("model_warnings", []), *result.get("model_warnings", [])]))
    anchors = host_positions(layout.system)
    for section in result["planes"]:
        sample = samples[int(section["uid"])]
        beam = next((b for b in section["beams"] if b["uid"] == sample_uid), None)
        if beam is None:
            sample["issue"] = section.get("message", "所选光束的已建模区间未覆盖这个测点。")
            continue
        width, issue = instrument_width(beam, args.axis, args.definition, args.unit)
        sample.update(
            z_mm=origin + section["z_mm"],
            element=anchors[section["target_uid"]][1].name,
            element_uid=section["target_uid"],
            depth_mm=section["depth_mm"],
            medium_n=beam.get("index"),
        )
        if issue:
            sample["issue"] = issue
            if beam.get("radius_origin", {}).get(args.axis) == "source_input":
                radius = beam["radii_mm"][args.axis]
                sample.update(supplied_radius_mm=radius, width_basis=beam["width_basis"][args.axis],
                              issue=f"源面登记半径为 {radius:g} mm。{issue}")
        else:
            sample["width"] = width
            if args.position_bound:
                sample.update(apply_position_bound(layout, anchors, section, beam, args.axis,
                                                   args.position_reference, args.position_bound, width))
    return model, samples


def _pair(position, first, second, issue, bound):
    row = {"position_mm": position, "reference": first, "target": second}
    if bound is not None:
        for sample in (first, second):
            if "width" in sample and not sample.get("position_issue"):
                lower, upper = sample.get("position_width_range", [sample["width"], sample["width"]])
                if not math.isfinite(upper + bound):
                    raise ValueError("误差区间超出可表示范围，请核对单位。")
                sample["reading_interval"] = [max(0.0, lower - bound), upper + bound]
    if issue or "width" not in first or "width" not in second:
        return {**row, "issue": issue or first.get("issue") or second["issue"]}
    gap = abs(first["width"] - second["width"])
    if first["medium_n"] != second["medium_n"]:
        row["sampling_note"] = "两版在此处的折射率不同，需核对实际取样介质和成像条件。"
    row["difference"] = gap
    row["numerically_resolved"] = gap > 32 * max(math.ulp(first["width"]), math.ulp(second["width"]))
    if first.get("position_issue") or second.get("position_issue"):
        return row
    if bound is not None:
        row["clearance"] = clearance(first["reading_interval"], second["reading_interval"])
        row["disjoint"] = row["clearance"] > 0
    return row


def compare_predictions(db, case_id, args, revision_id, task_id=None):
    if not revision_id or (args.revision_id is not None and args.revision_id != revision_id):
        raise ValueError("请选择与本次比较一致的目标版本。")
    if args.reference_revision_id == revision_id:
        raise ValueError("请选择两个不同的工程版本或快照。")
    recipe = args.model_copy(update={"revision_id": revision_id})
    models = read_project_inputs(db, case_id, [args.reference_revision_id, revision_id])
    receipt = solver_receipt("compare_beam_predictions", "BeamWeft", "1", compute=_sample_model,
                             inputs=[OpticalProject.model_validate(r["project"]) for r in models.values()])
    identity = [recipe.model_dump(mode="json"), solver_cache_key(receipt)]
    key = "width_pair_v2:" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    prior = db.query(GeneratedContent).filter_by(case_id=case_id, operation_key=key).one_or_none()
    if prior:
        return {"id": prior.id, **prior.content}
    first, second = [read_project(db, case_id, rid) for rid in (args.reference_revision_id, revision_id)]
    a, left = _sample_model(first, args)
    b, right = _sample_model(second, args)
    issue = None
    if args.beam_uid == "primary" and a["mode"] != b["mode"]:
        issue = "两版主光束类型不同，无法确认为同一可比较的量。"
    elif (
        a["wavelength_nm"]
        and b["wavelength_nm"]
        and not math.isclose(a["wavelength_nm"], b["wavelength_nm"], rel_tol=1e-9)
    ):
        issue = "两版所选光束的波长不同。"
    rows = [_pair(pos, x, y, issue, args.absolute_width_bound) for pos, x, y in zip(args.positions_mm, left, right)]
    valid = [r for r in rows if "difference" in r]
    resolved = [r for r in valid if r["numerically_resolved"]]
    largest = max((r["difference"] for r in resolved), default=0.0 if valid else None)
    best = [r["position_mm"] for r in resolved if math.isclose(r["difference"], largest, rel_tol=1e-10)]
    scope = "仅比较给定测点和两版固定参数的预测。测量区间只计入所填绝对误差，未传播模型参数及定位误差。"
    incomplete = any(p.get("position_issue") for row in valid for p in (row["reference"], row["target"]))
    if args.position_bound:
        scope = "两版参数固定。在所给轴向定位范围内计算每版可能的宽度，包含区间内的束腰。宽度读数误差按所填界限另计。"
    if args.beam_uid == "primary" and any(model["mode"] != "single_pass" for model in (a, b)):
        scope += "腔内宽度须通过能保留该腔模的取样口或经标定的成像测量核对。"
    data = {
        **recipe.model_dump(),
        "solver_receipt": receipt,
        "models": [a, b],
        "points": rows,
        "largest_difference": largest,
        "largest_at_mm": best,
        "status": "needs_input" if not valid else "completed" if len(valid) == len(rows) and not incomplete else "partial",
        "scope": scope,
    }
    if args.position_bound and args.absolute_width_bound is not None:
        margin = max((r["clearance"] for r in rows if "clearance" in r), default=None)
        data["maximum_clearance"] = margin
        data["separated_at_mm"] = [r["position_mm"] for r in rows if r.get("disjoint") and
                                   math.isclose(r["clearance"], margin, rel_tol=1e-10)]
    return store_comparison(
        db, case_id, "beam_prediction_comparison", "beam-prediction-contrast-v1", key, data, task_id
    )


def agent_compare_predictions(bind, case_id, args, revision_id, task_id):
    with Session(bind) as db:
        return compare_predictions(db, case_id, args, revision_id, task_id)

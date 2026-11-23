"""Read a frozen detector forecast in the coordinates and width convention of an archived observation."""
from hashlib import sha256
import json
import math

from pydantic import Field
from sqlalchemy.orm import Session

from ..models import Attachment, GeneratedContent
from ..study_api import StrictInput
from .authoring import EditablePath, EditableSystem
from .beam_comparisons import DataRole, model_data_use, store_comparison
from .beam_readings import registration_site
from .measurement_sources import case_original
from .observation import host_positions, host_route
from .projects import read_project, read_project_inputs


def agent_check_prediction(bind, case_id, args, task_id):
    with Session(bind) as db:
        return check_prediction(db, case_id, args, task_id)


class PredictionCheck(StrictInput):
    prediction_id: int = Field(gt=0, description="Saved comparison of detector predictions in this project.")
    reading_id: int = Field(gt=0, description="Beam observation transcribed from an archived original.")
    registration_note: str = Field(min_length=1, max_length=800, pattern=r"\S",
        description="Evidence that the beam, detector datum, axes, and acquisition conditions correspond.")
    data_role: DataRole = Field(default="unspecified", description="Declared relationship of this observation to the two models.")
    model_roles: dict[int, DataRole] = Field(default_factory=dict, max_length=2,
        description="Optional separate declarations keyed by the two model revision IDs, e.g. an input for one and held out for the other.")


def _same(a, b):
    # Permit round-off from unit conversion or adding a detector depth to its datum.
    return a is not None and b is not None and abs(a-b) <= 4 * max(math.ulp(a), math.ulp(b))


def _measured_width(reading, prediction):
    raw = reading.get(prediction["axis"])
    if raw is None:
        return None, "原件没有所选仪器方向的读数。"
    units = {"um": 1., "mm": 1000.}
    if reading["unit"] not in units or prediction["unit"] not in units:
        return None, "无法确认两份记录的长度单位。"
    old, new = reading["definition"], prediction["definition"]
    diameter = {"radius_1e2": 2., "diameter_1e2": 1.}
    if old != new and not (old in diameter and new in diameter):
        return None, "实测与预测的宽度定义不同，请提供相同定义的读数。"
    scale = units[reading["unit"]] / units[prediction["unit"]]
    if old != new:
        scale *= diameter[old] / diameter[new]
    value = raw * scale
    if not math.isfinite(value):
        return None, "宽度换算超出有限数值范围，请核对读数及单位。"
    return value, ""


def _face(project, site):
    element = next((e for e in project["system"]["elements"] if e.get("uid") == site.get("target_uid")), None)
    if element and element.get("length_mm", 0) > 0:
        if _same(site.get("depth_mm"), 0.):
            return "downstream"
        if _same(site.get("depth_mm"), element["length_mm"]):
            return "upstream"
    return None


def _registered_index(project, site, beam, uid):
    if beam.get("index") is not None:
        return beam["index"]
    # Older geometry-only registrations can lack a sampled field entirely.
    system = EditableSystem.model_validate(project["system"])
    anchors = host_positions(system)
    if uid == "primary":
        anchor = anchors.get(site["target_uid"])
        return anchor[1].n if anchor else None
    raw_path = next((p for p in project.get("incident_paths", []) if p["uid"] == uid), None)
    if raw_path is None:
        return None
    route, _ = host_route(EditablePath.model_validate(raw_path), anchors, system.wavelength_nm)
    return next((n for element, _, n in route if element.uid == site["target_uid"]), None)


def _find_station(prediction, reading, site):
    depth = prediction["position_reference"] == "element_depth"
    pos = site["depth_mm" if depth else "z_mm"]
    if prediction["region"] != "host":
        return pos, None, "这条读数登记在主光路观察面，无法据此核对接入前支路的测点。"
    if depth and site["target_uid"] != prediction["element_uid"]:
        return pos, None, "实测深度与预测深度使用了不同的定位元件。"
    if reading["beam_uid"] != prediction["beam_uid"]:
        return pos, None, "实测与预测登记了不同的光束。"
    matches = [point for point in prediction["points"] if _same(pos, point["position_mm"])]
    if not matches:
        return pos, None, "冻结的预测在此登记坐标没有采样。可将这个测点加入新的比较，作为测量后的补算。"
    if len(matches) > 1:
        return pos, None, "冻结的预测在此登记坐标有多个可能测点。请核对坐标值、单位和对应测点。"
    return pos, matches[0], ""


def _score(sample, issue, measured, role):
    predicted = sample.get("width")
    interval = sample.get("reading_interval")
    result = {"state": "unknown", "predicted": predicted, "interval": interval, "residual": None}
    if role == "different_configuration":
        issue = "已声明该模型对应不同实验配置，仅供查看其原预测。"
    if issue or predicted is None or measured is None:
        return {**result, "reason": issue or sample.get("issue") or "缺少可核对的预测或实测。"}
    result["residual"] = measured - predicted
    if sample.get("position_issue") or interval is None:
        return {**result, "reason": sample.get("position_issue") or "原预测没有带依据的绝对读数界限，仅报告实测减预测。"}
    inside = ((measured >= interval[0] or _same(measured, interval[0]))
              and (measured <= interval[1] or _same(measured, interval[1])))
    return {**result, "state": "inside" if inside else "outside",
            "reason": "读数在冻结预测的允许区间内。" if inside else "读数在冻结预测的允许区间外。"}


def _model_issue(prediction, reading, reading_project, model, project, sample, site, index):
    if reading["beam_uid"] == "primary" and reading_project["system"]["mode"] != model["mode"]:
        return "实测登记的主光束与该模型的光路类型不符。"
    if not model.get("wavelength_nm") or not reading.get("wavelength_nm"):
        return "缺少可确认的模型或仪器波长。"
    if not math.isclose(reading["wavelength_nm"], model["wavelength_nm"], rel_tol=1e-9):
        return "仪器波长与该模型的预测波长不符。"
    if not reading.get("axes_aligned"):
        return "仪器方向尚未与模型横向轴配准。"
    if "width" not in sample:
        return sample.get("issue") or "冻结比较缺少这一版在所选测点的预测。"
    if not _same(index, sample.get("medium_n")):
        return "读数登记处与预测处的取样介质未能核对一致。"
    if prediction["position_reference"] == "element_depth" and site["target_uid"] != sample.get("element_uid"):
        return "该预测的实际取样元件与读数的定位元件不同。"
    expected_face = _face(project, {"target_uid": sample.get("element_uid"), "depth_mm": sample.get("depth_mm")})
    observed_face = _face(reading_project, site)
    if expected_face and observed_face and expected_face != observed_face:
        return "仪器与预测分别取样于界面的两侧。"
    return ""


def check_prediction(db, case_id, args, task_id=None):
    records = db.query(GeneratedContent).filter_by(case_id=case_id)
    forecast = records.filter_by(id=args.prediction_id, content_type="beam_prediction_comparison").one_or_none()
    observed = records.filter_by(id=args.reading_id, content_type="beam_observation").one_or_none()
    if forecast is None or observed is None:
        raise ValueError("本工程中找不到指定的冻结预测比较或实测登记。")
    prediction, reading = forecast.content, observed.content
    ids = (prediction["reference_revision_id"], prediction["revision_id"])
    if set(args.model_roles) - set(ids):
        raise ValueError("实测数据用途只可指定给这份比较中的两个工程版本。")
    with case_original(db, case_id, reading["attachment_id"]) as (_, evidence):
        if evidence["sha256"] != reading["source_sha256"]:
            raise ValueError("原件字节与登记读数时的校验值不同，请核对原件。")
    registered = read_project(db, case_id, reading["revision_id"])
    site, beam = registration_site(registered, reading["plane_uid"], reading["beam_uid"])
    index = _registered_index(registered["project"], site, beam, reading["beam_uid"])
    position, point, issue = _find_station(prediction, reading, site)
    if (reading.get("target_uid") and site["target_uid"] != reading["target_uid"]) or any(
            key in reading and not _same(site[key], reading[key]) for key in ("depth_mm", "z_mm")):
        issue = "读数登记坐标与其来源版本的观察面不一致，请核对原记录。"
    measured, width_issue = _measured_width(reading, prediction)
    revisions = read_project_inputs(db, case_id, ids)
    models = {m["revision_id"]: m for m in prediction["models"]}
    outcomes = []
    for key, rid in zip(("reference", "target"), ids):
        model = models[rid]
        revision = revisions[rid]
        sample = point[key] if point else {}
        role = args.model_roles.get(rid, args.data_role)
        use = model_data_use(db, case_id, revision, prediction["beam_uid"], observed, role, axis=prediction["axis"])
        reason = issue or width_issue or _model_issue(prediction, reading, registered["project"], model,
                                                      revision["project"], sample, site, index)
        outcomes.append({"revision_id": rid, "title": model["title"], "evidence": use, "medium_n": sample.get("medium_n"),
                         **_score(sample, reason, measured, role)})
    states = [row["state"] for row in outcomes]
    status = "completed" if "unknown" not in states else "partial" if any(row["residual"] is not None for row in outcomes) else "needs_input"
    attachment = db.get(Attachment, evidence["attachment_id"])
    chronology = {"prediction_saved_at": forecast.generated_at, "original_uploaded_at": attachment.uploaded_at,
                  "reading_registered_at": observed.generated_at, "acquired_at": None}
    native = {key: reading.get(key) for key in ("revision_id", "plane_uid", "target_uid", "z_mm", "depth_mm", "beam_uid",
              "wavelength_nm", "axes_aligned", "definition", "unit", "tangential", "sagittal", "locator", "conditions")}
    if reading.get("transcribed_by"):
        native["transcribed_by"] = reading["transcribed_by"]
    plan = {key: prediction.get(key) for key in ("beam_uid", "region", "element_uid", "boundary_side",
            "registration_note", "absolute_width_bound", "bound_source", "position_bound")}
    plan["solver_sha256"] = (prediction.get("solver_receipt") or {}).get("sha256")
    data = {**args.model_dump(mode="json"), "revision_id": ids[1], "reference_revision_id": ids[0],
            "axis": prediction["axis"], "definition": prediction["definition"], "unit": prediction["unit"],
            "position_mm": position, "position_reference": prediction["position_reference"],
            "prediction_conditions": plan, "registered_medium_n": index,
            "reading": native, "measured": measured, "source": evidence, "outcomes": outcomes, "status": status,
            "chronology": {k: v.isoformat() if v is not None else None for k, v in chronology.items()}}
    digest = sha256(json.dumps(data, sort_keys=True, allow_nan=False, ensure_ascii=False).encode()).hexdigest()
    operation_key = "predict-check:" + digest
    prior = records.filter_by(operation_key=operation_key).one_or_none()
    if prior:
        return {"id": prior.id, **prior.content}
    return store_comparison(db, case_id, "prediction_check", "frozen-width-check-v1", operation_key, data, task_id)

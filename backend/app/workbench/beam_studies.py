"""Case-scoped beam propagation studies from archived tables or versioned observations."""
import hashlib
import json
import math
from typing import Annotated, Literal

import numpy as np
from pydantic import Field, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import AgentTask, Attachment, GeneratedContent
from ..version import APP_NAME
from .measurement_sources import TableRange, read_numeric_cells
from .beam_predictions import compare_to_fit, observation_keys, predictions
from .beam_holdout import HoldoutStations, assess_holdout, partition_stations
from ..physics.caustic import UnresolvedCaustic, fit_caustic
from .optics import StrictInput
from .projects import read_project
from .authoring import EditablePath, EditableSystem
from .observation import host_positions
from .shared_media import resolved_host_route

METHOD_VERSION = 4

class TableSeries(TableRange):
    kind: Literal["table"]
    position_column: int = Field(ge=1, le=16384)
    width_column: int = Field(ge=1, le=16384)
    acquisition_column: int | None = Field(default=None, ge=1, le=16384, exclude_if=lambda value: value is None,
        description="Original column identifying a physical acquisition. All positions linked by one ID stay on the same side of a holdout.")
    decimal_mark: Literal[".", ","] = Field(default=".", exclude_if=lambda value: value == ".")
    z_unit: Literal["mm", "cm", "um", "m"]
    width_unit: Literal["mm", "um"]
    width_definition: Literal["radius_1e2", "diameter_1e2", "d4sigma_diameter"]
    wavelength_nm: float | None = Field(default=None, gt=0, le=1e6, description="Vacuum wavelength for M². Geometry can be fitted without it.")
    medium_n: float | None = Field(default=None, ge=1, le=5, description="Refractive index at the measured wavelength for M².")
    axis: Literal["T", "S", "X", "Y"]
    reference: str = Field(min_length=1, max_length=500, description="Physical axial coordinate origin and direction used in the file.")
    conditions: str = Field(min_length=1, max_length=500)
    homogeneous_interval: Literal[True] = Field(description="User or source confirms these readings share a beam, run, transverse axis and an uninterrupted homogeneous interval.")

    @model_validator(mode="after")
    def check_mapping(self):
        if self.position_column == self.width_column:
            raise ValueError("位置与宽度须选择两列。")
        if self.acquisition_column in {self.position_column, self.width_column}:
            raise ValueError("采集标识、位置和宽度须选择不同的列。")
        if not self.reference.strip() or not self.conditions.strip():
            raise ValueError("请填写轴向坐标基准和实验工况。")
        return self


class RegisteredSeries(StrictInput):
    kind: Literal["registered"]
    revision_id: int = Field(gt=0)
    axis: Literal["tangential", "sagittal"]
    reading_ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=3000)


class FitReference(StrictInput):
    study_id: int = Field(gt=0)
    note: str = Field(min_length=1, max_length=800, pattern=r"\S", description="Why these coordinates, axes and run conditions correspond to the same uninterrupted physical beam.")
    z_offset_mm: float = Field(default=0, description="Coordinate in the original fit = new measurement position plus this offset (mm), same positive direction.")


class PredictionPositions(StrictInput):
    positions_mm: list[float] = Field(min_length=1, max_length=300)


class ForecastRequest(PredictionPositions):
    kind: Literal["prediction"]
    study_id: int = Field(gt=0)


class BeamStudy(StrictInput):
    source: TableSeries | RegisteredSeries | ForecastRequest = Field(discriminator="kind")
    reference_fit: FitReference | None = Field(default=None, description="Evaluate readings against a saved fit or saved forecast set. Omit to fit measurements or save a forecast.")
    holdout: HoldoutStations | None = Field(default=None, description="Reserve entire original stations before fitting. Repeated positions and known acquisition groups never split across fit and validation.")

    @model_validator(mode="after")
    def holdout_only_for_new_fit(self):
        if self.holdout and (self.reference_fit or isinstance(self.source, ForecastRequest)):
            raise ValueError("留出位置用于从测量原件新建拟合。对照既有拟合或预测时请使用 reference_fit。")
        return self

class StudyHistory(StrictInput):
    study_id: int | None = Field(default=None, gt=0)
    positions_mm: list[float] | None = Field(default=None, min_length=1, max_length=300, description="Predict at these positions in the named fit's original longitudinal frame and medium.")

    @model_validator(mode="after")
    def named_fit(self):
        if self.positions_mm is not None and self.study_id is None:
            raise ValueError("预测需要指定之前保存的拟合编号。")
        return self


def _from_table(db, case_id, source):
    rows, evidence = read_numeric_cells(db, case_id, source, [source.position_column, source.width_column],
                                        source.decimal_mark, identifier_column=source.acquisition_column)
    z_scale = {"mm": 1, "cm": 10, "um": .001, "m": 1000}[source.z_unit]
    w_scale = (1 if source.width_unit == "mm" else .001) / (1 if source.width_definition == "radius_1e2" else 2)
    points = []
    for index, cells in rows:
        raw_z, raw_w = cells[:2]
        z, w = raw_z * z_scale, raw_w * w_scale
        if not math.isfinite(z) or not math.isfinite(w) or w <= 0:
            raise ValueError(f"第 {index} 行的位置或宽度无效，请核对列、选区和单元格。")
        points.append({"row":index, "raw_position":raw_z, "raw_width":raw_w, "z_mm":z, "radius_mm":w,
                       **({"acquisition_id": cells[2]} if source.acquisition_column else {})})
    return points, {"axis": source.axis, "reference": source.reference, "conditions": source.conditions,
                    "wavelength_nm": source.wavelength_nm, "medium_n": source.medium_n,
                    "width_basis": "second_moment" if source.width_definition == "d4sigma_diameter" else "gaussian_equivalent",
                    "files": [evidence]}


def _from_registered(db, case_id, source):
    revision = read_project(db, case_id, source.revision_id)
    if not revision:
        raise ValueError("当前案例找不到所选的工程版本。")
    project = revision["project"]
    ids = set(source.reading_ids)
    saved = db.query(GeneratedContent).filter_by(case_id=case_id, content_type="beam_observation").filter(
        GeneratedContent.id.in_(ids)).order_by(GeneratedContent.id).all()
    if len(saved) != len(ids) or any(r.content["revision_id"] != source.revision_id for r in saved):
        raise ValueError("所选记录须属于同一案例和工程版本。")
    first = saved[0].content
    points, files, segments = [], {}, set()
    wavelengths = {r.content["wavelength_nm"] for r in saved}
    if len(wavelengths - {None}) > 1:
        raise ValueError("请选择同一实测波长的记录。")
    wavelength = first["wavelength_nm"] if None not in wavelengths else None
    for record in saved:
        r = record.content
        if (r["beam_uid"], r["conditions"]) != (first["beam_uid"], first["conditions"]):
            raise ValueError("请选择同一光束、实测波长和实验工况的记录。")
        if not r["axes_aligned"] or source.axis not in r["radii_mm"]:
            raise ValueError(f"记录 #{record.id} 缺少方向配准或可换算的宽度。")
        if (r["definition"] == "d4sigma_diameter") != (first["definition"] == "d4sigma_diameter"):
            raise ValueError("二阶矩直径与 1/e² 宽度需要分开拟合。")
        segments.add(r["target_uid"])
        files[r["attachment_id"]] = {"attachment_id": r["attachment_id"], "path": r["source_path"], "sha256": r["source_sha256"]}
        point = {"reading_id": record.id, "plane_uid": r["plane_uid"], "attachment_id": r["attachment_id"], "locator": r["locator"],
                 "raw_width": r[source.axis], "width_unit": r["unit"], "definition": r["definition"],
                 "z_mm": r["z_mm"], "radius_mm": r["radii_mm"][source.axis]}
        if source.axis in r["measured_minus_model_mm"]:
            point["model_radius_mm"] = r["prediction_mm"][source.axis]
        points.append(point)
    elements = project["system"]["elements"]
    indices = [i for i, e in enumerate(elements) if e["uid"] in segments]
    if len(indices) != len(segments):
        raise ValueError("工程版本中缺少测量所在的介质段。")
    interval = elements[min(indices):max(indices)+1]
    if any(e["kind"] != "space" for e in interval):
        raise ValueError("所选位置之间存在光学元件，请在同一均匀传播区间拟合。")
    if first["beam_uid"] == "primary":
        indices_n = {e["n"] for e in interval}
        model_wavelength = project["system"]["wavelength_nm"]
    else:
        path = next((p for p in project["incident_paths"] if p["uid"] == first["beam_uid"]), None)
        if not path:
            raise ValueError("工程版本中缺少这条入射支路。")
        model_wavelength = path["wavelength_nm"]
        sections = (revision.get("analysis") or {}).get("sections") or {}
        frozen = next((r.get("shared_route") for r in sections.get("paths", []) if r["uid"] == path["uid"]), None)
        if frozen is not None:
            index_by_uid = {step["element_uid"]:step["n"] for step in frozen}
        else:
            # Pre-route receipts only represented independent values and equal-wavelength host scalars.
            system = EditableSystem.model_validate(project["system"])
            route, _ = resolved_host_route(EditablePath.model_validate(path),host_positions(system),system.wavelength_nm)
            index_by_uid = {el.uid:(index["n"] if index["index_source"] in {"branch_value","host_same_wavelength"}
                                     else None) for el,_,index in route}
            for plane in sections.get("planes", []):
                for beam in plane.get("beams", []):
                    if beam["uid"] == path["uid"] and plane.get("target_uid") in index_by_uid and "index" in beam:
                        index_by_uid[plane["target_uid"]] = beam["index"]
        if any(el["uid"] not in index_by_uid for el in interval):
            raise ValueError("测点未落在同一条连续支路上。")
        indices_n = {index_by_uid[el["uid"]] for el in interval}
    same_spectrum = wavelength is not None and model_wavelength is not None and math.isclose(wavelength, model_wavelength, rel_tol=1e-9)
    if len(interval) > 1 and (not same_spectrum or len(indices_n) != 1 or None in indices_n):
        raise ValueError("跨段拟合需要确认各段在本次实测波长下具有相同的折射率。也可在单个均匀段内拟合。")
    n = indices_n.pop() if same_spectrum else None
    start = sum(e["length_mm"] for e in elements[:min(indices)])
    bounds = [start, start + sum(e["length_mm"] for e in interval)]
    return points, {"axis": "T" if source.axis == "tangential" else "S", "reference": f"工程 #{source.revision_id} 展开坐标",
                    "interval_mm": bounds, "segment_uids": [e["uid"] for e in interval],
                    "beam_uid": first["beam_uid"], "conditions": first["conditions"], "wavelength_nm": wavelength, "medium_n": n,
                    "width_basis": "second_moment" if first["definition"] == "d4sigma_diameter" else "gaussian_equivalent",
                    "files": list(files.values())}


def _compare_to_project(points, metadata, fit):
    if any("model_radius_mm" not in point for point in points):
        return {"message": "部分位置缺少同口径的版本模型值。仅报告独立测量拟合。"}
    z, groups, counts = np.unique([p["z_mm"] for p in points], return_inverse=True, return_counts=True)
    model = np.bincount(groups, weights=[p["model_radius_mm"] for p in points]) / counts
    measured = np.bincount(groups, weights=[p["radius_mm"] for p in points]) / counts
    try:
        baseline = fit_caustic(z, model, metadata["wavelength_nm"], metadata["medium_n"])
    except ValueError:
        return {"message": "该区间内模型曲率过小，无法从模型截面稳定重建束腰。"}
    def profile(at):
        return np.hypot(baseline["waist_radius_mm"], baseline["divergence_half_angle_rad"] * (at-baseline["waist_z_mm"]))
    def rms(values):
        return float(np.linalg.norm(values) / math.sqrt(len(values)))
    scale = float(measured @ model / (model @ model))
    shift = baseline["waist_z_mm"] - fit["waist_z_mm"]
    result = {"model": {k:baseline[k] for k in ("waist_radius_mm","waist_z_mm","divergence_half_angle_rad")},
              "nominal_rms_mm": rms(measured-model), "width_scale": scale, "scale_rms_mm": rms(measured-scale*model),
              "aligned_waist_shift_mm": shift}
    low, high = metadata["interval_mm"]
    tolerance = 1e-9 * max(1, abs(low), abs(high))
    if z[0]+shift < low-tolerance or z[-1]+shift > high+tolerance:
        result["message"] = "若仅靠移动坐标零点来对齐两个束腰，部分原测点将越过该均匀区间。"
        return result
    result["shift_rms_mm"] = rms(measured-profile(z+shift))
    if not fit["bracketed"] or fit["valid_omissions"] < fit["tested_omissions"]:
        result["message"] = "测量对束腰位置的约束仍弱，补足采样后再比较两个解释。"
        return result
    # Use unsampled midpoints inside the common valid span. No extrapolation through optics.
    low, high = max(low,low-shift), min(high,high-shift)
    grid = np.unique(np.r_[low, z[(z>=low)&(z<=high)], high])
    candidates = (grid[:-1]+grid[1:])/2
    if len(candidates):
        differences = np.abs(scale*profile(candidates)-profile(candidates+shift))
        best = int(np.argmax(differences))
        if differences[best] > 1e-9 * max(model):
            result["next_plane"] = {"z_mm": float(candidates[best]), "scale_radius_mm": float(scale*profile(candidates[best])),
                                    "shift_radius_mm": float(profile(candidates[best]+shift)), "difference_mm": float(differences[best])}
    return result


def run_agent_beam_study(bind, case_id, task_id, args):
    with Session(bind) as db:
        return run_beam_study(db, case_id, args, task_id=task_id)


def run_beam_study(db, case_id, args, *, task_id=None):
    source = args.source
    if isinstance(source, ForecastRequest):
        if args.reference_fit:
            raise ValueError("保存预测时只需指定一份拟合与预测位置。")
        key = "forecast:" + hashlib.sha256(json.dumps([source.study_id, source.positions_mm]).encode()).hexdigest()
        prior = db.query(GeneratedContent).filter_by(case_id=case_id, operation_key=key).one_or_none()
        if prior:
            return _record(prior)
        forecast = beam_study_history(db, case_id, source.study_id, source.positions_mm)
        archive = db.query(Attachment.id, Attachment.content_hash).filter_by(case_id=case_id).order_by(Attachment.id).all()
        forecast["archive_snapshot"] = [{"attachment_id": i, "sha256": sha} for i, sha in archive]
        forecast.update(reference_study_id=forecast.pop("id"), source=source.model_dump(), method_version=METHOD_VERSION)
        return _save_study(db, case_id, key, "beam_caustic_prediction", forecast, task_id)
    if isinstance(source, RegisteredSeries):
        source = source.model_copy(update={"reading_ids": sorted(set(source.reading_ids))})
        points, metadata = _from_registered(db, case_id, source)
    else:
        points, metadata = _from_table(db, case_id, source)
    recipe = source.model_dump()
    identity = [METHOD_VERSION, recipe, metadata["files"]]
    if args.reference_fit:
        identity.append(args.reference_fit.model_dump())
    if args.holdout:
        identity.append({"station_holdout_v1": args.holdout.model_dump()})
    key = "caustic:" + hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    def existing():
        return db.query(GeneratedContent).filter_by(case_id=case_id, operation_key=key).one_or_none()
    previous = existing()
    if previous:
        return _record(previous)
    content = {"method_version": METHOD_VERSION, "source": recipe, "metadata": metadata, "points": points}
    if args.reference_fit:
        reference = beam_study_history(db, case_id, args.reference_fit.study_id)
        forecast = reference if "predictions" in reference else None
        if forecast:
            reference = beam_study_history(db, case_id, forecast["reference_study_id"])
        if not reference.get("fit"):
            raise ValueError("请选择已保存的拟合，数据对照记录没有可复用的拟合参数。")
        if reference["source"]["kind"] == "registered" and "segment_uids" not in reference["metadata"]:
            _, old_metadata = _from_registered(db, case_id, RegisteredSeries.model_validate(reference["source"]))
            reference = {**reference, "metadata": {**reference["metadata"], "segment_uids": old_metadata["segment_uids"]}}
        check = compare_to_fit(reference, args.reference_fit, recipe, metadata, points, forecast)
        partial = check["counts"].get("scored", 0) != len(points) or any(p["extrapolated"] for p in points) or check.get("unverified_conditions")
        content.update(check=check, status="partial" if partial or reference["status"] == "partial" else "completed")
    else:
        try:
            if isinstance(source, RegisteredSeries):
                originals = {}
                duplicates = []
                for cell, point in zip(observation_keys(recipe, metadata, points), points):
                    prior = originals.get(cell)
                    if prior is None:
                        originals[cell] = point
                        continue
                    if (point["z_mm"] != prior["z_mm"] or
                        not math.isclose(point["radius_mm"], prior["radius_mm"], rel_tol=1e-12)):
                        raise ValueError(f"读数 #{prior['reading_id']} 和 #{point['reading_id']} 指向原件同一位置"
                                         f"「{point['locator']}」，但坐标或半径不同。请核对并选用正确记录，或分别标明帧号。")
                    duplicates.append({"reading_id":point["reading_id"], "counted_reading_id":prior["reading_id"]})
                points = list(originals.values())
                content.update(points=points, duplicate_readings=duplicates)
            if args.holdout:
                complete_metadata = metadata
                if isinstance(source, RegisteredSeries):
                    files = {f["attachment_id"]: f["sha256"] or f["attachment_id"] for f in metadata["files"]}
                    for point in points:
                        point["acquisition_id"] = files[point["attachment_id"]]
                    grouping = "source_file"
                else:
                    grouping = "acquisition_column" if source.acquisition_column else "position"
                points, report = partition_stations(points, args.holdout, grouping=grouping)
                report.update(source=recipe, files=complete_metadata["files"], width_basis=metadata["width_basis"],
                              axis=metadata["axis"], reference=metadata["reference"])
                if isinstance(source, RegisteredSeries):
                    # The holdout must not supply the training medium/wavelength or
                    # appear among the source files later attached to a launch.
                    fit_input = source.model_copy(update={"reading_ids": [p["reading_id"] for p in points]})
                    points, metadata = _from_registered(db, case_id, fit_input)
                    content.update(source=fit_input.model_dump(), metadata=metadata)
                content.update(points=points, holdout=report)
            fit = fit_caustic([p["z_mm"] for p in points], [p["radius_mm"] for p in points], metadata["wavelength_nm"], metadata["medium_n"],loss="radius")
        except UnresolvedCaustic as exc:
            if not exc.repeatability["repeated_positions"] and not args.holdout:
                return {"status":"needs_input", "message":str(exc), "point_count":len(points), "source":recipe}
            content.update(status="partial", message=str(exc), point_count=len(points), repeatability=exc.repeatability)
            return _save_study(db, case_id, key, "beam_width_summary", content, task_id)
        except ValueError as exc:
            return {"status": "needs_input", "message": str(exc), "point_count": len(points), "source": recipe}
        for point, predicted in zip(points, fit.pop("data")["fit_radius_mm"]):
            point["fit_radius_mm"] = predicted
            point["residual_mm"] = point["radius_mm"] - predicted
        content.update(status=fit["quality"], fit=fit)
        if args.holdout:
            assess_holdout(content["holdout"], fit, recipe, interval=complete_metadata.get("interval_mm"))
        if isinstance(source, RegisteredSeries):
            content["comparison"] = _compare_to_project(points, metadata, fit)
    kind = "beam_caustic_check" if args.reference_fit else "beam_caustic_study"
    return _save_study(db, case_id, key, kind, content, task_id)


def fit_evidence_key(content):
    """Identify the frozen fit, its input mapping and original readings across workspace copies."""
    evidence = {field:content.get(field) for field in ("method_version", "source", "metadata", "points", "fit")}
    data = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "fit-v1:" + hashlib.sha256(data).hexdigest()


def _record(row):
    record = {"id": row.id, **row.content}
    if "method_version" in record:
        record["method"] = {"name": "study_measured_beam", "title": "分析多位置光斑测量",
                            "version": str(record["method_version"]), "provider_display_name": APP_NAME}
    if row.content_type == "beam_caustic_study" and row.content.get("fit"):
        record["study_key"] = fit_evidence_key(row.content)
    if row.content_type == "beam_caustic_prediction":
        record["recorded_at"] = row.generated_at.isoformat()
    return record


def _save_study(db, case_id, key, kind, content, task_id):
    if task_id is not None:
        active = db.query(AgentTask.status).filter_by(case_id=case_id,id=task_id).with_for_update().scalar()
        if active != "running":
            raise ValueError("任务已停止，未写入本次测量分析。")
    row = GeneratedContent(case_id=case_id, content_type=kind, operation_key=key, content=content)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        previous = db.query(GeneratedContent).filter_by(case_id=case_id, operation_key=key).one_or_none()
        if previous:
            return _record(previous)
        raise
    return _record(row)


def beam_study_history(db, case_id, study_id=None, positions_mm=None):
    rows = db.query(GeneratedContent).filter_by(case_id=case_id).filter(
        GeneratedContent.content_type.in_(("beam_caustic_study", "beam_caustic_check", "beam_caustic_prediction", "beam_width_summary")))
    if study_id is not None:
        row = rows.filter_by(id=study_id).one_or_none()
        if not row:
            raise ValueError("案例中找不到此测量分析。")
        if positions_mm is not None:
            forecast = predictions(row.content, positions_mm)
            fit = row.content["fit"]
            warnings = fit["warnings"]
            if "output_limits" in fit:
                message = fit["output_limits"].get("geometry")
                warnings = ([message] if message else []) + fit.get("prediction_warnings", [])
            return {"id": row.id, "status": "partial" if warnings or any(p["extrapolated"] for p in forecast) else "completed",
                    "metadata": row.content["metadata"], "predictions": forecast, "fit_warnings": warnings}
        return _record(row)
    content = GeneratedContent.content
    overview = rows.with_entities(GeneratedContent.id, GeneratedContent.content_type, content["status"].as_string(), content["metadata"]["axis"].as_string(),
                                  content["metadata"]["conditions"].as_string(), content["source"]["kind"].as_string(),
                                  content["metadata"]["medium_n"].as_float())
    return [dict(zip(("id","type","status","axis","conditions","source","medium_n"), r)) for r in overview.order_by(GeneratedContent.id.desc())]

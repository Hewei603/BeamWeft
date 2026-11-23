"""Evaluate a frozen caustic fit in its original longitudinal frame."""
import math
from collections import Counter
from pathlib import Path

from pydantic import Field, model_validator
from ..attachment_files import WORKBOOK_EXTENSIONS
from ..study_api import StrictInput


class MeasuredCaustic(StrictInput):
    waist_radius_mm: float = Field(gt=0)
    waist_z_mm: float
    divergence_half_angle_rad: float = Field(gt=0)
    sampled_range_mm: tuple[float, float]
    measurement_basis: str = Field(min_length=1)
    prediction_warnings: list[str] = Field(default_factory=list, max_length=32, exclude_if=lambda v: not v)

    @model_validator(mode="after")
    def ordered_stations(self):
        if self.sampled_range_mm[0] >= self.sampled_range_mm[1]:
            raise ValueError("测量起点应小于终点。")
        return self


class CausticPrediction(StrictInput):
    geometry: MeasuredCaustic = Field(title="已拟合的同轴向光束模型")
    positions_mm: list[float] = Field(min_length=1, max_length=300, title="预测位置 / mm")


def project_caustic(args):
    geometry = args.geometry.model_dump()
    rows = predict_widths(geometry, args.positions_mm, args.geometry.sampled_range_mm)
    outside = sum(row["extrapolated"] for row in rows)
    warnings = list(args.geometry.prediction_warnings)
    if outside:
        warnings.append(f"{outside} 个位置超出原测量范围，外推依赖该段继续均匀传播。")
    return {"status":"partial" if warnings else "completed", "points":rows, "warnings":warnings,
            "measurement_basis":args.geometry.measurement_basis, "positions_mm":args.positions_mm,
            "radius_mm":[row["radius_mm"] for row in rows]}


def predictions(study, positions):
    fit = study.get("fit")
    if not fit:
        raise ValueError("请选择一份已保存的光束拟合。")
    sampled = [p["z_mm"] for p in study["points"]]
    return predict_widths(fit, positions, (min(sampled), max(sampled)), study["metadata"].get("interval_mm"))


def predict_widths(fit, positions, sampled_range, interval=None):
    low, high = sampled_range
    rows = []
    for z in positions:
        if interval and not interval[0] <= z <= interval[1]:
            raise ValueError(f"z = {z:g} mm 超出工程确认的连续介质段 [{interval[0]:g}, {interval[1]:g}] mm。")
        radius = math.hypot(fit["waist_radius_mm"], fit["divergence_half_angle_rad"] * (z - fit["waist_z_mm"]))
        if not math.isfinite(radius) or not math.isfinite(2 * radius):
            raise ValueError("指定位置的预测超出可表示范围，请核对轴向坐标。")
        rows.append({"z_mm": z, "radius_mm": radius, "diameter_mm": 2 * radius, "extrapolated": not low <= z <= high})
    return rows


def fitted_curvature(fit, position):
    """Physical second-moment/equivalent wavefront radius in the homogeneous measurement medium."""
    delta = position - fit["waist_z_mm"]
    zr = fit["waist_radius_mm"] / fit["divergence_half_angle_rad"]
    if abs(delta) <= 32 * max(map(math.ulp, (position, fit["waist_z_mm"], zr))):
        return None
    curvature = delta + (zr / delta) * zr
    if not math.isfinite(curvature):
        raise ValueError("源面曲率超出数值范围，请核对坐标和单位。")
    return curvature


def observation_keys(source, meta, points):
    """Identify numerical observations by original bytes and locator, across duplicate uploads."""
    files = {f["attachment_id"]: f["sha256"] or ("attachment", f["attachment_id"]) for f in meta["files"]}
    if source["kind"] == "table":
        digest = files[source["attachment_id"]]
        workbook = Path(meta["files"][0]["path"]).suffix.lower() in WORKBOOK_EXTENSIONS
        sheet = source["sheet"] if workbook else ""
        return [(digest, sheet, p["row"], source["width_column"]) for p in points]
    return [(files[p["attachment_id"]], p["locator"].strip(), source["axis"]) for p in points]


def _frozen_values(forecast, positions):
    saved = {p["z_mm"]: p for p in forecast["predictions"]}
    rows = []
    for z in positions:
        if z in saved:
            rows.append(saved[z])
            continue
        matches = [p for at, p in saved.items() if math.isclose(at, z, rel_tol=0., abs_tol=1e-9)]
        if len(matches) != 1:
            raise ValueError(f"z = {z:g} mm 无法唯一对应这份已存预测。请核对坐标，或选择原拟合计算其他位置。")
        rows.append(matches[0])
    return rows


def score_reading(point, prediction, source, use):
    """Compare using the normalized radius and the original instrument convention."""
    definition = point.get("definition", source.get("width_definition"))
    unit = point.get("width_unit", source.get("width_unit"))
    diameter = definition not in {"radius", "radius_1e2"}
    predicted_width = prediction["diameter_mm" if diameter else "radius_mm"] * (1000 if unit == "um" else 1)
    if not math.isfinite(predicted_width):
        raise ValueError("预测宽度超出所选仪器单位的数值范围。")
    residual = point["radius_mm"] - prediction["radius_mm"]
    point.update(model_z_mm=prediction["z_mm"], predicted_radius_mm=prediction["radius_mm"], residual_mm=residual,
                 predicted_width=predicted_width, width_residual=point["raw_width"]-predicted_width,
                 extrapolated=prediction["extrapolated"], use=use)
    return residual


def compare_to_fit(reference, registration, source, metadata, points, forecast=None):
    previous = reference["metadata"]
    if previous["axis"] != metadata["axis"]:
        raise ValueError("本次读数与所选拟合方向不同，请选取同一横向轴。")
    if previous.get("beam_uid") and metadata.get("beam_uid") and previous["beam_uid"] != metadata["beam_uid"]:
        raise ValueError("拟合与新读数来自不同的光束。")
    if previous["width_basis"] != metadata["width_basis"]:
        raise ValueError("新读数与原拟合的宽度口径不同，请分别核对二阶矩与 1/e² 定义。")
    unverified = []
    for key, label in (("medium_n","介质折射率"), ("wavelength_nm","真空波长")):
        if previous.get(key) is None or metadata.get(key) is None:
            unverified.append(label)
        elif not math.isclose(previous[key], metadata[key], rel_tol=1e-9):
            raise ValueError("拟合与新读数的波长或介质折射率不匹配。")
    same_revision = source.get("revision_id") and source["revision_id"] == reference["source"].get("revision_id")
    if same_revision and registration.z_offset_mm:
        raise ValueError("同一工程版本登记的坐标共用零点，两份记录的坐标偏移应为零。")
    original_segments = set(previous.get("segment_uids", []))
    if same_revision and original_segments and not set(metadata["segment_uids"]) <= original_segments:
        raise ValueError("新读数所在段超出原工程拟合的介质区间，请核对元件与传播路线。")
    original_cells = observation_keys(reference["source"], previous, reference["points"])
    fit_cells = set(original_cells)
    original_hashes = {cell[0] for cell in fit_cells}
    same_kind = source["kind"] == reference["source"]["kind"]
    new_cells = observation_keys(source, metadata, points)
    original_source = reference["source"]
    same_acquisition_mapping = (same_kind and source.get("acquisition_column") is not None
                                and source["acquisition_column"] == original_source.get("acquisition_column"))
    training_acquisitions = set()
    if same_acquisition_mapping:
        training_acquisitions = {(cell[0], cell[1], point["acquisition_id"])
                                 for cell, point in zip(original_cells, reference["points"])}
    grouped_by_file = reference.get("holdout", {}).get("grouping") == "source_file"
    seen = set()
    limits = Counter()
    retained = []
    positions = [p["z_mm"] + registration.z_offset_mm for p in points]
    rows = _frozen_values(forecast, positions) if forecast else predictions(reference, positions)
    for point, row, cell in zip(points, rows, new_cells):
        if cell in fit_cells:
            use = "fit_input"
        elif (same_acquisition_mapping and (cell[0], cell[1], point["acquisition_id"]) in training_acquisitions
              or grouped_by_file and cell[0] in original_hashes):
            use = "fit_acquisition"
        elif not same_kind and cell[0] in original_hashes:
            use = "unresolved_locator"
        elif cell in seen:
            use = "repeated_cell"
        else:
            use = "scored"
        seen.add(cell)
        residual = score_reading(point, row, source, use)
        limits[use] += 1
        if use == "scored":
            retained.append(residual)
    count = len(retained)
    # Scaling avoids overflow when squaring otherwise finite residuals.
    maximum = max(map(abs, retained), default=0.)
    rms = maximum * math.sqrt(math.fsum((v / maximum) ** 2 for v in retained) / count) if maximum else 0.
    if not math.isfinite(2*rms):
        raise ValueError("全直径误差超出可表示范围，请核对实测宽度和单位。")
    result = {"reference_study_id": reference["id"], "reference_source": reference["source"],
            "reference_metadata": previous, "registration_note": registration.note, "z_offset_mm": registration.z_offset_mm,
            "fit_warnings": reference["fit"]["warnings"], "counts": dict(limits), "unverified_conditions": unverified,
            "radius_rms_mm": rms if count else None, "diameter_rms_mm": 2*rms if count else None,
            "max_abs_radius_error_mm": maximum if count else None}
    if count:
        scored = [p for p in points if p["use"] == "scored"]
        result["scored_positions"] = len({p["model_z_mm"] for p in scored})
        peak = max(scored, key=lambda p: abs(p["residual_mm"]))
        result["largest_error"] = {k: peak[k] for k in ("model_z_mm", "raw_width", "predicted_width", "width_residual")}
        result["largest_error"].update(unit=peak.get("width_unit", source.get("width_unit")),
                                      definition=peak.get("definition", source.get("width_definition")))
    if forecast:
        hashes = {f["sha256"] for f in forecast["archive_snapshot"]}
        result.update(forecast_id=forecast["id"], forecast_recorded_at=forecast["recorded_at"],
                      archive_evidence=[{"attachment_id": f["attachment_id"], "path": f["path"],
                                         "state": "already_present" if f["sha256"] in hashes else
                                         "uncertain" if not all(hashes) else "added_later"} for f in metadata["files"]])
    return result

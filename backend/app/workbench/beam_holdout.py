"""Station-blocked validation on a documented homogeneous caustic series."""
import math
from bisect import bisect_left, bisect_right
from collections import defaultdict

from pydantic import Field, field_validator

from ..study_api import StrictInput
from .beam_predictions import predict_widths, score_reading


class HoldoutStations(StrictInput):
    positions_mm: list[float] = Field(min_length=1, max_length=3000,
        description="Axial coordinates in mm in the measured frame. Every row at a chosen station is withheld.")

    @field_validator("positions_mm")
    @classmethod
    def normalize(cls, values):
        return sorted(set(values))


def partition_stations(points, holdout, *, grouping="position"):
    """Select by position only; all stations connected by a known acquisition stay together."""
    stations = {p["z_mm"] for p in points}
    ordered = sorted(stations)
    requested = set()
    for wanted in holdout.positions_mm:
        if wanted in stations:
            requested.add(wanted)
            continue
        # Decimal coordinates may round during a cm-to-mm conversion. Do not
        # infer any wider physical equivalence without instrument resolution.
        # At a power-of-two boundary the neighboring float spacing can double.
        # Bracket both sides, then apply the original tolerance to each candidate.
        spacing = math.ulp(wanted)
        start = bisect_left(ordered, wanted - 8 * spacing)
        stop = bisect_right(ordered, wanted + 8 * spacing)
        matches = [z for z in ordered[start:stop] if abs(z-wanted) <= 4 * max(math.ulp(z), spacing)]
        if len(matches) != 1:
            raise ValueError(f"留出位置 {wanted:g} mm 在所选原始读数中未能唯一定位。请按当前坐标基准和单位核对。")
        requested.add(matches[0])
    ids_by_station, stations_by_id = defaultdict(set), defaultdict(set)
    if grouping not in {"position", "source_file", "acquisition_column"}:
        raise ValueError("未知采集分组方式。")
    if grouping == "position" and any(p.get("acquisition_id") is not None for p in points):
        grouping = "acquisition_column"
    if grouping != "position":
        for point in points:
            station, acquisition = point["z_mm"], point.get("acquisition_id")
            if acquisition is None or not str(acquisition).strip():
                raise ValueError("所选读数中存在空的采集标识，请补全原件映射后再留出。")
            ids_by_station[station].add(acquisition)
            stations_by_id[acquisition].add(station)
    reserved = set(requested)
    pending, grouped = list(reserved), set()
    while pending:
        for acquisition in ids_by_station[pending.pop()] - grouped:
            grouped.add(acquisition)
            extra = stations_by_id[acquisition] - reserved
            reserved.update(extra)
            pending.extend(extra)
    training_stations = sorted(stations - reserved)
    if len(training_stations) < 5:
        recovery = "请调整留出位置或增加独立采集。"
        if grouping == "source_file":
            recovery += "若一个表格汇总了多次采集，可从原表映射各次采集的标识列。"
        raise ValueError(f"已整组留出 {len(reserved)} 个位置，训练集余 {len(training_stations)} 个不同位置。"
                         f"焦散拟合至少需要 5 个。{recovery}")
    training = [p for p in points if p["z_mm"] not in reserved]
    notes = {"position": "按轴向位置整组留出。原件未登记采集编号，无法据此确认不同位置是否来自独立采集。",
             "source_file": "按位置和原始采集文件共同分组，相同原件字节的多次登记始终在同一侧。",
             "acquisition_column": "按位置和输入中的采集标识共同分组。同一标识关联的位置全部留在同一侧。"}
    report = {"status": "not_scored", "requested_positions_mm": holdout.positions_mm,
              "positions_mm": sorted(reserved), "training_positions_mm": training_stations,
              "training_range_mm": [training_stations[0], training_stations[-1]], "grouping": grouping,
              "grouping_note": notes[grouping], "points": [dict(p) for p in points if p["z_mm"] in reserved],
              "uncertainty_note": "此项衡量同批资料在未用于拟合位置的预测误差。重复读数共享位置和实验条件。"
                                  "误差统计不能充当测量不确定度、预测区间或新实验的独立验证。"}
    return training, report


def _rms(values):
    scale = max(map(abs, values))
    return scale * math.sqrt(math.fsum((v/scale)**2 for v in values) / len(values)) if scale else 0.


def assess_holdout(report, fit, source, *, interval=None):
    """Score the untouched readings in their archived units and give each station equal weight."""
    points = report["points"]
    predictions = predict_widths(fit, report["positions_mm"], report["training_range_mm"], interval)
    by_z = {prediction["z_mm"]: prediction for prediction in predictions}
    groups = defaultdict(list)
    for point in points:
        prediction = by_z[point["z_mm"]]
        groups[point["z_mm"]].append(score_reading(point, prediction, source, "held_out"))
    stations = []
    for z in sorted(groups):
        residuals = groups[z]
        stations.append({"z_mm": z, "n": len(residuals), "extrapolated": by_z[z]["extrapolated"],
                         "mean_residual_mm": math.fsum(r/len(residuals) for r in residuals),
                         "rms_radius_mm": _rms(residuals)})
    report.update(status="scored", station_results=stations,
                  station_rms_radius_mm=_rms([s["rms_radius_mm"] for s in stations]),
                  max_abs_radius_error_mm=max(abs(p["residual_mm"]) for p in points))

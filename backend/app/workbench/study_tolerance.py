"""Inspect sampled adjustment margins against the hard limits of a frozen design study."""
from collections import Counter, defaultdict
from itertools import product
import math
from typing import Literal

from pydantic import Field
from sqlalchemy.orm import Session

from ..models import GeneratedContent
from ..study_api import StrictInput
from .study_search import StudySearch, _exceeds, _numeric


class StudyTolerance(StrictInput):
    search_id: int = Field(gt=0, description="Saved parameter search or reassessment in this project.")
    row_index: int = Field(ge=0, description="Zero-based row in the original, unsorted search rows. This is the nominal setting.")
    axis_index: int = Field(ge=0, description="Zero-based parameter group (axis) from recipe.axes.")
    coordinate_index: int = Field(default=0, ge=0, description="Column of the selected group used for ordering and distances. Coupled columns keep their recorded values.")
    scope: Literal["slice", "all"] = Field(default="slice", description="Hold other groups at nominal values, or require every sampled combination of the other groups to pass.")


def hard_limits(recipe, report):
    limits = []
    for measure in recipe.measures:
        if measure.lower is None and measure.upper is None:
            continue
        info = report["measures"].get(measure.output)
        if not isinstance(info, dict):
            raise ValueError(f"原比较缺少评价量 {measure.output} 的定义。请核对基线计算，并重新保存评价。")
        limits.append({**info, "output": measure.output, "lower": measure.lower, "upper": measure.upper})
    if not limits:
        raise ValueError("此比较只有优化方向。请先给评价量设置可接受上限或下限，并保存评价。")
    return limits


def sample_decision(row, limits, digests):
    rid = row.get("run_id")
    if rid not in digests:
        return "unknown", row.get("message") or "本工程缺少该候选的计算记录。", []
    if row.get("solver_sha256") and row["solver_sha256"] != digests[rid]:
        return "unknown", "候选计算的方法指纹与比较记录不一致。", []
    if row.get("science_status", "completed") not in ("completed", "partial"):
        return "unknown", row.get("message") or "原计算没有可用于验收的结果。", []
    if not isinstance(row.get("values", {}), dict) or not isinstance(row.get("measure_issues", {}), dict):
        return "unknown", "这条记录的读数或异常说明格式不完整，请核对原计算。", []
    violations, missing = [], []
    for limit in limits:
        key, lower, upper = limit["output"], limit["lower"], limit["upper"]
        value = row.get("values", {}).get(key)
        issue = row.get("measure_issues", {}).get(key)
        if issue or not _numeric(value):
            missing.append({**limit, "value":None, "message":issue or row.get("message") or "没有可核对的读数。"})
        elif (lower is not None and _exceeds(lower, value)) or (upper is not None and _exceeds(value, upper)):
            violations.append({**limit, "value":value, "message":"超出允许范围。"})
    if violations:
        return "outside", "已算结果超出约束。", violations + missing
    if missing:
        return "unknown", "缺少判定这组约束所需的读数。", missing
    return "pass", "所选硬限全部满足。", []


def require_all_samples(states):
    if "outside" in states:
        return "outside"
    return "unknown" if "unknown" in states else "pass"


def sampled_cells(recipe, rows, limits, digests):
    expected = Counter(tuple(v for point in combination for v in point)
                       for combination in product(*(axis.points() for axis in recipe.axes)))
    recorded = defaultdict(list)
    for index, row in enumerate(rows):
        coords = row.get("coordinates") if isinstance(row, dict) else None
        if not isinstance(coords, list) or not all(_numeric(v) for v in coords) or tuple(coords) not in expected:
            raise ValueError(f"第 {index + 1} 条候选记录的坐标与原搜索网格不一致，请核对这份比较。")
        state, reason, issues = sample_decision(row, limits, digests)
        run_id = row.get("run_id")
        expected_digest = row.get("solver_sha256")
        receipt = "unavailable"
        if expected_digest and run_id in digests:
            receipt = "matched" if expected_digest == digests[run_id] else "mismatch"
        recorded[tuple(coords)].append({"row_index": index, "run_id": run_id if run_id in digests else None,
                                        "solver_identity": receipt, "state": state, "reason": reason, "issues": issues})

    cells = []
    for coords, count in expected.items():
        witnesses = recorded[coords]
        for _ in range(count - len(witnesses)):
            witnesses.append({"row_index": None, "run_id": None, "solver_identity": "unavailable", "state": "unknown",
                              "reason": "原网格在这组工况下缺少候选记录。", "issues": []})
        if len(witnesses) > count:
            state, reason, issues = "unknown", "同一坐标的记录数超过原网格，无法确认哪些记录属于本次采样。", []
        else:
            state = require_all_samples({row["state"] for row in witnesses})
            decisive = next(row for row in witnesses if row["state"] == state)
            reason, issues = decisive["reason"], decisive["issues"]
        cells.append({"coordinates": list(coords), "state": state, "reason": reason, "issues": issues,
                      "expected_rows": count, "rows": witnesses})
    return cells


def _point(position, settings, entries, ambiguous, anchor_index):
    # A failure in one sampled condition suffices to reject the whole station.
    counts = dict.fromkeys(("pass", "outside", "unknown"), 0)
    for state, *_ in entries:
        counts[state] += 1
    state = "outside" if counts["outside"] else "unknown" if counts["unknown"] else "pass"
    matching = [entry for entry in entries if entry[0] == state]
    _, reason, issues, index, row = next((e for e in matching if e[3] == anchor_index), matching[0])
    if ambiguous:
        state = "unknown"
        reason = "同一坐标对应多个不同的联动设定，不能唯一标定这一档。请选择可区分各档位的坐标。"
        row, index, issues = {}, None, []
    return {"position":position, "coordinates":None if ambiguous else list(settings[0]),
            "state":state, "row_index":index, "run_id":row.get("run_id"), "counts":counts,
            "witness_coordinates":row.get("coordinates"), "reason":reason, "issues":issues,
            **({"alternative_coordinates":[list(s) for s in settings]} if ambiguous else {})}


def _side(nominal, outward):
    if nominal["state"] != "pass":
        return {"passed":None, "stop":nominal, "termination":"nominal", "margin":None}
    last, stop = nominal, None
    for point in outward:
        if point["state"] != "pass":
            stop = point
            break
        last = point
    margin = abs(last["position"] - nominal["position"])
    return {"passed":last, "stop":stop, "termination":stop["state"] if stop else "edge",
            "margin":margin if math.isfinite(margin) else None}


def _inspect(report, request, digests):
    recipe = StudySearch.model_validate(report["recipe"])
    if request.axis_index >= len(recipe.axes):
        raise ValueError("所选参数组不在这份比较中。")
    axis = recipe.axes[request.axis_index]
    if request.coordinate_index >= len(axis.paths):
        raise ValueError("所选排序坐标不在这个联动参数组中。")
    if request.row_index >= len(report["rows"]):
        raise ValueError("找不到作为名义设定的候选行。")
    limits = hard_limits(recipe, report)

    start = sum(len(a.paths) for a in recipe.axes[:request.axis_index])
    end = start + len(axis.paths)
    cells = sampled_cells(recipe, report["rows"], limits, digests)
    anchor = tuple(report["rows"][request.row_index]["coordinates"])
    settings, observations = defaultdict(list), defaultdict(list)
    for cell in cells:
        coords = tuple(cell["coordinates"])
        if request.scope == "slice" and (coords[:start] != anchor[:start] or coords[end:] != anchor[end:]):
            continue
        vector = coords[start:end]
        position = vector[request.coordinate_index]
        if vector not in settings[position]:
            settings[position].append(vector)
        if len(cell["rows"]) > cell["expected_rows"]:
            observations[position].append(("unknown", cell["reason"], [], None, {"coordinates": list(coords)}))
            continue
        for witness in cell["rows"]:
            observations[position].append((witness["state"], witness["reason"], witness["issues"], witness["row_index"],
                                           {"run_id": witness["run_id"], "coordinates": list(coords)}))

    positions = sorted(settings)
    points = [_point(p, settings[p], observations[p], len(settings[p]) > 1, request.row_index) for p in positions]
    at = positions.index(anchor[start + request.coordinate_index])
    nominal = points[at]
    return {**request.model_dump(), "baseline_id":report["baseline_id"], "paths":axis.paths,
            "coordinate_path":axis.paths[request.coordinate_index], "limits":limits, "sample_count":len(points),
            "stations":[{key:p[key] for key in ("position", "state", "row_index", "run_id")} for p in points],
            "nominal":nominal, "lower":_side(nominal, reversed(points[:at])), "upper":_side(nominal, points[at+1:])}


def read_tolerance_source(bind, case_id, search_id):
    with Session(bind) as db:
        records = db.query(GeneratedContent).filter_by(case_id=case_id)
        source = records.filter_by(id=search_id, content_type="study_search").one_or_none()
        if source is None:
            raise ValueError("本工程中找不到要检查的参数比较。")
        report = source.content
        if (not isinstance(report, dict) or not isinstance(report.get("recipe"), dict)
                or not isinstance(report.get("rows"), list) or not isinstance(report.get("measures"), dict)
                or not report.get("baseline_id")):
            raise ValueError("原比较缺少基线、参数网格或评价量定义。请核对保存的比较记录。")
        ids = set()
        for index, row in enumerate(report["rows"]):
            if not isinstance(row, dict) or (row.get("run_id") is not None and type(row["run_id"]) is not int):
                raise ValueError(f"第 {index + 1} 条候选记录的计算引用格式不完整，请核对原比较。")
            if row.get("run_id"):
                ids.add(row["run_id"])
        digests = dict(records.filter(GeneratedContent.id.in_(ids)).filter_by(content_type="scientific_study")
                       .with_entities(GeneratedContent.id, GeneratedContent.content["solver_receipt"]["sha256"].as_string()).all())
        return report, digests


def inspect_tolerance(bind, case_id, args):
    report, digests = read_tolerance_source(bind, case_id, args.search_id)
    return _inspect(report, args, digests)

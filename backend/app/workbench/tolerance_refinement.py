"""Evaluate additional physical settings inside a sampled pass/fail bracket."""
from copy import deepcopy
import math
from typing import Literal

from pydantic import Field, FiniteFloat
from sqlalchemy.orm import Session

from ..models import GeneratedContent
from .studies import installed_study_receipt
from .study_search import StudySearch, _save_search, grid_evaluator
from .study_tolerance import StudyTolerance, inspect_tolerance, sample_decision


MAX_PROBES = 16


class RefineStudyTolerance(StudyTolerance):
    side: Literal["lower", "upper"] = Field(description="Side of the nominal setting with adjacent passing and outside samples.")
    resolution: FiniteFloat = Field(gt=0, description="Requested width of the numeric input bracket, in that input's recorded unit.")


def _probe_recipe(source, axis_index, coordinates, positions):
    axes = []
    offset = 0
    for i, axis in enumerate(source["recipe"]["axes"]):
        paths = axis["paths"]
        fixed = coordinates[offset:offset + len(paths)]
        samples = [[p] for p in positions] if i == axis_index else [fixed]
        axes.append({"paths":paths, "samples":samples})
        offset += len(paths)
    return StudySearch.model_validate({**source["recipe"], "axes":axes})


def _discrete(schema, value, path, root, visited=()):
    """A declared count, choice or discrete step cannot be freely bisected as a real setting."""
    if not isinstance(schema, dict):
        return False
    if "const" in schema or "enum" in schema or schema.get("readOnly"):
        return True
    ref = schema.get("$ref")
    if ref and ref.startswith("#/$defs/") and ref not in visited:
        if _discrete(root.get("$defs", {}).get(ref[8:]), value, path, root, (*visited, ref)):
            return True
    if any(_discrete(part, value, path, root, visited) for part in schema.get("allOf", ())):
        return True
    alternatives = schema.get("anyOf") or schema.get("oneOf")
    if alternatives:
        discriminator = schema.get("discriminator", {})
        if isinstance(value, dict) and discriminator.get("mapping"):
            selected = discriminator["mapping"].get(str(value.get(discriminator["propertyName"])))
            if selected:
                return _discrete({"$ref":selected}, value, path, root, visited)
        branches = [s for s in alternatives if s.get("type") != "null"]
        # A compound untagged union can describe distinct objects with different
        # controls. Without an identified variant, require a continuous setting in all.
        if path:
            return any(_discrete(s, value, path, root, visited) for s in branches)
        return bool(branches) and all(_discrete(s, value, path, root, visited) for s in branches)
    if not path:
        return schema.get("type") == "integer" or "multipleOf" in schema
    key, *rest = path
    if isinstance(key, int):
        items = schema.get("prefixItems", ())
        child = items[key] if key < len(items) else schema.get("items")
    else:
        child = schema.get("properties", {}).get(key, schema.get("additionalProperties"))
    child_value = None
    if isinstance(value, dict):
        child_value = value.get(key)
    elif isinstance(value, list) and isinstance(key, int) and key < len(value):
        child_value = value[key]
    return _discrete(child, child_value, rest, root, visited)


def refine_tolerance(bind, case_id, args, registry, task_id=None):
    if args.scope != "slice":
        raise ValueError("补算边界时需要固定其他参数组。可在全部工况的结果中查看限制工况，再选择这一组设定。")
    selection = StudyTolerance.model_validate(args.model_dump(exclude={"side", "resolution"}))
    view = inspect_tolerance(bind, case_id, selection)
    if len(view["paths"]) != 1:
        raise ValueError("这是联动采样组。补算中间位置需要明确各参数的联动关系，请在参数搜索中给出新的成组设定。")
    bracket = view[args.side]
    if bracket["termination"] != "outside" or not bracket["passed"] or not bracket["stop"]:
        raise ValueError("所选方向需要相邻的已验证达标点和越界点。可以先增加采样范围或补齐缺失的评价依据。")
    inside, outside = bracket["passed"]["position"], bracket["stop"]["position"]
    if abs(inside-outside) <= args.resolution:
        raise ValueError(f"两侧已算取值相距 {abs(inside-outside):g}，已满足所填分辨率。")
    with Session(bind) as db:
        records = db.query(GeneratedContent).filter_by(case_id=case_id)
        source = records.filter_by(id=args.search_id, content_type="study_search").one().content
        baseline = records.filter_by(id=view["baseline_id"], content_type="scientific_study").one_or_none()
        if baseline is None:
            raise ValueError("本工程缺少基线计算。")
        schema = baseline.content["definition"].get("inputs", {})
        if _discrete(schema, baseline.content["inputs"], view["coordinate_path"], schema):
            raise ValueError("这个参数取整数、选项或离散步长。请在参数搜索中直接比较可用的档位。")
        passing = source["rows"][bracket["passed"]["row_index"]]
        failing = source["rows"][bracket["stop"]["row_index"]]
        digest = passing.get("solver_sha256")
        saved = source.get("solver_receipts", {}).get(digest)
        current = installed_study_receipt(baseline.content["definition"], saved, registry)
        if (not digest or failing.get("solver_sha256") != digest or (current or {}).get("sha256") != digest
                or not saved or not saved.get("complete") or saved.get("opaque_state")):
            raise ValueError("新增取值需使用两端计算时的同一求解器和元件实现。可以查阅原区间，或在当前环境重新计算端点。")

        column = sum(len(a["paths"]) for a in source["recipe"]["axes"][:args.axis_index])
        recipe = _probe_recipe(source, args.axis_index, passing["coordinates"], sorted([inside,outside]))
        execution = {"computed":0, "reused":0}

        def record_execution(record):
            execution[record["execution"]] += 1

        report, evaluate = grid_evaluator(db, case_id, recipe, registry, task_id, on_run=record_execution)
        report["solver_receipts"][digest] = saved
        rows = [deepcopy(passing), deepcopy(failing)]
        for row in rows:
            if row.get("root_measurements") and not row.get("measurement_source_search_id") and source.get("root_search_id"):
                row["measurement_source_search_id"] = source["root_search_id"]
        info = {**args.model_dump(exclude={"search_id", "resolution"}), "source_search_id":args.search_id,
                "requested_resolution":args.resolution, "initial_pass":inside, "initial_outside":outside}
        status = "budget"
        for _ in range(MAX_PROBES):
            # Avoid overflow for brackets crossing zero near the floating-point limit.
            difference = outside-inside
            mid = inside+difference/2 if math.isfinite(difference) else inside/2+outside/2
            if mid in (inside, outside):
                status = "precision"
                break
            coords = list(passing["coordinates"])
            coords[column] = mid
            row = evaluate(coords)
            rows.append(row)
            if row.get("run_id") and row.get("solver_sha256") != digest:
                raise ValueError("补算时求解器或元件实现已变化，请在同一环境重建比较。")
            run_id = row.get("run_id")
            decision, reason, issues = sample_decision(row, view["limits"], {run_id:digest} if run_id else {})
            if decision == "unknown":
                status = "unknown"
                info["blocked"] = {"position":mid, "run_id":run_id, "reason":reason, "issues":issues}
                break
            if decision == "pass":
                inside = mid
            else:
                outside = mid
            if abs(inside-outside) <= args.resolution:
                status = "resolved"
                break

        rows.sort(key=lambda row: row["coordinates"][column])
        report["recipe"]["axes"][args.axis_index]["samples"] = [[row["coordinates"][column]] for row in rows]
        width = abs(inside-outside)
        report["refinement"] = {**info, "status":status, "pass_position":inside, "outside_position":outside,
                                "width":width if math.isfinite(width) else None, "evaluations":len(rows)-2}
        for row in rows:
            row.pop("rank", None)
            row.pop("preferred", None)
        execution["without_record"] = len(rows) - 2 - execution["computed"] - execution["reused"]
        saved_report = _save_search(db, case_id, report, rows, recipe.measures, task_id,
                                    creation_evidence={"original_refinement_execution":execution})
        return {**saved_report, "refinement_execution":execution}

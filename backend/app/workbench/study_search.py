"""Explore independent or coupled parameter grids using a saved scientific calculation."""
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, localcontext
from hashlib import sha256
from itertools import product
import json
import math
from typing import Annotated, Literal

from pydantic import Field, FiniteFloat, ValidationError, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import AgentTask, GeneratedContent
from ..solver_receipt import solver_cache_key
from ..study_api import PublishedPort, StrictInput
from .authoring import layout_receipt
from .project_outputs import LAYOUT, FrozenProjectOutputs, published_project_outputs
from .projects import read_project, validate_locked_parts
from .scoped_parameters import parameter_label, scan_bindings
from .studies import StudyFailure, _output_value, recorded_study_inputs, run_study
from .study_inputs import WorkingDraft, capture_snapshot
from .study_replay import _rebind_port, _row_address


MAX_POINTS = 128
FieldPath = Annotated[list[str | Annotated[int, Field(ge=0)]], Field(min_length=1, max_length=32)]
Coordinate = Annotated[list[FiniteFloat], Field(min_length=1, max_length=16)]


class LinearSamples(StrictInput):
    start: Coordinate
    stop: Coordinate
    count: int = Field(ge=2, le=MAX_POINTS)


class SearchAxis(StrictInput):
    paths: list[FieldPath] = Field(min_length=1, max_length=16, description="Paths in the frozen study inputs. Use field names and zero-based integer indices. Paths on one axis change together.")
    samples: Annotated[list[Coordinate], Field(min_length=1, max_length=MAX_POINTS)] | LinearSamples

    def points(self):
        if isinstance(self.samples, list):
            return self.samples
        start, stop, count = self.samples.start, self.samples.stop, self.samples.count
        return [start, *[[a*(1-i/(count-1)) + b*(i/(count-1)) for a,b in zip(start, stop)]
                         for i in range(1, count-1)], stop]

    @model_validator(mode="after")
    def matching_columns(self):
        rows = self.samples if isinstance(self.samples,list) else [self.samples.start,self.samples.stop]
        if any(len(row) != len(self.paths) for row in rows):
            raise ValueError("每一行都需要给本轴的每个参数一个数值。")
        return self


class SearchMeasure(StrictInput):
    output: str = Field(min_length=1, max_length=2600, description="A numeric metric or published scalar output path of the baseline run. Table rows are matched by stable coordinates.")
    direction: Literal["min", "max", "target"] | None = None
    target: Annotated[FiniteFloat, Field(strict=True)] | None = Field(default=None, exclude_if=lambda value: value is None,
                                      description="Desired scalar in the baseline output unit. Used only with direction 'target', which minimizes absolute deviation on a linear scale.")
    lower: FiniteFloat | None = None
    upper: FiniteFloat | None = None
    rationale: str = Field(default="", max_length=500, description="Optional experimental reason or evidence for this preference or bound. Identify a provisional assumption when relevant.")

    @model_validator(mode="after")
    def meaningful(self):
        if (self.direction == "target") != (self.target is not None):
            raise ValueError("选择「最接近目标值」时需要填写目标值，其他比较方向请留空。")
        if self.direction is None and self.lower is None and self.upper is None:
            raise ValueError("请选择优化方向或至少一个可接受边界。")
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError("下限超过上限。")
        return self


Measures = Annotated[list[SearchMeasure], Field(min_length=1, max_length=8)]


class StudyReassessment(StrictInput):
    search_id: int = Field(gt=0, description="Saved parameter comparison in this project. Its exact candidates and scientific results are reused.")
    measures: Measures

    @model_validator(mode="after")
    def unique_measures(self):
        if len({m.output for m in self.measures}) != len(self.measures):
            raise ValueError("同一评价量只能选择一次。")
        return self


class StudySearch(StrictInput):
    run_id: int = Field(gt=0, description="Saved baseline scientific study in this case.")
    axes: list[SearchAxis] = Field(min_length=1, max_length=8, description="Cross product across axes; columns within each axis are paired. Up to 128 combinations per search.")
    measures: Measures
    registration: str = Field(default="", max_length=500, description="When varying a linked project layout, explain which reference planes, axes and experimental relationships are preserved.")

    @model_validator(mode="after")
    def check_grid(self):
        paths = [tuple(path) for axis in self.axes for path in axis.paths]
        keys = [m.output for m in self.measures]
        if len(set(paths)) != len(paths) or len(set(keys)) != len(keys):
            raise ValueError("同一参数或评价量只能选择一次。")
        if math.prod(len(a.points()) for a in self.axes) > MAX_POINTS:
            raise ValueError(f"本次组合超过 {MAX_POINTS} 个。请缩小网格或分段运行。")
        return self


def _numeric(value):
    return type(value) in (float, int) and math.isfinite(value)


def _target_deviation(value, target):
    # Work from the published decimal readings, including ties across the target.
    reading, desired = Decimal(str(value)), Decimal(str(target))
    with localcontext() as context:
        last_place = min(reading.as_tuple().exponent, desired.as_tuple().exponent)
        context.prec = max(reading.adjusted(), desired.adjusted()) - last_place + 2
        distance = abs(reading - desired)
    number = float(distance)
    # JSON numbers round-trip through binary64 in the workbench. Keep an exact decimal
    # for the rare distances that would otherwise overflow or collapse into a false tie.
    if math.isfinite(number) and Decimal(str(number)) == distance:
        return number
    return str(distance)


def _exceeds(left, right):
    # Inclusive limits admit floating-point contact after unit conversion.
    return left > right and left-right > 4 * max(math.ulp(left),math.ulp(right))


def _validation_message(error, schema):
    limits = {"greater_than":("大于","gt"), "greater_than_equal":("大于等于","ge"),
              "less_than":("小于","lt"), "less_than_equal":("小于等于","le")}
    reasons = []
    for issue in error.errors(include_input=False, include_url=False)[:5]:
        location = list(issue["loc"])
        if location:
            location[0] = schema.get("properties", {}).get(location[0], {}).get("title") or location[0]
        field = " › ".join(map(str,location)) or "候选模型"
        rule = limits.get(issue["type"])
        if rule:
            message = f"需要{rule[0]} {issue['ctx'][rule[1]]}"
        else:
            message = issue["msg"].removeprefix("Value error, ")
        reasons.append(f"{field} · {message}")
    return "。".join(reasons)


def _address(inputs, path):
    node = inputs
    for key in path:
        parent = node
        if isinstance(parent, dict) and isinstance(key, str) and key in parent:
            node = parent[key]
        elif isinstance(parent, list) and type(key) is int and 0 <= key < len(parent):
            node = parent[key]
        else:
            raise ValueError(f"基线输入中找不到参数 {path}。")
    if not _numeric(node):
        raise ValueError(f"参数 {path} 在基线中没有已确定的数值。")
    return parent, key


def _key(prefix, data):
    body = json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    return prefix + sha256(body).hexdigest()


def _existing(db, case_id, key):
    return db.query(GeneratedContent).filter_by(case_id=case_id, operation_key=key).one_or_none()


def _snapshot(db, case_id, source, layout, verify=None):
    rid = source["revision_id"]
    if all(source["project"].get(key) == val for key, val in layout.items()):
        if verify:
            verify(source)
        return rid
    draft = WorkingDraft(project={**source["project"], **layout}, source_revision_id=rid,
                         base_revision_id=source.get("base_revision_id") if source.get("model_kind")=="draft" else rid)
    key = _key("grid-model:", [rid, layout, solver_cache_key(layout_receipt(draft.project))])
    row = _existing(db, case_id, key)
    if row:
        if verify:
            verify(read_project(db, case_id, row.id))
        return row.id
    validate_locked_parts(source["project"],draft.project)
    try:
        result = capture_snapshot(db, case_id, draft, "参数搜索候选", operation_key=key)
        if verify:
            verify(result)
        db.commit()
        return result["revision_id"]
    except IntegrityError:
        db.rollback()
        row = _existing(db, case_id, key)
        if row is None:
            raise
        if verify:
            verify(read_project(db, case_id, row.id))
        return row.id
    except ValueError:
        db.rollback()
        raise


def _layout_bounds(db, case_id, base, roots=None):
    """Resolve current search permissions from the actual frozen source, including for pre-contract receipts."""
    sources, models, bindings = {}, {}, {}
    for root, port in base["definition"].get("input_ports", {}).items():
        if (roots is not None and root not in roots) or port["quantity"] != LAYOUT.quantity or not base["inputs"].get(root):
            continue
        ref = base.get("connections", {}).get(root)
        layout = base["inputs"][root]
        if ref is None:
            bindings[root] = scan_bindings(layout, {"scope":"layout"}, layout)
            continue
        if "revision_id" not in ref:
            continue
        rid = ref["revision_id"]
        if rid not in models:
            try:
                models[rid] = FrozenProjectOutputs(db, case_id, rid)
            except ValueError:
                # An older or partly transferred receipt remains readable. It grants no write target.
                models[rid] = None
        source = models[rid]
        if source is None:
            bindings[root] = {}
            continue
        sources[root] = source
        evidence = source.get(ref["port"])
        bindings[root] = scan_bindings(source.row["project"], evidence or {}, layout)
    return sources, bindings


def layout_search_parameters(db, case_id, base):
    _, roots = _layout_bounds(db, case_id, base)
    result = {}
    for root, bindings in roots.items():
        items = []
        for path, owner in bindings.items():
            label = parameter_label(path)
            if owner[0] == "observation_planes" and owner[-1] == "depth_mm":
                if path[0] == "system":
                    continue  # The projected terminal length and the station depth control the same setting.
                label = "从定位元件入口起的深度 / mm"
            items.append({"path":list(path), **({"label":label} if label else {})})
        result[root] = items
    return result


def _layout_candidate(db, case_id, source, port, paths, bindings, baseline, candidate):
    if candidate == baseline:
        return source.row["revision_id"]
    evidence = source.get(port)
    full = deepcopy(source.row["project"])
    requested = []
    for path in paths:
        prior, key = _address(baseline, path)
        proposed, _ = _address(candidate, path)
        requested.append((path, proposed[key]))
        if prior[key] == proposed[key]:
            continue
        owner, target = _address(full, bindings[tuple(path)])
        owner[target] = proposed[key]

    if evidence["scope"] == "layout":
        return _snapshot(db, case_id, source.row, full)

    def verify(row):
        output = published_project_outputs(row, only_port=port)["outputs"].get(port)
        if output is None:
            raise ValueError("候选工程的光路未到达原观察面。请核对光路范围、介质和观察面位置。")
        for path, expected in requested:
            if path[:2] == ["system","elements"]:
                index = path[2]
                after = output["element_sources"]
                before = evidence["element_sources"][index]
                if index >= len(after) or after[index]["source_path"] != before["source_path"]:
                    raise ValueError("候选变化使这个参数对应到另一件元件。请在原工程核对观察面和元件。")
            owner, key = _address(output["value"], path)
            if owner[key] != expected:
                raise ValueError("候选参数不能同时满足原光路的坐标关系。请核对同一位置的各个取值。")

    return _snapshot(db, case_id, source.row, full, verify)


def search_measures(base):
    metrics = {}
    metric_labels = base["definition"].get("metrics", {})
    for path, label in metric_labels.items():
        if path in base["definition"].get("output_ports", {}):
            continue  # A declared physical result must actually have been published.
        if base["output"].get("status", "completed") == "completed" and _numeric(_output_value(base["output"], path)):
            metrics[path] = {"label":label, "unit":""}
    for path, port in base.get("available_outputs", {}).items():
        if port["shape"] == "scalar":
            # A study may publish several observables through the same compatible port type.
            label = metric_labels.get(path, port["label"]).removesuffix(" / " + port["unit"])
            address = _row_address(base["definition"],path)
            if address:
                table, index, _, spec = address
                rows = _output_value(base["output"],table)
                if isinstance(rows,list) and index < len(rows):
                    identity = [_output_value(rows[index],k) for k in spec.get("identity_fields",())]
                    label = " · ".join([label, *(str(v) for v in identity if v is not None)])
            metrics[path] = {"label":label, "unit":port["unit"]}
    return metrics


def _value(base, current, path, registry):
    if path in base.get("available_outputs", {}):
        key, _ = _rebind_port(base, current, path, registry)
        offer = current["available_outputs"][key]
        original = base["available_outputs"][path]
        if offer["shape"] != "scalar":
            raise ValueError("候选输出的表示不再是标量。")
        for condition in dict.fromkeys((*original.get("comparison_conditions", ()), *offer.get("comparison_conditions", ()))):
            before = original.get("condition_values", {}).get(condition)
            after = offer.get("condition_values", {}).get(condition)
            matches = (math.isclose(before, after, rel_tol=1e-12) if _numeric(before) and _numeric(after) else before == after)
            if before is None or not matches:
                name = (original.get("conditions", {}).get(condition) or offer["conditions"][condition])["label"]
                raise ValueError(f"候选的{name}与基线不一致（{before} / {after}），该数值不参与排名。")
        value = _output_value(current["output"],key)
        return PublishedPort.model_validate(offer).value_for(PublishedPort.model_validate(original), value)
    if current["output"].get("status", "completed") == "completed":
        value = _output_value(current["output"], path)
        if _numeric(value):
            return value
    raise ValueError(current["output"].get("message") or f"候选没有可用于比较的数值 {path}。")


def _rank_feasible(rows, measures):
    """Peel nondominated layers. Ties share a layer; incomparable goals need no numeric weights."""
    feasible = [r for r in rows if r["state"] == "feasible"]
    preferences = [measure for measure in measures if measure.direction]
    if not preferences:
        return

    def cost(row, measure):
        if measure.direction == "target":
            return Decimal(str(row["target_deviations"][measure.output]))
        value = row["values"][measure.output]
        return value if measure.direction == "min" else -value

    vectors = [[cost(row, measure) for measure in preferences] for row in feasible]
    successors = [[] for _ in feasible]
    predecessors = [0] * len(feasible)
    for i, first in enumerate(vectors):
        for j, second in enumerate(vectors):
            if all(a<=b for a,b in zip(first,second)) and any(a<b for a,b in zip(first,second)):
                successors[i].append(j)
                predecessors[j] += 1
    layer = [i for i,count in enumerate(predecessors) if count==0]
    rank = 1
    while layer:
        following = []
        for i in layer:
            feasible[i].update(rank=rank, preferred=rank==1)
            for j in successors[i]:
                predecessors[j] -= 1
                if predecessors[j]==0:
                    following.append(j)
        layer = following
        rank += 1


def _assess_row(row, measures, value_for):
    """Evaluate every selected measurement so one missing result does not hide the others."""
    issues, outside = {}, []
    for measure in measures:
        try:
            value = value_for(measure.output)
            if not _numeric(value):
                raise ValueError("换算到基线单位后没有可比较的有限数值。")
        except ValueError as exc:
            issues[measure.output] = str(exc)[:1800]
            continue
        row["values"][measure.output] = value
        if measure.direction == "target":
            row.setdefault("target_deviations", {})[measure.output] = _target_deviation(value, measure.target)
        if ((measure.lower is not None and _exceeds(measure.lower, value))
                or (measure.upper is not None and _exceeds(value, measure.upper))):
            outside.append(measure.output)
    if issues:
        row.update(measure_issues=issues, message="。".join(dict.fromkeys(issues.values()))[:1800])
    row.update(state="outside" if outside else "unavailable" if issues else "feasible", outside=outside)


def _save_search(db, case_id, report, rows, measures, task_id, *, creation_evidence=None):
    _rank_feasible(rows, measures)
    unavailable = sum(r["state"] == "unavailable" for r in rows)
    incomplete = sum(r["state"] == "unavailable" or bool(r.get("measure_issues")) for r in rows)
    preferred = [i for i, row in enumerate(rows) if row.get("preferred")]
    report.update(status="needs_input" if unavailable == len(rows) else "partial" if incomplete else "completed",
                  ranking_mode="pareto" if any(m.direction for m in measures) else "feasibility",
                  rows=rows, feasible=sum(r["state"] == "feasible" for r in rows), preferred=len(preferred),
                  preferred_indices=preferred, unavailable=unavailable, incomplete=incomplete,
                  outside=sum(r["state"] == "outside" for r in rows))
    return store_search(db, case_id, report, task_id, creation_evidence=creation_evidence)


def store_search(db, case_id, report, task_id=None, *, creation_evidence=None):
    """Store an immutable candidate table or evaluation, deduplicating concurrent saves."""
    if task_id and db.query(AgentTask.status).filter_by(case_id=case_id, id=task_id).with_for_update().scalar() != "running":
        raise ValueError("任务已停止，本次方案比较没有写入。")
    key = _key("study-grid:", report)
    row = _existing(db, case_id, key)
    if row is None:
        stored = {**report, **creation_evidence} if creation_evidence else report
        row = GeneratedContent(case_id=case_id, content_type="study_search", operation_key=key, content=stored)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = _existing(db, case_id, key)
            if row is None:
                raise
    return {"id": row.id, **row.content}


def grid_evaluator(db, case_id, args, registry, task_id=None, *, on_run=None):
    """Prepare the frozen input bindings once. Both grid searches and boundary probes use this evaluator."""
    stored = db.query(GeneratedContent).filter_by(id=args.run_id, case_id=case_id, content_type="scientific_study").one_or_none()
    if stored is None:
        raise ValueError("本案例中找不到所选的基线计算。")
    base = stored.content
    definition = base["definition"]
    entry = registry.get(definition["name"])
    if not entry or (entry[0].version,entry[1]) != (definition["version"],definition["provider"]):
        raise ValueError("需要安装基线计算所用的同一求解器版本。")
    study, provider = entry
    metrics = search_measures(base)
    if any(m.output not in metrics for m in args.measures):
        raise ValueError("请选择基线计算实际发布的数值评价量。")
    for measure in args.measures:
        _value(base,base,measure.output,registry)
    links = base.get("connections", {})
    paths = [path for axis in args.axes for path in axis.paths]
    varied_layouts, bindings = _layout_bounds(db, case_id, base, {p[0] for p in paths})
    invalid_path = next((p for p in paths if p[0] in bindings and tuple(p[1:]) not in bindings[p[0]]), None)
    for path in paths:
        parent, _ = _address(base["inputs"], path)
        root = path[0]
        if root in study.original_inputs:
            if (isinstance(parent,dict) and "attachment_id" in parent) or any(
                not original.get("input_paths") or any(path[:len(p)] == p for p in original["input_paths"])
                for original in base.get("originals", [])
            ):
                raise ValueError("原件引用须保持完整。请选择可调的分析参数。")
        if root in links:
            ref = links[root]
            if "revision_id" not in ref or study.input_ports[root].quantity != LAYOUT.quantity:
                raise ValueError(f"{root} 引用了已存的数值或测量。要改变来源，请使用换来源复算。")
            if not args.registration.strip():
                raise ValueError("请说明试算光路沿用的参考面、轴向及实验工况。")
    if len({v.row["revision_id"] for v in varied_layouts.values()}) < len(varied_layouts):
        raise ValueError("同一工程从多个输入进入了本计算。请只在一个输入中定义对它的修改。")

    solvers = {}
    report = {"baseline_id":args.run_id, "title":definition["title"], "recipe":args.model_dump(mode="json"),
              "baseline_solver_receipt":base.get("solver_receipt"), "solver_receipts":solvers,
              "measures":{m.output:metrics[m.output] for m in args.measures}}

    def evaluate(coordinates):
        if task_id and db.query(AgentTask.status).filter_by(case_id=case_id,id=task_id).scalar() != "running":
            raise ValueError("任务已停止，已完成的候选计算可从案例历史查看。")
        row = {"coordinates":coordinates, "values":{}}
        inputs = deepcopy(base["inputs"])
        for path, value in zip(paths, coordinates):
            parent, key = _address(inputs, path)
            parent[key] = value
        try:
            if invalid_path:
                field = " › ".join(map(str, invalid_path))
                raise ValueError(f"「{field}」不属于此模型可调的物理输入。请在原工程核对元件类型、锁定参数、来源或观察面的定位方式。")
            recipe = deepcopy(links)
            for root, project in varied_layouts.items():
                source_id = project.row["revision_id"]
                revision = _layout_candidate(db, case_id, project, links[root]["port"],
                    [path[1:] for path in paths if path[0] == root], bindings[root], base["inputs"][root], inputs[root])
                for ref in recipe.values():
                    if ref.get("revision_id") == source_id:
                        ref["revision_id"] = revision
                        if revision != source_id and ("registration" in ref or ref is recipe[root]):
                            ref["registration"] = args.registration
                            ref.pop("registration_from", None)
                if revision != source_id:
                    for match in study.condition_matches:
                        if root in (match.anchor, match.other) and match.other in recipe:
                            recipe[match.other]["registration"] = args.registration
                            recipe[match.other].pop("registration_from", None)
            raw = recorded_study_inputs(base, resolved=inputs, varied=(path[0] for path in paths))
            receipt = run_study(db.get_bind(),case_id,study,provider,raw,task_id,connections=recipe)
            record = receipt.pop("study_record")
            if on_run is not None:
                on_run(record)
            captured = record["solver_receipt"]
            solvers[captured["sha256"]] = captured
            row.update(run_id=record["id"], solver_sha256=captured["sha256"], science_status=receipt.get("status","completed"))
            current = {"output":receipt, "definition":record, "available_outputs":record["available_outputs"]}
            if receipt.get("status","completed") not in {"completed","partial"}:
                raise ValueError(receipt.get("message") or receipt.get("summary") or "候选超出模型适用范围。")
            _assess_row(row, args.measures, lambda path: _value(base, current, path, registry))
        except ValidationError as exc:
            row.update(state="unavailable", message=_validation_message(exc,definition.get("inputs",{})))
        except (ValueError, StudyFailure) as exc:
            row.update(state="unavailable", message=str(exc)[:1800])
        return row

    return report, evaluate


def run_search(bind, case_id, args, registry, task_id=None):
    with Session(bind) as db:
        report, evaluate = grid_evaluator(db, case_id, args, registry, task_id)
        rows = [evaluate([value for group in combination for value in group])
                for combination in product(*(axis.points() for axis in args.axes))]
        return _save_search(db, case_id, report, rows, args.measures, task_id)


def _original_grid(records, search, baseline_digest):
    source = search.content
    root_id = source.get("root_search_id")
    if root_id is None:
        return None, ""
    root = records.filter_by(content_type="study_search", id=root_id).one_or_none()
    if root is None:
        return None, f"本工程中缺少首轮搜索 #{root_id}，无法核对它原先绑定的读数。"
    original = root.content
    def grid(report):
        return {k: v for k, v in report["recipe"].items() if k != "measures"}
    digest = (original.get("baseline_solver_receipt") or {}).get("sha256")
    if (original.get("source_search_id") or original["baseline_id"] != source["baseline_id"]
            or grid(original) != grid(source) or (digest and digest != baseline_digest)):
        return None, f"首轮搜索 #{root_id} 的基线、方法或参数网格与当前记录不一致。"
    return original, ""


def _original_candidate(original, index, previous, actual_digest):
    """Only the same frozen sample and run can witness a previously bound detector row."""
    if index >= len(original["rows"]):
        return None
    first = original["rows"][index]
    if (actual_digest and first.get("solver_sha256") == actual_digest
            and previous.get("solver_sha256", actual_digest) == actual_digest
            and first.get("run_id") == previous.get("run_id") and first["coordinates"] == previous["coordinates"]
            and first.get("science_status", "completed") in ("completed", "partial")):
        return first
    return None


def _missing_frozen_identity(base, current, path):
    address = _row_address(base["definition"], path)
    if address is None or path not in base.get("available_outputs", {}):
        return False
    table, _, column, declaration = address
    previous, following = base["definition"], current["definition"]
    other = following.get("row_outputs", {}).get(table, {})
    return ((previous["name"], previous["provider"]) == (following["name"], following["provider"])
            and column in other.get("columns", {}) and (not declaration.get("identity_fields") or not other.get("identity_fields")))


def _archived_measure(path, base, current, previous, first, row, root_issue):
    old = previous["values"].get(path)
    if _numeric(old):
        return old
    try:
        return _value(base, current, path, {})
    except ValueError as exc:
        if not _missing_frozen_identity(base, current, path):
            raise
        if first:
            issue = first.get("measure_issues", {}).get(path)
            witness = first["values"].get(path)
            if issue:
                root_issue = f"首轮评价对此项记录了：{issue}"
            elif _numeric(witness):
                row.setdefault("root_measurements", []).append(path)
                return witness
        if root_issue:
            raise ValueError(f"{exc} {root_issue}") from exc
        raise


def reassess_search(bind, case_id, args, *, task_id=None):
    """Compare archived candidates. Only evidence frozen in this project may supply a value or a row identity."""
    with Session(bind) as db:
        records = db.query(GeneratedContent).filter_by(case_id=case_id)
        search = records.filter_by(content_type="study_search", id=args.search_id).one_or_none()
        if search is None:
            raise ValueError("本工程中找不到要重新评价的参数搜索。")
        source = search.content
        studies = records.filter_by(content_type="scientific_study")
        baseline = studies.filter_by(id=source["baseline_id"]).one_or_none()
        if baseline is None:
            raise ValueError("这组候选的基线计算已缺失，无法核对评价量。")
        base = baseline.content
        expected = (source.get("baseline_solver_receipt") or {}).get("sha256")
        baseline_digest = (base.get("solver_receipt") or {}).get("sha256")
        if expected and expected != baseline_digest:
            raise ValueError("基线计算的方法指纹与原搜索不一致，无法核对评价量。")
        original, root_issue = _original_grid(records, search, baseline_digest)
        metrics = {**source["measures"], **search_measures(base)}
        paths = [m.output for m in args.measures]
        if any(path not in metrics for path in paths):
            raise ValueError("请选择基线计算实际发布的数值评价量。")

        rows = []
        needs_output = defaultdict(list)
        candidate_ids = {row["run_id"] for row in source["rows"] if row.get("run_id")}
        content = GeneratedContent.content
        digests = dict(studies.filter(GeneratedContent.id.in_(candidate_ids)).with_entities(
            GeneratedContent.id, content["solver_receipt"]["sha256"].as_string()).all())
        for index, previous in enumerate(source["rows"]):
            row = {k: previous[k] for k in ("coordinates", "run_id", "solver_sha256", "science_status") if k in previous}
            row["values"] = {}
            rows.append(row)
            rid = previous.get("run_id")
            if not rid:
                row.update(state="unavailable", message=previous.get("message") or "该组合没有保存的候选计算。")
            elif rid not in digests:
                row.update(state="unavailable", message=f"本工程中缺少候选计算 #{rid}，无法核对来源。")
            elif row.get("solver_sha256") and row["solver_sha256"] != digests[rid]:
                row.update(state="unavailable", message=f"候选计算 #{rid} 的方法指纹与原搜索不一致。")
            elif row.get("science_status") in ("completed", "partial") and all(
                _numeric(previous["values"].get(path)) for path in paths
            ):
                _assess_row(row, args.measures, previous["values"].__getitem__)
            else:
                first = _original_candidate(original, index, previous, digests[rid]) if original else None
                note = root_issue
                if original and first is None:
                    note = "首轮搜索中同一采样位置、计算编号和方法指纹的候选无法核对。"
                needs_output[rid].append((row, previous, first, note))

        # A new objective may need archived detector tables. Stream only those candidates
        # and read neither the potentially large raw inputs nor the installed implementation.
        columns = (GeneratedContent.id, content["definition"], content["output"], content["available_outputs"])
        archived = studies.filter(GeneratedContent.id.in_(needs_output)).with_entities(*columns).yield_per(1)
        for rid, definition, output, offers in archived:
            current = {"definition": definition, "output": output, "available_outputs": offers or {}}
            status = output.get("status", "completed")
            for row, previous, first, note in needs_output[rid]:
                row["science_status"] = status
                if status not in ("completed", "partial"):
                    row.update(state="unavailable", message=output.get("message") or output.get("summary") or "候选超出模型适用范围。")
                    continue

                def recorded_value(path):
                    return _archived_measure(path, base, current, previous,
                                             first if original and path in original["measures"] else None, row, note)

                _assess_row(row, args.measures, recorded_value)

        report = {"baseline_id": source["baseline_id"], "title": source["title"], "source_search_id": search.id,
                  "root_search_id": source.get("root_search_id", search.id),
                  "recipe": {**source["recipe"], "measures": [m.model_dump(mode="json") for m in args.measures]},
                  "measures": {path: metrics[path] for path in paths},
                  "baseline_solver_receipt": source.get("baseline_solver_receipt"),
                  "solver_receipts": source.get("solver_receipts", {})}
        return _save_search(db, case_id, report, rows, args.measures, task_id)

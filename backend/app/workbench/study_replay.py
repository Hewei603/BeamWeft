"""Recalculate the affected part of an archived study after replacing one immutable source."""

from graphlib import CycleError, TopologicalSorter

from typing import Annotated

from pydantic import Field, model_validator
from sqlalchemy import null
from sqlalchemy.orm import Session

from ..models import AgentTask, GeneratedContent
from ..study_api import StrictInput
from ..version import provider_display_name
from .project_outputs import FrozenProjectOutputs
from .studies import StudyFailure, _output_value, installed_study_receipt, recorded_study_inputs, run_study


class StudyReplay(StrictInput):
    run_id: int = Field(gt=0, description="Final scientific study whose actual upstream records define the workflow.")
    source_id: int | None = Field(
        default=None, gt=0, description="One upstream project revision or study from the inspected workflow."
    )
    replacement_id: int | None = Field(
        default=None, gt=0, description="Alternative saved record of the same kind in this case."
    )
    execute: bool = Field(
        default=False,
        description="False inspects dependencies without writing. True saves new results for the affected steps.",
    )
    registrations: dict[int, dict[str, Annotated[str, Field(max_length=500)]]] = Field(
        default_factory=dict,
        description="Updated reference-frame or cross-condition explanations by original study ID and input field.",
    )

    @model_validator(mode="after")
    def selection(self):
        if (self.source_id is None) != (self.replacement_id is None) or (self.execute and self.source_id is None):
            raise ValueError("请同时选择原来源与替代来源。")
        if self.source_id is not None and self.source_id == self.replacement_id:
            raise ValueError("请选择不同的来源版本或计算。")
        return self


class StudyHistoryError(ValueError):
    """An archived source cannot be traced. The saved scientific output is still readable."""

    def __init__(self, message, *, invalid_bindings_at=None):
        super().__init__(message)
        self.invalid_bindings_at = invalid_bindings_at


def binding_edges(rid, bindings):
    """Read the source identities, independent of newer solver or file-column schemas."""
    parents, projects = set(), set()
    if bindings is None:
        return parents, projects  # Old records can lack binding metadata altogether.
    def malformed(field=None):
        where = f"的输入「{field}」" if field is not None else "的输入"
        return StudyHistoryError(f"计算 #{rid}{where}来源记录格式无效，无法核对依赖。仍可查看已存结果。",
                                 invalid_bindings_at=rid)
    if not isinstance(bindings, dict):
        raise malformed()
    for field, ref in bindings.items():
        if not isinstance(ref, dict):
            raise malformed(field)
        keys = {"run_id", "revision_id", "table"}.intersection(ref)
        if len(keys) != 1:
            raise malformed(field)
        key = keys.pop()
        if key == "table":
            if not isinstance(ref[key], dict):
                raise malformed(field)
            source_id = ref[key].get("attachment_id")
        else:
            source_id = ref[key]
            if not isinstance(ref.get("port"), str) or not ref["port"].strip():
                raise malformed(field)
        if type(source_id) is not int or not 0 < source_id <= 2**63 - 1:
            raise malformed(field)
        if key == "run_id":
            parents.add(source_id)
        elif key == "revision_id":
            projects.add(source_id)
    return parents, projects


def _history(db, case_id, root, *, with_receipts=False):
    """Read edge lists and method headers in batches; arrays are loaded only for steps actually used."""
    definition = GeneratedContent.content["definition"]
    query = db.query(
        GeneratedContent.id,
        GeneratedContent.content["connections"],
        *(definition[k].as_string() for k in ("name", "title", "provider", "version")),
        definition["registration_inputs"], GeneratedContent.content["solver_receipt"] if with_receipts else null(),
    ).filter_by(case_id=case_id, content_type="scientific_study")
    steps, edges, projects = {}, {}, set()
    pending = {root}
    while pending:
        batch = sorted(pending)[:256]
        pending.difference_update(batch)
        rows = query.filter(GeneratedContent.id.in_(batch)).all()
        if len(rows) != len(batch):
            raise StudyHistoryError("此案例缺少所选计算或其依赖记录，无法还原这条计算链。")
        for rid, refs, name, title, provider, version, shared, saved in rows:
            bindings_recorded = refs is not None
            parents, bound_projects = binding_edges(rid, refs)
            refs = refs or {}
            if shared is not None and (not isinstance(shared, dict) or not all(isinstance(field, str) for field in shared.values())):
                raise StudyHistoryError(f"计算 #{rid} 的工况说明记录格式无效，无法还原这条计算链。")
            steps[rid] = dict(id=rid, name=name, title=title, provider=provider, version=version, connections=refs,
                              bindings_recorded=bindings_recorded, bound_projects=bound_projects, solver_receipt=saved,
                              registration_inputs={key: text for key, text in (shared or {}).items() if key in refs})
            edges[rid] = parents
            projects.update(bound_projects)
            pending.update(edges[rid] - steps.keys())
        pending.difference_update(steps)
    try:
        order = list(TopologicalSorter(edges).static_order())
    except CycleError as exc:
        raise StudyHistoryError("已存计算含循环引用，无法排出计算顺序。") from exc
    return [steps[rid] for rid in order], projects


def _project_sources(db, case_id, ids):
    if not ids:
        return []
    rows = db.query(GeneratedContent.id, GeneratedContent.content_type, GeneratedContent.content["project"]["title"].as_string()).filter(
        GeneratedContent.case_id == case_id, GeneratedContent.id.in_(ids),
        GeneratedContent.content_type.in_(("optical_project", "optical_snapshot")),
    ).order_by(GeneratedContent.id).all()
    if len(rows) != len(ids):
        raise StudyHistoryError("计算链引用的工程版本在此案例中已缺失。")
    return [{"id": rid, "title": title, "model_kind": "draft" if kind == "optical_snapshot" else "saved"}
            for rid, kind, title in rows]


def study_project_basis(db, case_id, run_id, connections):
    """Describe saved bindings without comparing model values or inferring provenance from handwritten inputs."""
    missing = "历史记录缺少输入绑定，工程来源无法完整核对。"
    if connections is None:
        return {"complete": False, "projects": [], "message": missing}
    try:
        parents, projects = binding_edges(run_id, connections)
        steps = [{"id": run_id, "bound_projects": projects, "bindings_recorded": True}]
        if parents:
            steps, projects = _history(db, case_id, run_id)
        sources = _project_sources(db, case_id, projects)
    except StudyHistoryError as exc:
        return {"complete": False, "projects": [], "message": str(exc),
                **({"invalid_bindings_at": exc.invalid_bindings_at} if exc.invalid_bindings_at is not None else {})}
    consumers = {rid: [] for rid in projects}
    for step in steps:
        for rid in step["bound_projects"]:
            consumers[rid].append(step["id"])
    for source in sources:
        source["used_by"] = sorted(consumers[source["id"]])
    complete = all(step["bindings_recorded"] for step in steps)
    return {"complete": complete, "projects": sources, **({} if complete else {"message": missing})}


def _row_address(definition, port):
    for table, spec in definition.get("row_outputs", {}).items():
        if port.startswith(table + "."):
            index, _, column = port[len(table) + 1 :].partition(".")
            if index.isdecimal() and column in spec["columns"]:
                return table, int(index), column, spec
    return None


def _identity_indices(rows, fields, at):
    if at is None or any(type(v) not in (str, float, int) or v == "" for v in at):
        return []
    matches = []
    for index, row in enumerate(rows):
        values = tuple(_output_value(row, field) for field in fields)
        if values == at and all(type(v) in (str, float, int) for v in values):
            matches.append(index)
    return matches


def _rebind_port(old, new, port, registry):
    """Row numbers are local to a run. Only the author's stable physical coordinates can cross runs."""
    previous, following = old["definition"], new["definition"]
    original = old.get("available_outputs", {}).get(port)
    address = _row_address(previous, port)
    new_port = port
    match = None
    if address:
        if (previous["name"], provider_display_name(previous["provider"])) != (following["name"], provider_display_name(following["provider"])):
            raise ValueError("这两个方法没有共同声明跨方法的结果行标识。请在替代结果中选择对应的物理量。")
        table, index, column, spec = address
        new_spec = following.get("row_outputs", {}).get(table, {})

        # A recorded version may predate identity metadata. Only its exact installed producer can supply it.
        def identities(definition, declaration):
            if declaration.get("identity_fields"):
                return tuple(declaration["identity_fields"])
            entry = registry.get(definition["name"])
            if entry and (entry[0].version, entry[1]) == (definition["version"], provider_display_name(definition["provider"])):
                installed = entry[0].row_outputs.get(table)
                return installed.identity_fields if installed else ()
            return ()

        fields = identities(previous, spec)
        if not fields or fields != identities(following, new_spec) or column not in new_spec.get("columns", {}):
            raise ValueError("结果表缺少一致的跨版本行标识。请在新版结果中重新选择测点或元件。")
        old_rows = _output_value(old["output"], table) or []
        new_rows = _output_value(new["output"], table) or []
        coords = tuple(_output_value(old_rows[index], f) for f in fields) if index < len(old_rows) else None
        matches = _identity_indices(new_rows, fields, coords)
        if len(matches) != 1 or _identity_indices(old_rows, fields, coords) != [index]:
            raise ValueError(f"结果表 {table} 中标识 {coords} 的测点或元件缺失或重复。请核对两次结果。")
        new_port = f"{table}.{matches[0]}.{column}"
        match = {"coordinates": dict(zip(fields, coords)), "old_row": index + 1, "new_row": matches[0] + 1}
    elif port not in previous.get("output_ports", {}) or port not in following.get("output_ports", {}):
        raise ValueError(f"两次计算未共同声明固定输出 {port}。请重新选择输出。")
    offer = new.get("available_outputs", {}).get(new_port)
    if not offer:
        assessment = new["output"].get("output_assessments", {}).get(new_port, {})
        reason = assessment.get("message") or new["output"].get("message")
        raise ValueError(f"替代计算未发布可用输出 {new_port}。{reason or '请查看新计算的适用条件。'}")
    if not original or original["quantity"] != offer["quantity"]:
        raise ValueError(f"替代计算未发布相同含义的可用输出 {new_port}。请查看新计算的适用条件。")
    return new_port, match


def replay_study(bind, case_id, args, registry, task_id=None):
    with Session(bind) as db:
        steps, project_ids = _history(db, case_id, args.run_id, with_receipts=True)
        note_fields = {step["id"]: set(step["connections"]) | set(step["registration_inputs"].values()) for step in steps}
        for rid, notes in args.registrations.items():
            if rid not in note_fields or notes.keys() - note_fields[rid]:
                raise ValueError(f"工况说明须对应链中实际存在的计算及其来源字段，请核对计算 #{rid}。")
        upstream = {s["id"]: s for s in steps if s["id"] != args.run_id}
        sources = [dict(id=rid, kind="study", title=s["title"]) for rid, s in upstream.items()]
        sources.extend(dict(id=item["id"], kind="project", title=item["title"])
                       for item in _project_sources(db, case_id, project_ids))
        source, replacement = args.source_id, args.replacement_id
        project = source in project_ids
        if source is not None:
            if source not in upstream and not project:
                raise ValueError("所选原来源不在这条计算链中。")
            kinds = ("optical_project", "optical_snapshot") if project else ("scientific_study",)
            if (
                not db.query(GeneratedContent.id)
                .filter(
                    GeneratedContent.case_id == case_id,
                    GeneratedContent.id == replacement,
                    GeneratedContent.content_type.in_(kinds),
                )
                .scalar()
            ):
                raise ValueError("替代来源须为本案例中同类型的已保存记录。")
        outputs = FrozenProjectOutputs(db, case_id, replacement) if project else None
        report = {
            "run_id": args.run_id,
            "source_id": source,
            "replacement_id": replacement,
            "sources": sources,
            "steps": [],
            "result_id": None,
            "status": "preview",
        }
        affected, mapped, failed = set(), {}, set()
        records = {}

        def record(rid):
            if rid not in records:
                records[rid] = (
                    db.query(GeneratedContent.content)
                    .filter_by(case_id=case_id, id=rid, content_type="scientific_study")
                    .one()[0]
                )
            return records[rid]

        for step in steps:
            rid, refs = step["id"], step["connections"]
            changed = [
                field
                for field, ref in refs.items()
                if ("revision_id" in ref and project and ref["revision_id"] == source)
                or ("run_id" in ref and ref["run_id"] in affected)
            ]
            row = {
                "id": rid,
                "title": step["title"],
                "action": "reuse",
                "result_id": rid,
                "parents": sorted({r["run_id"] for r in refs.values() if "run_id" in r}),
                "projects": sorted({r["revision_id"] for r in refs.values() if "revision_id" in r}),
            }
            report["steps"].append(row)
            if rid == source:
                affected.add(rid)
                mapped[rid] = replacement
                row.update(action="replace", result_id=replacement)
                continue
            if not changed:
                continue
            affected.add(rid)
            row.update(action="recalculate", result_id=None, changed_inputs=changed)
            row["saved_solver_receipt"] = step["solver_receipt"]
            row["installed_solver_receipt"] = installed_study_receipt(step, step["solver_receipt"], registry)
            if failed.intersection(row["parents"]):
                row["action"] = "waiting"
                failed.add(rid)
                continue
            try:
                entry = registry.get(step["name"])
                if not entry or (entry[0].version, entry[1]) != (step["version"], provider_display_name(step["provider"])):
                    raise ValueError(f"缺少原计算使用的 {provider_display_name(step['provider'])} / {step['name']} v{step['version']}。")
                study = entry[0]
                links = {k: dict(v) for k, v in refs.items()}
                annotations = args.registrations.get(rid, {})
                notes = {}
                shared = set(step["registration_inputs"].values())
                if shared:
                    old_inputs = record(rid)["inputs"]
                    properties = study.inputs.model_json_schema()["properties"]
                    notes.update({key: {"label":properties[key].get("title",key), "previous":old_inputs.get(key), "required":True}
                                  for key in sorted(shared)})
                for field, ref in links.items():
                    target = study.input_ports.get(field)
                    if target is None:
                        raise ValueError(f"已安装方法缺少原记录的输入接口 {field}。")
                    # A note about the old experimental relation must not waive checks on a new combination.
                    old_note = ref.pop("registration", "")
                    inherited = ref.pop("registration_from", None)
                    if not inherited and (old_note or target.shape == "state" or any(m.other == field for m in study.condition_matches)):
                        notes[field] = {
                            "label": target.registration_label or target.label,
                            "previous": old_note,
                            "required": target.shape == "state",
                        }
                    if annotations.get(field):
                        ref["registration"] = annotations[field]
                    if field not in changed:
                        continue
                    if "revision_id" in ref:
                        if outputs.get(ref["port"]) is None:
                            raise ValueError(
                                f"新版缺少输入「{target.label}」所用的工程量 {ref['port']}。请核对元件和端口。"
                            )
                        ref["revision_id"] = replacement
                    elif ref["run_id"] in mapped:
                        prior, new = ref["run_id"], mapped[ref["run_id"]]
                        port, match = _rebind_port(record(prior), record(new), ref["port"], registry)
                        ref.update(port=port, run_id=new)
                        if match:
                            row.setdefault("row_matches", []).append({"input": field, "label": target.label, **match})
                if notes:
                    row["registrations"] = notes
                if not args.execute:
                    continue
                if (
                    task_id is not None
                    and db.query(AgentTask.status).filter_by(id=task_id, case_id=case_id).scalar() != "running"
                ):
                    raise ValueError("任务已停止，已保存的上游记录可供继续复算。")
                archived = record(rid)
                raw = recorded_study_inputs(archived)
                for basis in shared:
                    if not annotations.get(basis, "").strip():
                        raise ValueError(f"换用新来源后，请重新确认「{notes[basis]['label']}」。")
                    raw[basis] = annotations[basis]
                for field in study.registration_inputs.keys() & links.keys():
                    if field in notes and not annotations.get(field, "").strip():
                        raise ValueError(f"请核对「{notes[field]['label']}」先前单独登记的配准说明。")
                result = run_study(bind, case_id, *entry, raw, task_id, connections=links)
                mapped[rid] = result["study_record"]["id"]
                row.update(action="computed", result_id=mapped[rid], science_status=result.get("status", "completed"),
                           execution=result["study_record"]["execution"], result_solver_receipt=result["study_record"]["solver_receipt"])
            except (ValueError, StudyFailure) as exc:
                row.update(action="unavailable", message=str(exc))
                failed.add(rid)
        if args.execute:
            report["result_id"] = mapped.get(args.run_id)
            report["status"] = (
                "completed"
                if report["result_id"]
                else "partial"
                if any(r["action"] == "computed" for r in report["steps"])
                else "needs_input"
            )
            if report["result_id"]:
                old, new = record(args.run_id), record(report["result_id"])
                report["status"] = new["output"].get("status", "completed")
                report["comparison"] = [
                    {
                        "label": label,
                        "previous": _output_value(old["output"], path),
                        "current": _output_value(new["output"], path),
                    }
                    for path, label in old["definition"].get("metrics", {}).items()
                    if all(
                        type(_output_value(rec["output"], path)) in (int, float, str, bool, type(None))
                        for rec in (old, new)
                    )
                ]
        return report

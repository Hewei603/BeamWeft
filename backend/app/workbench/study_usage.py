"""Find saved calculations derived from a case source using their frozen input bindings."""
from collections import defaultdict, deque
from graphlib import CycleError, TopologicalSorter
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import literal, select
from sqlalchemy.orm import Session

from ..study_compat import port_conditions
from ..models import Attachment, GeneratedContent
from ..study_api import StrictInput
from .project_outputs import FrozenProjectOutputs
from .scoped_parameters import parameter_origins
from .study_replay import StudyHistoryError, binding_edges


class StudyUsage(StrictInput):
    source_id: int | None = Field(default=None, gt=0, description="Saved optical project, snapshot, or scientific study ID in this case.")
    attachment_id: int | None = Field(default=None, gt=0, description="Archived file ID in this case, for original-byte, table-column, or recorded scenario-weight evidence.")
    compare_revision_id: int | None = Field(default=None, gt=0, description="For a project source, compare its actually consumed outputs with this other frozen project.")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)
    view: Literal["all", "terminal"] = "all"

    @model_validator(mode="after")
    def one_source(self):
        if (self.source_id is None) == (self.attachment_id is None):
            raise ValueError("请选择一份已存工程、计算或实验原件作为来源。")
        if self.compare_revision_id is not None and (self.attachment_id is not None or self.source_id == self.compare_revision_id):
            raise ValueError("比较需要选择两个不同的已存工程版本。")
        return self


def _port_change(previous, current, port):
    old, new = (outputs.get(port) for outputs in (previous, current))
    if old is None or new is None:
        return {"state": "unavailable", "missing": "previous" if old is None else "replacement"}
    left, right = port_conditions(old["port"]), port_conditions(new["port"])
    old_conditions = left.get("condition_values", {})
    new_conditions = right.get("condition_values", {})
    rules = {**left.get("conditions", {}), **right.get("conditions", {})}
    differences = [
        {"key": key, "label": rules.get(key, {}).get("label", key),
         "previous": old_conditions.get(key), "current": new_conditions.get(key)}
        for key in sorted(old_conditions.keys() | new_conditions.keys())
        if old_conditions.get(key) != new_conditions.get(key)
    ]
    reference_keys = {key for key in rules if all(p.get("conditions", {}).get(key, {}).get("reference_only") for p in (left, right))}
    condition_changes = [change for change in differences if change["key"] not in reference_keys]
    references = [change for change in differences if change["key"] in reference_keys]
    old_owners, new_owners = parameter_origins(old), parameter_origins(new)
    for key in sorted(old_owners.keys() & new_owners.keys()):
        label, before = old_owners[key]
        after = new_owners[key][1]
        if before != after:
            condition_changes.append({"key":key, "label":label, "previous":before, "current":after})
    same_basis = not condition_changes and all(left.get(key) == right.get(key) for key in ("quantity", "shape", "unit", "scale", "conditions"))
    same_value = old["value"] == new["value"]
    state = "same" if same_basis else "conditions"
    result = {"state": state if same_value else "changed",
              "value_changed": not same_value, "basis_changed": not same_basis, "label": left["label"],
              "condition_changes": condition_changes}
    if references:
        result["reference_changes"] = references
    if left["shape"] in {"scalar", "interval"} and right["shape"] in {"scalar", "interval"}:
        result.update(previous=old["value"], current=new["value"], previous_unit=left["unit"], current_unit=right["unit"])
    return result


def study_usage(bind, case_id, args):
    with Session(bind) as db:
        return _find_usage(db, case_id, args)


def _file_receipts(record_id, originals, attachment_id, unreadable):
    if originals is None:
        return
    if not isinstance(originals, list):
        unreadable.add(record_id)
        return
    for receipt in originals:
        if not isinstance(receipt, dict) or type(receipt.get("attachment_id")) is not int:
            unreadable.add(record_id)
        elif receipt["attachment_id"] == attachment_id:
            yield receipt


def _find_usage(db, case_id, args):
    record = GeneratedContent
    if args.attachment_id is not None:
        source = db.execute(select(Attachment.id, Attachment.filename).where(
            Attachment.case_id == case_id, Attachment.id == args.attachment_id)).one_or_none()
        kind = "file"
    else:
        source = db.execute(select(record.id, record.content_type).where(record.case_id == case_id, record.id == args.source_id,
            record.content_type.in_(("scientific_study", "optical_project", "optical_snapshot")))).one_or_none()
        kind = "study" if source and source.content_type == "scientific_study" else "project"
    if source is None:
        raise ValueError("此案例中找不到所选来源。")
    comparison = None
    if args.compare_revision_id is not None:
        if kind != "project":
            raise ValueError("只有工程版本之间可以对照已用的工程量。")
        comparison = [FrozenProjectOutputs(db, case_id, rid) for rid in (source.id, args.compare_revision_id)]

    # Inspect just the headers and bindings, even when the archived results contain large field arrays.
    headers = select(record.id, record.content[("definition", "title")].as_string(), record.content["connections"],
                     record.content["originals"] if kind == "file" else literal(None)).where(
                         record.case_id == case_id, record.content_type == "scientific_study")
    children, direct, titles = defaultdict(set), defaultdict(list), {}
    unreadable = set()
    for rid, title, connections, originals in db.execute(headers).yield_per(256):
        titles[rid] = title
        try:
            parents, _ = binding_edges(rid, connections)
        except StudyHistoryError:
            # Other records and independent original-byte receipts remain inspectable.
            connections, parents = {}, set()
            unreadable.add(rid)
        for parent in parents:
            children[parent].add(rid)
        for field, ref in (connections or {}).items():
            if ((kind == "project" and ref.get("revision_id") == source.id)
                or (kind == "study" and ref.get("run_id") == source.id)):
                direct[rid].append({"input": field, "port": ref["port"]})
            elif kind == "file" and ref.get("table", {}).get("attachment_id") == source.id:
                direct[rid].append({"input": field, "column": ref["column"]})
        for original in _file_receipts(rid, originals, source.id, unreadable):
            paths = original.get("input_paths") or []
            if not isinstance(paths, list) or not all(isinstance(path, list) and all(type(key) in (str, int) for key in path)
                                                       for path in paths):
                unreadable.add(rid)
                paths = []
            direct[rid].extend({"input_path": path} for path in paths or [None])

    # A distribution can cite an original independently of the optical calculation
    # supplying its readings. Its saved comparison owns that citation.
    evaluations = {}
    if kind == "file":
        records = select(record.id, record.content["baseline_id"], record.content["title"].as_string(),
                         record.content[("adjustment_evaluation", "distribution", "originals")]).where(
                             record.case_id == case_id, record.content_type == "study_search")
        for rid, baseline, title, originals in db.execute(records).yield_per(256):
            for ref in _file_receipts(rid, originals, source.id, unreadable):
                direct[rid].append({"input_label": "工况权重依据",
                                    **{key: ref[key] for key in ("relative_path", "sha256") if isinstance(ref.get(key), str)}})
            if rid in direct:
                view = {"run_id": baseline, "comparison_id": rid} if type(baseline) is int and baseline in titles else None
                evaluations[rid] = {"content_type": "study_search", "title": title, "workbench_view": view}
                if view is None:
                    evaluations[rid]["navigation_issue"] = "此评价所用的基准计算已不可用。"

    # Walk forward from real bindings. IDs need not be in topological order in an imported case.
    parents = {rid: set() for rid in direct}
    pending = deque(direct)
    while pending:
        parent = pending.popleft()
        for child in children.get(parent, ()):
            if child not in parents:
                parents[child] = set()
                pending.append(child)
            parents[child].add(parent)
    try:
        order = list(TopologicalSorter(parents).static_order())
    except CycleError as exc:
        raise ValueError("来源涉及的已存计算含循环引用，无法核对依赖顺序。") from exc
    states, changes = {}, {}
    for rid in order:
        state = "linked"
        if comparison is not None:
            inputs = direct.get(rid, ())
            for binding in inputs:
                port = binding["port"]
                if port not in changes:
                    changes[port] = _port_change(*comparison, port)
                binding.update(changes[port])
            # Prefer a direct explanation. Each input and upstream path remains inspectable.
            state = "review" if any(states[p] != "same" for p in parents[rid]) else "same"
            direct_states = {item["state"] for item in inputs}
            for reason in ("unavailable", "changed", "conditions"):
                if reason in direct_states:
                    state = reason
                    break
        states[rid] = state
    terminals = {rid for rid in parents if not children.get(rid)}
    listed = sorted(terminals if args.view == "terminal" else parents, key=lambda rid: (rid not in terminals, -rid))
    end = args.offset + args.limit
    page = listed[args.offset:end]
    # Read human-facing input labels for this page only. Old definitions may have none.
    visible = [rid for rid in page if rid in direct and rid not in evaluations]
    if visible:
        labels = select(record.id, record.content[("definition", "input_ports")]).where(
            record.case_id == case_id, record.id.in_(visible))
        for rid, ports in db.execute(labels):
            for binding in direct[rid]:
                label = (ports or {}).get(binding.get("input"), {}).get("label")
                if label:
                    binding["input_label"] = label
    return {"source_id": source.id, "kind": kind, "compare_revision_id": args.compare_revision_id,
            **({"unreadable_binding_count": len(unreadable)} if unreadable else {}),
            "total": len(parents), "direct_count": len(direct), "terminal_count": len(terminals),
            "review_count": sum(s != "same" for s in states.values()) if comparison is not None else None,
            "next_offset": end if end < len(listed) else None,
            "items": [{"id": rid, "title": titles.get(rid), **evaluations.get(rid, {}), "state": states[rid], "terminal": rid in terminals,
                       "inputs": direct.get(rid, []), "via": sorted(parents[rid]),
                       "upstream_needs_review": comparison is not None and any(states[p] != "same" for p in parents[rid])}
                      for rid in page]}

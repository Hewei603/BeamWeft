"""Editable models actually written by a task, including historical runs."""
from collections import defaultdict

from ..models import AgentTask, AgentToolCall, GeneratedContent


MODEL_WRITE_TOOLS = frozenset({
    "save_optical_project", "place_component_model", "import_design_project", "bind_measured_launch",
    "adopt_optical_snapshot", "replace_component", "replace_crystal",
})


def _writes(db, case_id, *columns):
    call, model = AgentToolCall, GeneratedContent
    return (db.query(*columns).select_from(call)
            .join(AgentTask, AgentTask.id == call.task_id)
            .join(model, model.id == call.output_json["revision_id"].as_integer())
            .filter(AgentTask.case_id == case_id, model.case_id == case_id, AgentTask.mode == "workbench",
                    call.tool_name.in_(MODEL_WRITE_TOOLS), call.status.in_(("completed", "partial", "needs_input")),
                    model.content_type.in_(("optical_project", "optical_snapshot"))))


def last_model_run(db, case_id, before):
    task = (_writes(db, case_id, AgentTask.id, AgentTask.status).filter(AgentTask.id < before)
            .order_by(AgentTask.id.desc()).first())
    if task:
        return {"id": task.id, "status": task.status,
                **editable_results(db, case_id, [task.id], follow_edits=True)[task.id]}


def editable_results(db, case_id, task_ids, *, follow_edits=False):
    call, model = AgentToolCall, GeneratedContent
    ids = sorted(set(task_ids))
    results = {}
    # Project identifiers and labels; scans and field arrays can be several MB per call.
    for offset in range(0, len(ids), 400):
        rows = (_writes(db, case_id, call.task_id, model.id, model.content_type,
                         model.content["variant_name"].as_string(), model.content["parent_revision_id"].as_integer(),
                         model.content["source_revision_id"].as_integer())
                .filter(call.task_id.in_(ids[offset:offset+400]))
                .order_by(call.id))
        for task_id, revision, content_type, label, parent, source in rows:
            kind = "draft" if content_type == "optical_snapshot" else "saved"
            outputs = results.setdefault(task_id, {})
            previous = source if kind == "draft" else parent
            predecessor = outputs.get(previous)
            if predecessor and predecessor["kind"] == kind and predecessor.get("variant_name") == label:
                del outputs[previous]
            outputs[revision] = {"kind": kind, "revision_id": revision, **({"variant_name": label} if label else {})}
    receipts = {}
    for task, outputs in results.items():
        models = list(outputs.values())
        receipts[task] = {"editable_model": models[-1], "editable_models": models}
    if follow_edits:
        canvas_models(db, case_id, receipts)
    return receipts


def canvas_models(db, case_id, receipts, *, whole_project=False):
    """Resolve saved alternatives once for this response, through any intermediate calculation snapshots."""
    roots = {item["revision_id"] for receipt in receipts.values() for item in receipt["editable_models"]
             if item["kind"] == "draft"}
    if not roots and not whole_project:
        return []
    model = GeneratedContent
    rows = (db.query(model.id, model.content["source_revision_id"].as_integer(), model.content["variant_name"].as_string(),
                     model.content["saved_variant"].as_boolean(), model.operation_key)
            .filter(model.case_id == case_id, model.content_type == "optical_snapshot")
            .order_by(model.id))
    if not whole_project:
        rows = rows.filter(model.id >= min(roots))
    # Legacy Agent writes have a persisted tool receipt. Legacy manual saves have an idempotency key.
    authored = {r.id for r in _writes(db, case_id, model.id).filter(model.content_type == "optical_snapshot")}
    nearest = {}
    refs = {}
    children = defaultdict(list)
    choices = {}
    for revision, parent, name, saved, key in rows:
        ancestor = nearest.get(parent)
        if not (saved or revision in authored or (key or "").startswith(
                ("variant:", "design:", "design-geometry:", "study-layout:"))):
            nearest[revision] = ancestor
            continue
        ref = {"kind": "draft", "revision_id": revision, **({"variant_name": name} if name else {})}
        refs[revision] = ref
        nearest[revision] = revision
        choices[revision] = ref
        if ancestor is not None:
            children[ancestor].append(revision)
            if refs[ancestor].get("variant_name") == name:
                choices.pop(ancestor, None)
    for receipt in receipts.values():
        options = {}
        for ref in receipt["editable_models"]:
            revision = ref["revision_id"]
            if revision not in refs:
                options[revision] = ref
                continue
            pending = [revision]
            while pending:
                current = pending.pop()
                if current in choices:
                    options[current] = choices[current]
                pending.extend(children[current])
        receipt["canvas_models"] = [options[key] for key in sorted(options)]
    return list(choices.values())

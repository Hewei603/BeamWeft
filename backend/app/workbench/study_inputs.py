"""Immutable model inputs for a question, including unsaved what-if studies."""
import hashlib
import json
from types import SimpleNamespace

from pydantic import Field, field_validator, model_validator
from sqlalchemy.exc import IntegrityError

from ..models import GeneratedContent
from ..solver_receipt import solver_cache_key
from .authoring import layout_receipt
from .optics import StrictInput
from .projects import (
    OpticalProject, ProjectRevisionConflict, ReadProject, SaveProject, _sync_saved_project,
    evaluate_project, normalize_project, read_project, read_project_inputs, save_project, validate_locked_parts,
)


class WorkingDraft(StrictInput):
    project: OpticalProject
    source_revision_id: int | None = Field(default=None, gt=0)
    base_revision_id: int | None = Field(default=None, gt=0)


class VariantLabel(StrictInput):
    variant_name: str | None = Field(default=None, max_length=160, description="Optional name for this independent design alternative.")

    @field_validator("variant_name")
    @classmethod
    def trim_name(cls, value):
        return (value or "").strip() or None


class SaveVariant(WorkingDraft, VariantLabel):
    pass


class StudyInputRef(StrictInput):
    run_id: int = Field(gt=0)
    input_key: str = Field(min_length=1, max_length=200)


class ChooseStudyInput(VariantLabel):
    input_key: str = Field(min_length=1, max_length=200)


class BeginStudy(ReadProject, VariantLabel):
    study_input: StudyInputRef | None = Field(default=None, description="An exact layout input listed in a saved scientific study's layout_inputs.")

    @model_validator(mode="after")
    def one_source(self):
        if self.study_input and (self.revision_id or self.case_head):
            raise ValueError("请选择计算输入或工程版本中的一个来源。")
        return self


class AdoptSnapshot(StrictInput):
    snapshot_id: int = Field(gt=0)
    base_revision_id: int | None = Field(default=None, gt=0)
    source_quote: str = Field(min_length=1, max_length=2000, description="Current user's words instructing adoption into the official case project.")


def basis(revision):
    if revision is None:
        return {"kind": "empty", "revision_id": None}
    return {
        "kind": revision.get("model_kind", "saved"),
        "revision_id": revision["revision_id"],
        "source_revision_id": revision.get("source_revision_id"),
        "base_revision_id": revision.get("base_revision_id"),
        **({"variant_name": revision["variant_name"]} if revision.get("variant_name") else {}),
        **({"calculation_status": revision["analysis"]["status"],
            "input_issues": revision["analysis"]["input_issues"]} if revision.get("analysis", {}).get("input_issues") else {}),
    }


def draft_source(draft, models):
    for revision_id in {draft.source_revision_id, draft.base_revision_id} - {None}:
        revision = models[revision_id]
        if revision_id == draft.base_revision_id and revision.get("model_kind") == "draft":
            raise ValueError("草稿的基线必须是已存工程版本。")
        if revision.get("model_kind") == "draft" and revision.get("base_revision_id") != draft.base_revision_id:
            raise ValueError("继续试算时需要保留来源快照的工程基线。请在采用时核对新版本。")
    return models.get(draft.source_revision_id or draft.base_revision_id)


def capture_snapshot(db, case_id, draft, note="提问时的光路草稿", *, operation_key=None, variant_name=None,
                     preserve_locks=False, study_input=None, saved_variant=False):
    models = read_project_inputs(db, case_id, (draft.source_revision_id, draft.base_revision_id))
    source = draft_source(draft, models)
    if preserve_locks and source:
        validate_locked_parts(source["project"], draft.project)
    data = evaluate_project(db, draft.project, source)
    data.update(model_kind="draft", base_revision_id=draft.base_revision_id, source_revision_id=draft.source_revision_id,
                change_note=note, parent_revision_id=draft.source_revision_id)
    if saved_variant:
        data["saved_variant"] = True
    name = variant_name or (source or {}).get("variant_name")
    if name:
        data["variant_name"] = name
    if study_input is None and source:
        study_input = source.get("study_input")
    if study_input is not None:
        data["study_input"] = study_input
    row = GeneratedContent(case_id=case_id, content_type="optical_snapshot", content=data,
                           operation_key=operation_key, prompt_version="optical-snapshot-v1")
    db.add(row)
    db.flush()
    return {"status": "completed", "revision_id": row.id, **data}


def snapshot_request_key(prefix, identity):
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return prefix + hashlib.sha256(encoded).hexdigest()


def snapshot_operation_key(db, case_id, draft, prefix, request_identity):
    model = draft.project
    parent = draft.source_revision_id or draft.base_revision_id
    if parent and "network" not in model.model_fields_set:
        source = read_project_inputs(db, case_id, [parent])[parent]
        model = normalize_project(model, source)
    identity = [request_identity, solver_cache_key(layout_receipt(model))]
    return snapshot_request_key(prefix, identity)


def save_variant(db, case_id, request, *, operation_key=None, note="保存独立光路方案", study_input=None):
    if request.source_revision_id != request.base_revision_id:
        source = read_project(db, case_id, request.source_revision_id) if request.source_revision_id else None
        if not source or source.get("model_kind") != "draft" or source.get("base_revision_id") != request.base_revision_id:
            raise ValueError("方案的基线必须来自所选版本。请重新打开来源光路。")
    # Include omitted-vs-explicit fields because normalization can inherit omitted values.
    payload = request.model_dump(mode="json", exclude_unset=True)
    key = operation_key or snapshot_operation_key(db, case_id, request, "variant:", payload)
    lookup = db.query(GeneratedContent).filter_by(case_id=case_id, operation_key=key, content_type="optical_snapshot")
    previous = lookup.one_or_none()
    if previous is None:
        try:
            saved = capture_snapshot(db, case_id, request, note, operation_key=key, variant_name=request.variant_name,
                                     study_input=study_input, saved_variant=True)
            db.commit()
            return saved
        except IntegrityError:
            db.rollback()
            previous = lookup.one_or_none()
            if previous is None:
                raise
    return {"status": "completed", **read_project(db, case_id, previous.id)}


def begin_study(db, case_id, working, source_id=None, variant_name=None):
    pinned_empty = source_id is None and working is not None and working["revision_id"] is None
    if source_id is None and working:
        source_id = working["revision_id"]
    source = None if pinned_empty else read_project(db, case_id, source_id)
    if source is None:
        model = {**basis(None), "study_only": True}
        name = variant_name or (working or {}).get("variant_name")
        if name:
            model["variant_name"] = name
        return {"status": "completed", "model_basis": model}
    if variant_name:
        parent = source["revision_id"]
        base = source.get("base_revision_id") if source.get("model_kind") == "draft" else parent
        source = save_variant(db, case_id, SaveVariant(project=source["project"], source_revision_id=parent,
                              base_revision_id=base, variant_name=variant_name))
    elif source.get("model_kind") != "draft":
        parent = source["revision_id"]
        draft = WorkingDraft(project=source["project"], source_revision_id=parent, base_revision_id=parent)
        source = capture_snapshot(db, case_id, draft, "从已存光路开始独立试算")
        db.commit()
    return {"status": "completed", "model_basis": basis(source), "project": source["project"]}


def save_working_project(db, case_id, args, working, *, allow_unlock=False):
    if working and args.base_revision_id != working["revision_id"]:
        raise ValueError("保存时必须使用本轮正在分析的光路版本。需要修改其他版本时，请先指定该版本重新提问。")
    if working and (working["kind"] == "draft" or working.get("study_only")):
        saved = capture_snapshot(db, case_id, WorkingDraft(project=args.project, source_revision_id=working["revision_id"],
                                 base_revision_id=working.get("base_revision_id")), args.change_note, variant_name=working.get("variant_name"),
                                 preserve_locks=not allow_unlock, saved_variant=True)
        db.commit()
        return saved
    return save_project(db, case_id, args, allow_unlock=allow_unlock)


def adopt_snapshot(db, case_id, snapshot_id, base_revision_id, *, reviewed_against=None, reviewed_project=None):
    snapshot = read_project(db, case_id, snapshot_id)
    if snapshot.get("model_kind") != "draft":
        raise ValueError("请选择一份草稿快照。")
    if snapshot["base_revision_id"] != base_revision_id:
        raise ValueError("快照基线与采用请求不一致，请先核对工程差异。")
    if (reviewed_against is None) != (reviewed_project is None):
        raise ValueError("核对结果需要同时提供所对照的正式工程版本和完整模型。")
    if reviewed_against is None:
        parent, project = base_revision_id, snapshot["project"]
        link, note = "adopted_snapshot_id", f"采用试算快照 #{snapshot_id} 并重新计算"
    else:
        if read_project(db, case_id, reviewed_against).get("model_kind") == "draft":
            raise ValueError("核对基线必须为正式工程版本。")
        parent, project = reviewed_against, reviewed_project.model_dump()
        link, note = "reviewed_snapshot_id", f"核对试算快照 #{snapshot_id} 与工程 #{parent} 后重新计算"
    try:
        return save_project(db, case_id, SaveProject(project=project, base_revision_id=parent, change_note=note),
                            snapshot_lineage={link:snapshot_id})
    except ProjectRevisionConflict:
        # A repeated approval may have lost the original response. Only the
        # exact adopted snapshot qualifies; a competing edit remains a conflict.
        accepted = db.query(GeneratedContent).filter_by(case_id=case_id, content_type="optical_project",
                     operation_key=f"optical_project:parent:{parent or 0}").one_or_none()
        if accepted is None or accepted.content.get(link) != snapshot_id or accepted.content["project"] != project:
            raise
        return {"status":"completed", "already_adopted":True, "revision_id":accepted.id, **accepted.content}


def case_for_planning(db, case, working):
    """Project case-level optical facts onto the chosen study, without publishing them."""
    if not working:
        return case
    live = read_project(db, case.id)
    if working["revision_id"] is None:
        if live:
            raise ValueError("提问时工程为空，随后已有光路存入。要根据新光路进行设计，请从该工程重新提问。")
        if working.get("study_only"):
            return SimpleNamespace(id=case.id, title=case.title, description=case.description, parameters=case.parameters)
        return case
    if live and live["revision_id"] == working["revision_id"]:
        return case
    target = read_project(db, case.id, working["revision_id"])
    view = SimpleNamespace(id=case.id, title=case.title, description=case.description, parameters=case.parameters)
    _sync_saved_project(view, target["project"], live["project"] if live else None, target["revision_id"],
                        target["analysis"].get("scope_warnings", []))
    return view

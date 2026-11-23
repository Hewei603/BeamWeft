"""Read-only comparisons of engineering models and their archived calculations."""
from typing import get_args
from pydantic import BaseModel, Field, model_validator

from ..study_api import StrictInput
from .authoring import EditableLayout
from .projects import OpticalProject, calculate_project_outputs, normalize_project, read_project, read_project_inputs
from .study_inputs import WorkingDraft, draft_source


class CompareProjectInputs(StrictInput):
    reference_revision_id: int = Field(gt=0)
    revision_id: int | None = Field(default=None, gt=0, description="Target revision. Defaults to the model attached to this question.")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=30, ge=1, le=100)


class CanvasInputComparison(CompareProjectInputs):
    working_draft: WorkingDraft | None = None

    @model_validator(mode="after")
    def one_target(self):
        if (self.revision_id is None) == (self.working_draft is None):
            raise ValueError("请选择一个已存目标版本，或提供画布草稿。")
        return self


_MISSING = object()


def _identified(rows):
    if not all(isinstance(row, dict) and isinstance(row.get("uid"), str) and row["uid"] for row in rows):
        return False
    return len({row["uid"] for row in rows}) == len(rows)


def _changes(before, after, path=(), annotation=None, definition=None):
    if before is after:
        return
    def change(kind, left, right):
        return {"path":path, "kind":kind, **({"before":left} if left is not _MISSING else {}),
                **({"after":right} if right is not _MISSING else {}),
                **({"definition":definition} if definition and (left is None or right is None) else {})}

    if before is _MISSING or after is _MISSING:
        yield change("added" if before is _MISSING else "removed", before, after)
    elif isinstance(before, dict) and isinstance(after, dict):
        models = [m for m in (annotation,*get_args(annotation)) if isinstance(m,type) and issubclass(m,BaseModel)]
        fields = models[0].model_fields if len(models)==1 else {}
        for key in sorted(before.keys() | after.keys()):
            field = fields.get(key)
            yield from _changes(before.get(key, _MISSING), after.get(key, _MISSING), (*path, key),
                                field.annotation if field else None, field.description if field else None)
    elif isinstance(before, list) and isinstance(after, list) and _identified(before) and _identified(after):
        left, right = ({item["uid"]:item for item in rows} for rows in (before, after))
        positions = [{key:i for i,key in enumerate(side,1)} for side in (left,right)]
        def object_ref(uid, side):
            obj = side[uid]
            return {"uid":uid, "name":obj.get("name", uid),
                    "before_position":positions[0].get(uid), "after_position":positions[1].get(uid)}
        shared_before = [key for key in left if key in right]
        shared_after = [key for key in right if key in left]
        if shared_before != shared_after:
            yield change("reordered", [object_ref(k,left) for k in shared_before], [object_ref(k,right) for k in shared_after])
        element_type = next(iter(get_args(annotation)), None)
        for uid in dict.fromkeys((*left, *right)):
            item_path = (*path, object_ref(uid, right if uid in right else left))
            yield from _changes(left.get(uid, _MISSING), right.get(uid, _MISSING), item_path, element_type)
    elif isinstance(before, list) and isinstance(after, list):
        # Unidentified optics, arbitrary extension arrays and matrices have no
        # proven object correspondence. Keep both ordered values intact.
        if len(before) != len(after) or any(next(_changes(a,b), None) is not None for a,b in zip(before,after)):
            yield change("changed", before, after)
    elif before != after or isinstance(before, bool) != isinstance(after, bool):
        yield change("changed", before, after)


def _bound_parts(path, elements):
    # Stock bindings use path-local indices. Resolve physical identities before
    # comparing two revisions whose elements may have been inserted or moved.
    parts = path.get("parts", [])
    if not parts or any(not elements[p["element_index"]].get("uid") for p in parts):
        return path
    bound = []
    for part in parts:
        element = elements[part["element_index"]]
        bound.append({"uid":element["uid"], **{k:v for k,v in part.items() if k != "element_index"}})
    return {**path, "parts":sorted(bound,key=lambda row:row["uid"])}


def _inputs(project):
    inputs = _bound_parts(project, project["system"]["elements"])
    if project.get("incident_paths"):
        return {**inputs, "incident_paths":[_bound_parts(p, p.get("elements", [])) for p in project["incident_paths"]]}
    return inputs


def _label(record):
    return {"revision_id":record["revision_id"], "title":record["project"]["title"],
            "kind":record.get("model_kind", "saved"), "variant_name":record.get("variant_name"),
            "solver_receipt":record.get("analysis", {}).get("solver_receipt")}


def compare_project_inputs(db, case_id, request, target_id=None, draft=None):
    target_id = request.revision_id or target_id
    if draft is None and target_id is None:
        raise ValueError("没有可对照的目标模型，请选择已存版本或画布草稿。")
    related = (draft.source_revision_id, draft.base_revision_id) if draft else (target_id,)
    models = read_project_inputs(db, case_id, (request.reference_revision_id, *related), summary=True)
    reference = models[request.reference_revision_id]
    if draft is not None:
        source = draft_source(draft, models)
        project = normalize_project(draft.project, source).model_dump()
        target = {"project":project, "revision_id":None, "model_kind":"canvas"}
    else:
        target = models[target_id]
    page, total = [], 0
    for total, row in enumerate(_changes(_inputs(reference["project"]), _inputs(target["project"]), annotation=OpticalProject), start=1):
        if request.offset < total <= request.offset+request.limit:
            page.append(row)
    end = request.offset+request.limit
    return {"reference":_label(reference), "target":_label(target), "total":total,
            "items":page, "offset":request.offset, "next_offset":end if end<total else None}


class RecheckProject(StrictInput):
    revision_id: int | None = Field(default=None, gt=0, description="Saved project revision or snapshot. Defaults to the question's model.")


def compare_values(saved, calculated, *, exclude=(), limit=16):
    """Compare JSON fields by path. A missing subtree counts as one structural change."""
    counts = {"numeric": 0, "structure": 0, "other": 0}
    examples = []
    compared_numbers = 0

    def brief(value):
        if isinstance(value, (list, tuple)):
            return {"items": len(value)}
        if isinstance(value, dict):
            return {"fields": len(value)}
        return value

    def difference(path, kind, left, right):
        counts[kind] += 1
        if len(examples) < limit:
            examples.append({"path": list(path), "kind": kind,
                             **({"saved": brief(left)} if left is not _MISSING else {}),
                             **({"recomputed": brief(right)} if right is not _MISSING else {})})

    def visit(path, left, right):
        nonlocal compared_numbers
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(left.keys() | right.keys()):
                if not path and key in exclude:
                    continue
                visit((*path, key), left.get(key, _MISSING), right.get(key, _MISSING))
        elif isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
            if len(left) != len(right):
                difference(path, "structure", left, right)
            for i, (a, b) in enumerate(zip(left, right)):
                visit((*path, i), a, b)
        elif (isinstance(left, (int, float)) and not isinstance(left, bool)
              and isinstance(right, (int, float)) and not isinstance(right, bool)):
            compared_numbers += 1
            if left != right:
                difference(path, "numeric", left, right)
        elif type(left) is not type(right):
            difference(path, "structure", left, right)
        elif left != right:
            difference(path, "other", left, right)

    visit((), saved, calculated)
    return {"compared_numbers": compared_numbers, "difference_counts": counts, "differences": examples,
            "total_differences": sum(counts.values())}


def read_recheck_source(db, case_id, revision_id):
    if revision_id is None or revision_id <= 0:
        raise ValueError("请先选择要核对的已存工程或试算快照。")
    record = read_project(db, case_id, revision_id)
    if not isinstance(record.get("analysis"), dict) or not record["analysis"]:
        raise ValueError("这个版本缺少保存时的计算结果，无法逐项核对。")
    return record


def recheck_project(record):
    # Editing a project can assign new IDs and consult inventory. Replaying it must use the archive.
    document, saved = record["project"], record["analysis"]
    frozen_inputs = {k: document[k] for k in EditableLayout.model_fields if k in document}
    layout = EditableLayout.model_validate(frozen_inputs)
    # Retain unrecognised top-level archive fields in the comparison so omissions stay visible.
    extra_inputs = {k: v for k, v in document.items() if k not in OpticalProject.model_fields}
    parsed = compare_values({**frozen_inputs, **extra_inputs}, layout.model_dump(mode="json"))
    current = calculate_project_outputs(layout, document)
    return {"status": "completed", "revision_id": record["revision_id"],
            "saved_status": saved.get("status"), "recomputed_status": current.get("status"),
            "saved_solver_receipt": saved.get("solver_receipt"), "recomputed_solver_receipt": current.get("solver_receipt"),
            **({"input_interpretation": parsed} if parsed["total_differences"] else {}),
            **compare_values(saved, current, exclude={"system", "engine", "solver_receipt"})}

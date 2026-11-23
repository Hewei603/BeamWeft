"""Typed references connecting conversation, evidence and engineering objects."""
from pydantic import Field, model_validator
from sqlalchemy import func

from ..attachment_files import file_reference
from ..models import AgentTask, AgentToolCall, Attachment, GeneratedContent, InventoryItem
from ..version import provider_display_name
from .network import Terminal, declared_ports
from .optics import StrictInput
from .projects import current_project_input, read_project_inputs


def attachment_page(db, case_id, *, query="", before_id=None, page_size=12):
    files = db.query(Attachment.id, Attachment.filename, Attachment.relative_path, Attachment.file_type).filter_by(case_id=case_id)
    path = func.coalesce(Attachment.relative_path, Attachment.filename)
    for term in dict.fromkeys(query.replace("\\", "/").split()):
        files = files.filter(path.icontains(term, autoescape=True))
    total = files.count()
    if before_id is not None:
        files = files.filter(Attachment.id < before_id)
    rows = files.order_by(Attachment.id.desc()).limit(page_size + 1).all()
    return {"items": [{**file_reference(row), "type":row.file_type} for row in rows[:page_size]],
            "total_matches":total, "next_before_id":rows[page_size-1].id if len(rows)>page_size else None}


class PathObject(StrictInput):
    """The source or a private element of one ordered path, by identity within that path."""
    path_uid: str = Field(min_length=1, max_length=100)
    element_uid: str | None = Field(default=None, min_length=1, max_length=100)


class ObservationTarget(StrictInput):
    """A registered observation plane and an optional path whose field is of interest.

    The reference remains addressable when that beam cannot be evaluated at the plane.
    """
    plane_uid: str = Field(min_length=1, max_length=100)
    beam_uid: str | None = Field(default=None, min_length=1, max_length=100)


class InputRefs(StrictInput):
    """Object identities can refer to the working draft supplied with a new run."""
    project_revision_id: int | None = Field(default=None, gt=0)
    element_uids: list[str] = Field(default_factory=list, max_length=200)
    project_objects: list[PathObject | Terminal | ObservationTarget] = Field(default_factory=list, max_length=200)
    inventory_ids: list[int] = Field(default_factory=list, max_length=100)
    attachment_ids: list[int] = Field(default_factory=list, max_length=8)
    tool_call_ids: list[int] = Field(default_factory=list, max_length=100)
    artifact_ids: list[int] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def valid_references(self):
        for name in ("inventory_ids", "attachment_ids", "tool_call_ids", "artifact_ids"):
            values = getattr(self, name)
            if any(value <= 0 for value in values):
                raise ValueError("引用ID必须为正整数")
            setattr(self, name, list(dict.fromkeys(values)))
        self.element_uids = list(dict.fromkeys(self.element_uids))
        self.project_objects = list({obj.model_dump_json(): obj for obj in self.project_objects}.values())
        return self

    def selects_objects(self):
        return bool(self.element_uids or self.project_objects)


class ContextRefs(InputRefs):
    """Persisted object references must resolve against an immutable revision."""

    @model_validator(mode="after")
    def anchored_elements(self):
        if self.selects_objects() and self.project_revision_id is None:
            raise ValueError("引用工程对象时需要同时选择其工程版本")
        return self


def validate_context_refs(db, case_id, refs: ContextRefs):
    """Reject stale identities or references belonging to another experiment."""
    if refs.project_revision_id is not None:
        revision = read_project_inputs(db, case_id, [refs.project_revision_id])[refs.project_revision_id]
        project = revision["project"]
        if refs.element_uids and set(refs.element_uids) - {el.get("uid") for el in project["system"]["elements"]}:
            raise ValueError("引用元件不属于所选工程版本，请重新选择当前工程对象")
        if refs.project_objects:
            paths = {"primary": project["system"], **{p["uid"]: p for p in project.get("incident_paths", [])}}
            planes = {p["uid"] for p in project.get("observation_planes", [])}
            ports = {c["uid"]: declared_ports(c["model"]["kind"], c["model"].get("ports"))
                     for c in (project.get("network") or {}).get("components", [])}
            for obj in refs.project_objects:
                if isinstance(obj, PathObject):
                    path = paths.get(obj.path_uid)
                    if path is None or (obj.element_uid is not None and
                                        not any(el.get("uid") == obj.element_uid for el in path["elements"])):
                        raise ValueError(f"所选版本中找不到引用的光路或元件（{obj.path_uid} / {obj.element_uid or '光源'}）。请移除该引用后重新选择。")
                elif isinstance(obj, ObservationTarget):
                    if obj.plane_uid not in planes:
                        raise ValueError(f"所选版本中找不到观察面 {obj.plane_uid}。请移除该引用后重新选择。")
                    if obj.beam_uid is not None and obj.beam_uid not in paths:
                        raise ValueError(f"所选版本中找不到观察面 {obj.plane_uid} 引用的光路 {obj.beam_uid}。请重新选择光路。")
                elif obj.port not in ports.get(obj.component, ()):
                    raise ValueError(f"所选版本中找不到端口 {obj.component} / {obj.port}。请移除该引用后重新选择。")
    for item_id in refs.inventory_ids:
        if db.get(InventoryItem, item_id) is None:
            raise ValueError("引用的库存元件不存在")
    for attachment_id in refs.attachment_ids:
        row = db.get(Attachment, attachment_id)
        if row is None or row.case_id != case_id:
            raise ValueError("引用的附件不属于当前案例")
    if refs.artifact_ids:
        owned = db.query(GeneratedContent.id).filter(GeneratedContent.case_id == case_id, GeneratedContent.id.in_(refs.artifact_ids))
        if set(refs.artifact_ids) != {rid for (rid,) in owned}:
            raise ValueError("引用的结果不属于当前案例")
    for call_id in refs.tool_call_ids:
        row = db.get(AgentToolCall, call_id)
        task = db.get(AgentTask, row.task_id) if row else None
        if task is None or task.case_id != case_id:
            raise ValueError("引用的计算记录不属于当前案例")
    return refs


def selected_context(db, case_id, refs):
    refs = validate_context_refs(db, case_id, ContextRefs.model_validate(refs or {}))
    result = refs.model_dump()
    if refs.tool_call_ids:
        rows = db.query(AgentToolCall.id, AgentToolCall.tool_name, AgentToolCall.status).join(AgentTask).filter(
            AgentTask.case_id == case_id, AgentToolCall.id.in_(refs.tool_call_ids))
        records = {rid: {"tool_call_id": rid, "name": name, "status": status,
                         "read_with": "read_tool_result"} for rid, name, status in rows}
        result["tool_records"] = [records[rid] for rid in refs.tool_call_ids if rid in records]
    if refs.artifact_ids:
        definition = GeneratedContent.content["definition"]
        rows = db.query(GeneratedContent.id, GeneratedContent.content_type, definition["title"].as_string(),
                        definition["name"].as_string(), definition["version"].as_string(),
                        definition["provider"].as_string(), GeneratedContent.content["adjustment_evaluation"]["search_id"].as_integer()).filter(
            GeneratedContent.case_id == case_id, GeneratedContent.id.in_(refs.artifact_ids))
        artifacts = {}
        for rid, kind, title, name, version, provider, policy_source in rows:
            artifact = {"id":rid, "type":kind}
            if kind == "scientific_study":
                artifact.update(title=title, method=name, version=version,
                                read_project_basis={"content_id":rid, "path":["project_basis"]},
                                read_outputs={"content_id":rid, "path":["available_outputs"], "offset":0})
                if provider:
                    artifact["provider_display_name"] = provider_display_name(provider)
            elif kind == "study_search":
                artifact.update(read_criteria={"content_id":rid, "path":["recipe", "measures"]},
                                read_options={"content_id":rid, "path":["assessment_options"], "offset":0},
                                read_candidates={"content_id":rid, "path":["rows"], "offset":0})
                if policy_source is not None:
                    artifact.update(source_search_id=policy_source,
                                    read_adjustment_assumptions={"content_id":rid, "path":["adjustment_evaluation","roles"]},
                                    read_adjustment_results={"content_id":rid, "path":["adjustment_evaluation","designs"]})
            artifacts[rid] = artifact
        result["artifacts"] = [artifacts[rid] for rid in refs.artifact_ids]
    if refs.project_revision_id is not None:
        selected = read_project_inputs(db, case_id, [refs.project_revision_id])[refs.project_revision_id]
        result["project"] = selected["project"]
        head_id = db.query(GeneratedContent.id).filter_by(case_id=case_id,content_type="optical_project").order_by(GeneratedContent.id.desc()).limit(1).scalar()
        result["is_current_project"] = head_id == refs.project_revision_id
        result["meaning"] = "User-selected analysis context; a historical reference does not restore or replace current hardware."
    return result


def workspace_view(db, case, latest_task=None, include_attachments=True):
    current = current_project_input(db, case.id, summary=True)
    project = current["project"] if current else {}
    system = project.get("system", {})
    analysis = (current or {}).get("analysis", {})
    result = (latest_task.final_content.content.get("workbench", {})
              if latest_task and latest_task.final_content else {})
    return {"project_revision_id": current["revision_id"] if current else None,
            "project_title": project.get("title"), "mode": (current or {}).get("analysis", {}).get("mode") or system.get("mode"),
            "wavelength_nm": ((project.get("network") or {}).get("wavelength_nm") if not system.get("elements") else None) or system.get("wavelength_nm"),
            "elements": system.get("elements", []), "network_component_count": len((project.get("network") or {}).get("components", [])),
            "parts": project.get("parts", []), "assemblies": project.get("assemblies", []), "assumptions": project.get("assumptions", []),
            "calculation_status": analysis.get("status"), "input_issues": analysis.get("input_issues", []),
            "scope_warnings": analysis.get("scope_warnings", project.get("component_context", {}).get("scope_warnings", [])),
            **({"attachments": [file_reference(a) for a in case.attachments]} if include_attachments else {}),
            "latest_goal": latest_task.goal if latest_task else case.goal,
            "facts": result.get("facts", []), "open_questions": result.get("questions", [])}

"""Persist reproducible scans from both the Agent and engineering workspace."""

import asyncio
from copy import deepcopy

from pydantic import Field
from sqlalchemy import func

from ..models import AgentTask, GeneratedContent
from .optics import OpticalScan, OpticalScan2D, scan_system, scan_system_2d
from .observation import LayoutSystem
from .projects import read_project, revisions


class ProjectScan(OpticalScan):
    project_revision_id: int | None = Field(
        default=None,
        gt=0,
        description="Saved engineering version to analyze. Omit only for an exploratory system or to auto-match an unchanged saved system.",
    )


class ProjectScan2D(OpticalScan2D):
    project_revision_id: int | None = Field(
        default=None, gt=0, description="Saved engineering version to analyze; the submitted system must match it."
    )


def _same_system(left, right):
    left = LayoutSystem.model_validate(deepcopy(left)).model_dump()
    right = LayoutSystem.model_validate(deepcopy(right)).model_dump()
    # Optional UI identities do not change the submitted optical model.
    for system in (left, right):
        for element in system["elements"]:
            element.pop("uid", None)
    return left == right


def _resolve_revision(db, case_id, system, revision_id, working):
    if revision_id is not None:
        saved = read_project(db, case_id, revision_id)
        if not _same_system(system, saved["project"]["system"]):
            raise ValueError("Save the changed project before scanning; scan must match its revision")
        return saved
    if working is not None:
        if not working.get("revision_id"):
            return None
        candidate = read_project(db, case_id, working["revision_id"])
        return candidate if _same_system(system, candidate["project"]["system"]) else None
    for row in revisions(db, case_id):
        if _same_system(system, row.content["project"]["system"]):
            return {"revision_id": row.id, **row.content}
    return None


async def run_scan(db, case_id, request, *, revision_id=None, task_id=None, working=None):
    """Never silently install an exploratory model or attach it to other hardware."""
    config = request.model_dump()
    requested_revision = config.pop("project_revision_id", None)
    if revision_id is not None and requested_revision not in {None, revision_id}:
        raise ValueError("Scan revision references disagree")
    revision_id = revision_id if revision_id is not None else requested_revision
    saved = _resolve_revision(db, case_id, config["system"], revision_id, working)
    dimensions = 2 if "x" in config else 1
    compute = scan_system_2d if dimensions == 2 else scan_system
    output = await asyncio.to_thread(compute, config)
    if task_id is not None:
        task = db.get(AgentTask, task_id)
        if task is None or task.case_id != case_id:
            raise ValueError("Scan task does not belong to this case")
        db.refresh(task)
        if task.status == "cancelled":
            raise ValueError("Task stopped before scan result was saved")
    warnings = list((saved or {}).get("project", {}).get("component_context", {}).get("scope_warnings", []))
    if saved and saved.get("model_kind") == "draft":
        warnings.append(f"扫描使用试算快照 #{saved['revision_id']}。")
    if saved is None:
        warnings.append("这是独立探索模型的扫描，尚未安装为当前工程；请先保存工程再把结果作为当前实物方案。")
    output = {**output, "scope_warnings": warnings}
    linked_revision = saved["revision_id"] if saved else None
    content = {
        "project_revision_id": linked_revision,
        "dimensions": dimensions,
        "configuration": config,
        "scan": output,
        "task_id": task_id,
        "summary": f"光学工程{'二维稳定图' if dimensions == 2 else '单参数扫描'}",
    }
    artifact = GeneratedContent(
        case_id=case_id, content_type="optical_scan", prompt_version="optical-scan-v2", content=content
    )
    db.add(artifact)
    db.commit()
    return {"artifact_id": artifact.id, "project_revision_id": linked_revision, "dimensions": dimensions, **output}


def scans_for_revision(db, case_id, revision_id, *, latest_2d=False):
    rows = (
        db.query(GeneratedContent)
        .filter_by(case_id=case_id, content_type="optical_scan")
        .filter(GeneratedContent.content["project_revision_id"].as_integer() == revision_id)
        .order_by(GeneratedContent.id.desc())
    )
    if latest_2d:
        rows = rows.filter(func.coalesce(GeneratedContent.content["dimensions"].as_integer(), 2) == 2).limit(1)
    return [
        {
            "artifact_id": row.id,
            "project_revision_id": revision_id,
            "dimensions": row.content.get("dimensions", 2),
            **row.content["scan"],
        }
        for row in rows
    ]

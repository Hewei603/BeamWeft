"""Design workspace API. Uses existing case, conversation and artifact storage."""
from __future__ import annotations

import csv
import io
import json
from typing import Any, Literal
from threading import Lock

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ..auth.acl import assert_case_edit, assert_case_view
from ..auth.security import Principal, get_current_principal
from ..database import get_db
from ..run_ownership import RunLease, reconcile
from ..design import service
from ..design.brief import validate_fact
from ..design.rezonator import cold_cavity_schema
from ..design.analysis import analyze_design, probe_design
from ..models import AgentChatMessage, AgentChatSession, AgentTask, AgentToolCall, Attachment, GeneratedContent
from ..schemas import GeneratedContentResponse
from ..workbench.contracts import DesignSelection
from ..workbench.design_import import independent_design
from ..workbench.projects import current_project_input
from .cases import get_case_or_404

router = APIRouter()
_start_lock = Lock()


class DesignRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    attachment_ids: list[int] = Field(default_factory=list, max_length=4)
    constraints: dict[str, Any] = Field(default_factory=dict, max_length=60)
    assumption_policy: Literal["evidence_only", "exploratory"] = "exploratory"

    @field_validator("constraints")
    @classmethod
    def validate_constraints(cls, value):
        return {key: validate_fact(key, val) for key, val in value.items()}


class AnalysisRequest(BaseModel):
    candidate: int = Field(default=1, ge=1, le=5)
    mode: Literal["cavity", "pump"] = "cavity"
    output_gap_mm: float | None = Field(default=None, ge=.001, le=2000, allow_inf_nan=False)
    thermal_f_mm: float | None = Field(default=None, ge=1, le=1e7, allow_inf_nan=False)
    samples: int = Field(default=900, ge=100, le=5000)
    scan_samples: int = Field(default=600, ge=100, le=2000)


class ProbeRequest(AnalysisRequest):
    z_mm: float = Field(allow_inf_nan=False)


def _analysis_artifact(case_id, content_id, rank, db, principal):
    case = get_case_or_404(case_id, db)
    assert_case_view(db, case, principal)
    content = db.get(GeneratedContent, content_id)
    if not content or content.case_id != case_id:
        raise HTTPException(404, "设计版本不存在")
    design = content.content.get("engineering_design", {})
    candidates = design.get("candidates", [])
    if not 1 <= rank <= len(candidates):
        raise HTTPException(404, "候选方案不存在")
    return design, candidates[rank - 1]


@router.post("/{case_id}/design/{content_id}/analysis")
def analyze_saved_design(case_id: int, content_id: int, request: AnalysisRequest,
                         db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    design, candidate = _analysis_artifact(case_id, content_id, request.candidate, db, principal)
    try:
        return analyze_design(design, candidate, **request.model_dump(exclude={"candidate"}))
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{case_id}/design/{content_id}/import-project")
def open_design_geometry(case_id: int, content_id: int, request: DesignSelection,
                         db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    assert_case_edit(db, get_case_or_404(case_id, db), principal)
    content = db.get(GeneratedContent, content_id)
    if not content or content.case_id != case_id or "engineering_design" not in content.content:
        raise HTTPException(404, "本工程没有此设计记录。")
    current = current_project_input(db, case_id, summary=True)
    base = current["revision_id"] if current else None
    try:
        return independent_design(db, case_id, content.content["engineering_design"], content.id,
                                  request.candidate, source=base, base=base, target=request.target)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{case_id}/design/{content_id}/probe")
def probe_saved_design(case_id: int, content_id: int, request: ProbeRequest,
                       db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    design, candidate = _analysis_artifact(case_id, content_id, request.candidate, db, principal)
    try:
        return probe_design(design, candidate, **request.model_dump(exclude={"candidate", "samples", "scan_samples"}))
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc


def _task_json(task, ownership=None):
    return {"id": task.id, "status": task.status, "goal": task.goal, "final_content_id": task.final_content_id,
            **({"execution_owner_state":ownership} if ownership and task.status in {"pending", "running"} else {}),
            "steps": [{"title": step.title, "status": step.status, "detail": step.result_summary or step.rationale}
                      for step in sorted(task.steps, key=lambda step: step.step_index)]}


def _recover_interrupted(db, case_id):
    return reconcile(db, case_id, "design")


@router.get("/{case_id}/design")
def get_design(case_id: int, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    case = get_case_or_404(case_id, db)
    assert_case_view(db, case, principal)
    owners = _recover_interrupted(db, case_id)
    sid = (case.parameters or {}).get("design_session_id")
    messages = (db.query(AgentChatMessage).join(AgentChatSession)
                .filter(AgentChatMessage.session_id == sid, AgentChatSession.case_id == case_id)
                .order_by(AgentChatMessage.id).all()) if sid else []
    revisions = db.query(GeneratedContent).filter_by(case_id=case_id, content_type="plan").order_by(GeneratedContent.id.desc()).all()
    tasks = db.query(AgentTask).filter_by(case_id=case_id, mode="design").order_by(AgentTask.id.desc()).limit(10).all()
    return {"messages": [{"id": msg.id, "role": msg.role, "content": msg.content, "metadata": msg.metadata_json, "created_at": msg.created_at} for msg in messages],
            "revisions": [GeneratedContentResponse.model_validate(item).model_dump() for item in revisions if "engineering_design" in item.content],
            "runs": [_task_json(task, owners.get(task.id)) for task in tasks], "facts": (case.parameters or {}).get("design_facts", {}),
            "inventory": service.inventory_snapshot(db)}


@router.post("/{case_id}/design/runs", status_code=202)
def start_design(case_id: int, request: DesignRequest, background_tasks: BackgroundTasks,
                 db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    case = get_case_or_404(case_id, db)
    assert_case_edit(db, case, principal)
    if not request.message.strip():
        raise HTTPException(422, "请描述实验目标或要修改的条件。")
    attachments = db.query(Attachment).filter(Attachment.id.in_(request.attachment_ids)).all()
    if len(attachments) != len(set(request.attachment_ids)) or any(item.case_id != case_id for item in attachments):
        raise HTTPException(404, "图片附件不存在或不属于当前案例。")
    with _start_lock:
        _recover_interrupted(db, case_id)
        if db.query(AgentTask).filter(AgentTask.case_id == case_id, AgentTask.mode == "design", AgentTask.status.in_(["pending", "running"])).first():
            raise HTTPException(409, "当前案例正在计算，请等待完成后再修改需求。")
        session = service.design_session(db, case, principal.user_id)
        task = AgentTask(case_id=case_id, user_id=principal.user_id, goal=request.message.strip(), mode="design", status="pending")
        db.add(task)
        db.flush()
        db.add(AgentChatMessage(session_id=session.id, role="user", content=request.message.strip(),
                                metadata_json={"design_run_id": task.id, "attachment_ids": request.attachment_ids,
                                               "constraints": request.constraints, "assumption_policy": request.assumption_policy}))
        lease = RunLease(db.get_bind())
        db.add(AgentToolCall(task_id=task.id, tool_name="design_request", status="completed",
                             input_json={"session_id": session.id, "execution_owner":lease.stamp, "attachment_ids": request.attachment_ids,
                                         "constraints": request.constraints, "assumption_policy": request.assumption_policy}, output_json={}))
        service.ACTIVE_RUNS.add(task.id)
        try:
            db.commit()
        except Exception:
            service.ACTIVE_RUNS.discard(task.id)
            lease.close(remove=True)
            raise
        background_tasks.add_task(lease.execute, service.execute_design, task.id)
        return {"task_id": task.id, "status": "pending"}


@router.post("/{case_id}/design/runs/{task_id}/end-unverified")
def end_unverified(case_id: int, task_id: int, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    assert_case_edit(db, get_case_or_404(case_id, db), principal)
    task = db.query(AgentTask).filter_by(id=task_id, case_id=case_id, mode="design").one_or_none()
    if not task:
        raise HTTPException(404, "设计任务不存在。")
    owners = reconcile(db, case_id, "design", task_id)
    if owners.get(task_id) == "live":
        raise HTTPException(409, "原设计任务的执行进程仍在运行。")
    if owners.get(task_id) == "unverified":
        db.query(AgentTask).filter(AgentTask.id == task_id, AgentTask.case_id == case_id,
                                   AgentTask.status.in_(["pending", "running"])).update(
            {AgentTask.status:"cancelled"}, synchronize_session=False)
        db.commit()
    return {"task_id":task.id, "status":task.status}


def _csv_basis(design):
    policy = {"evidence_only": "仅采用有依据的输入", "exploratory": "条件设计探索"}.get(
        design.get("assumption_policy"), "历史版本未记录计算取值方式")
    assumptions = "；".join(
        f"{a['name']} = {a['value']} {a.get('unit', '')}（{a.get('basis', '')}）"
        for a in design.get("assumptions", []) if isinstance(a, dict) and "name" in a and "value" in a)
    return [policy, assumptions]


@router.get("/{case_id}/design/{content_id}/positions.csv")
def export_positions(case_id: int, content_id: int, candidate: int = 1, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    case = get_case_or_404(case_id, db)
    assert_case_view(db, case, principal)
    content = db.get(GeneratedContent, content_id)
    if not content or content.case_id != case_id:
        raise HTTPException(404, "设计版本不存在")
    design = content.content.get("engineering_design", {})
    candidates = design.get("candidates", [])
    if not 1 <= candidate <= len(candidates):
        raise HTTPException(404, "候选方案不存在")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["元件", "z (mm，相对 M1 镀膜面)", "结束 z (mm)", "位置依据", "计算取值方式", "本方案的假设与约定"])
    basis = _csv_basis(design)
    for row in candidates[candidate - 1]["positions"]:
        writer.writerow([row["name"], row.get("z_mm") if row.get("z_mm") is not None else "待补参数", row.get("end_z_mm", ""), row["basis"], *basis])
    return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="design-{content_id}-candidate-{candidate}.csv"'})


@router.get("/{case_id}/design/{content_id}/bom.csv")
def export_bom(case_id: int, content_id: int, candidate: int = 1, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    case = get_case_or_404(case_id, db)
    assert_case_view(db, case, principal)
    content = db.get(GeneratedContent, content_id)
    if not content or content.case_id != case_id:
        raise HTTPException(404, "设计版本不存在")
    design = content.content.get("engineering_design", {})
    candidates = design.get("candidates", [])
    if not 1 <= candidate <= len(candidates):
        raise HTTPException(404, "候选方案不存在")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["元件", "需求规格", "最小有效通光口径 mm", "输出耦合率 %（候选非定值）", "库存判断", "规格匹配记录", "待核验记录", "计算取值方式", "本方案的假设与约定"])
    basis = _csv_basis(design)
    for row in candidates[candidate - 1]["bom"]:
        transmission = row.get("transmission_pct")
        if transmission is None:
            transmission = "/".join(map(str, row.get("transmission_candidates_pct") or [])) or "未指定"
        writer.writerow([row["role"], row["spec"], row["min_clear_aperture_mm"], transmission, row["status"],
                         "; ".join(f"#{item['id']} {item['name']}" for item in row["matched"]),
                         "; ".join(f"#{item['id']} {item['name']}: {', '.join(item['unknowns'])}" for item in row["needs_verification"]), *basis])
    return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="design-{content_id}-candidate-{candidate}-bom.csv"'})


@router.get("/{case_id}/design/{content_id}/cavity.rez")
def export_rezonator(case_id: int, content_id: int, candidate: int = 1, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    case = get_case_or_404(case_id, db)
    assert_case_view(db, case, principal)
    content = db.get(GeneratedContent, content_id)
    if not content or content.case_id != case_id:
        raise HTTPException(404, "设计版本不存在")
    design = content.content.get("engineering_design", {})
    candidates = design.get("candidates", [])
    if not 1 <= candidate <= len(candidates):
        raise HTTPException(404, "候选方案不存在")
    wl = design.get("resolved_inputs", {}).get("laser_wavelength_nm") or design["facts"]["laser_wavelength_nm"]["value"]
    output = cold_cavity_schema(candidates[candidate - 1], wl)
    return Response(json.dumps(output, ensure_ascii=False, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="design-{content_id}-candidate-{candidate}-cold.rez"'})

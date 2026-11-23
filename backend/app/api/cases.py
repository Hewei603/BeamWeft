"""Experiment case API routes."""
from __future__ import annotations

import os
import re
from hashlib import sha256
from tempfile import NamedTemporaryFile

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from ..case_package import PackageError, write_package
from ..project_details import DetailsConflict, ProjectDetails, project_details

from ..auth.acl import case_visibility, assert_case_delete, assert_case_edit, assert_case_view, has_project_role, is_admin
from ..auth.security import Principal, get_current_principal
from ..database import get_db
from ..knowledge.ingestion import upsert_case_source
from ..models import Attachment, ExperimentCase, GeneratedContent, Project
from ..observability.audit import record_audit
from ..schemas import ExperimentCaseCreate, ExperimentCaseResponse, ExperimentCaseUpdate

router = APIRouter()


class CaseDetail(ExperimentCaseResponse):
    draft_scope: str
    is_open_document: bool
    recovered_from: str | None


def draft_scope(case, principal):
    account = f"user:{principal.user_id}" if principal.user_id is not None else f"local:{principal.actor}"
    return sha256(f"{case.id}|{case.created_at}|{account}".encode()).hexdigest()


def get_case_or_404(case_id: int, db: Session) -> ExperimentCase:
    case = db.query(ExperimentCase).filter(ExperimentCase.id == case_id).first()
    if not case:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Case {case_id} does not exist")
    return case


@router.post("", response_model=ExperimentCaseResponse, status_code=status.HTTP_201_CREATED)
def create_case(
    case_data: ExperimentCaseCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    """Create an experiment case and index it for retrieval."""
    payload = case_data.model_dump()
    if payload.get("project_id") is not None:
        project = db.query(Project).filter(Project.id == payload["project_id"]).first()
        if not project:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Project {payload['project_id']} does not exist")
        if (
            principal.user_id is not None
            and not is_admin(principal)
            and project.visibility != "organization"
            and not has_project_role(db, project.id, principal, "editor")
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Project editor role required")
    if payload.get("owner_id") is None and principal.user_id is not None:
        payload["owner_id"] = principal.user_id
    case = ExperimentCase(**payload)
    db.add(case)
    db.flush()
    upsert_case_source(db, case)
    record_audit(db, action="case.create", resource_type="case", resource_id=str(case.id))
    db.commit()
    db.refresh(case)
    return case


@router.get("", response_model=list[ExperimentCaseResponse])
async def list_cases(
    skip: int = 0,
    limit: int = 100,
    project_id: int | None = None,
    status_filter: str | None = None,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    """List experiment cases."""
    query = db.query(ExperimentCase)
    restriction = case_visibility(principal)
    if restriction is not None:
        query = query.filter(restriction)
    if project_id is not None:
        query = query.filter(ExperimentCase.project_id == project_id)
    if status_filter:
        query = query.filter(ExperimentCase.status == status_filter)
    return query.offset(skip).limit(limit).all()


@router.get("/library")
def case_library(request: Request, q: str = Query("", max_length=255), before: int | None = Query(None, gt=0),
                 limit: int = Query(24, ge=1, le=100), db: Session = Depends(get_db),
                 principal: Principal = Depends(get_current_principal)):
    """Browse in descending creation order without loading optical models or execution history."""
    case = ExperimentCase
    files = select(func.count(Attachment.id)).where(Attachment.case_id == case.id).scalar_subquery()
    contents = select(GeneratedContent.id).where(GeneratedContent.case_id == case.id)
    revision = contents.where(GeneratedContent.content_type == "optical_project").order_by(GeneratedContent.id.desc()).limit(1)
    studies = contents.with_only_columns(func.count(GeneratedContent.id)).where(GeneratedContent.content_type == "scientific_study")
    query = select(case.id, case.title, func.substr(case.description, 1, 240).label("description"),
                   case.created_at, case.status, case.project_id, case.cavity_type, case.tags,
                   files.label("file_count"), revision.scalar_subquery().label("revision_id"),
                   studies.scalar_subquery().label("study_count"))
    restriction = case_visibility(principal)
    if restriction is not None:
        query = query.where(restriction)
    if before is not None:
        query = query.where(case.id < before)
    if term := q.strip():
        query = query.where(or_(case.title.icontains(term, autoescape=True),
                                case.description.icontains(term, autoescape=True)))
    rows = db.execute(query.order_by(case.id.desc()).limit(limit + 1)).mappings().all()
    items = [dict(row) for row in rows[:limit]]
    opened = getattr(request.app.state, "opened_case", None)
    if opened:
        for item in items:
            if item["id"] == opened["case_id"]:
                item["is_open_document"] = True
                if opened.get("opening_revision_id") is not None:
                    item["opening_revision_id"] = opened["opening_revision_id"]
                break
    return {"items": items, "next_before": rows[limit - 1]["id"] if len(rows) > limit else None}


@router.get("/{case_id}", response_model=CaseDetail)
async def get_case(case_id: int, request: Request, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    """Get one experiment case."""
    case = get_case_or_404(case_id, db)
    assert_case_view(db, case, principal)
    opened = getattr(request.app.state, "opened_case", None)
    is_open_document = opened is not None and case.id == opened["case_id"]
    return {**ExperimentCaseResponse.model_validate(case).model_dump(), "draft_scope": draft_scope(case, principal),
            "is_open_document": is_open_document, "recovered_from": opened.get("recovered_from") if is_open_document else None}


@router.put("/{case_id}", response_model=ExperimentCaseResponse)
def update_case(
    case_id: int,
    case_data: ExperimentCaseUpdate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    """Update an experiment case and refresh the knowledge index."""
    case = get_case_or_404(case_id, db)
    assert_case_edit(db, case, principal)
    payload = case_data.model_dump(exclude_unset=True)
    if payload.get("project_id") is not None:
        project = db.query(Project).filter(Project.id == payload["project_id"]).first()
        if not project:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Project {payload['project_id']} does not exist")
        if (
            principal.user_id is not None
            and not is_admin(principal)
            and project.visibility != "organization"
            and not has_project_role(db, project.id, principal, "editor")
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Project editor role required")
    if "goal" in payload and payload["goal"] is None:
        payload["goal"] = ""
    if "title" in payload and not (payload["title"] or "").strip():
        raise HTTPException(422, "请填写工程名称。")
    for field, value in payload.items():
        setattr(case, field, value)
    upsert_case_source(db, case)
    record_audit(db, action="case.update", resource_type="case", resource_id=str(case.id))
    db.commit()
    db.refresh(case)
    return case


@router.patch("/{case_id}/details")
def edit_project_details(case_id: int, payload: ProjectDetails, db: Session = Depends(get_db),
                         principal: Principal = Depends(get_current_principal)):
    case = get_case_or_404(case_id, db)
    assert_case_edit(db, case, principal)
    try:
        result = project_details(db, case, payload)
    except DetailsConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    return result


@router.delete("/{case_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_case(case_id: int, request: Request, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    """Delete an experiment case."""
    case = get_case_or_404(case_id, db)
    assert_case_delete(db, case, principal)
    opened = getattr(request.app.state, "opened_case", None)
    if opened and opened["case_id"] == case.id:
        raise HTTPException(409, "这是此窗口打开的工程文件。完成编辑后可关闭窗口。需要移除磁盘文件时，请在文件管理器中操作。")
    record_audit(db, action="case.delete", resource_type="case", resource_id=str(case.id))
    db.delete(case)
    db.commit()
    return None


def write_case_snapshot(bind, case_id, principal, destination, opening_revision_id=None):
    with bind.connect() as conn:
        if conn.dialect.name == "sqlite":
            conn.exec_driver_sql("BEGIN")
        elif conn.dialect.name == "postgresql":
            conn = conn.execution_options(isolation_level="REPEATABLE READ")
        with Session(conn) as snapshot:
            case = get_case_or_404(case_id, snapshot)
            assert_case_view(snapshot, case, principal)
            write_package(snapshot, case, destination, principal, opening_revision_id)
            return case.title


@router.get("/{case_id}/project-file")
@router.get("/{case_id}/bundle")
def export_case_bundle(
    case_id: int,
    request: Request,
    revision_id: int | None = Query(None, gt=0),
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    """Capture one committed case snapshot, including the bytes behind its evidence."""
    bind = db.get_bind()
    tmp = NamedTemporaryFile(prefix=f"laserclaw-case-{case_id}-", suffix=".zip", delete=False)
    tmp.close()
    try:
        case_title = write_case_snapshot(bind, case_id, principal, tmp.name, revision_id)
        native = request.url.path.endswith("/project-file")
        title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", case_title).strip(" .")[:100] or "BeamWeft"
        if re.match(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", title, re.I):
            title = "_" + title
        filename = f"{title}.lclaw" if native else f"beamweft-case-{case_id}-bundle.zip"
        record_audit(db, action="case.file.save" if native else "case.bundle.export", resource_type="case", resource_id=str(case_id))
        db.commit()
        return FileResponse(tmp.name, filename=filename, media_type="application/octet-stream" if native else "application/zip",
                            background=BackgroundTask(os.unlink, tmp.name))
    except Exception as exc:
        os.unlink(tmp.name)
        if isinstance(exc, PackageError):
            if "text/html" in request.headers.get("accept", ""):
                return PlainTextResponse(f"案例未能导出\n\n{exc}\n\n关闭此标签页即可回到原案例。", status_code=409)
            raise HTTPException(409, str(exc)) from exc
        raise

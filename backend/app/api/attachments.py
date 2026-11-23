"""Attachment API routes."""
from __future__ import annotations

import logging
import mimetypes
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..attachment_evidence import OriginalRemoved, retained_by
from ..attachment_files import TEXT_EXTENSIONS, archive_location, file_mode, image_mime, read_file, tabular_text, text_sample
from ..auth.acl import assert_case_edit, assert_case_view
from ..auth.security import Principal, get_current_principal
from ..config import get_settings
from ..database import get_db
from ..knowledge.ingestion import create_attachment_source
from ..knowledge.ingestion import create_generated_content_source
from ..models import Attachment, ExperimentCase, GeneratedContent
from ..observability.audit import record_audit
from ..providers import get_ai_provider
from ..schemas import AttachmentResponse
from .cases import draft_scope
from .original_io import download_file, restore_file, write_upload

router = APIRouter()
settings = get_settings()

logger = logging.getLogger(__name__)


def _receipt(db, case_id, key, logical_path, digest):
    if not key:
        return None
    existing = db.query(Attachment).filter_by(case_id=case_id, upload_id=key).one_or_none()
    if existing and (existing.relative_path != logical_path or existing.content_hash != digest):
        raise HTTPException(409, "这次上传的回执对应另一份文件。请重新选择要归档的原件。")
    return existing


@router.post("/cases/{case_id}/attachments", response_model=AttachmentResponse, status_code=status.HTTP_201_CREATED)
def upload_attachment(
    case_id: int,
    file: UploadFile = File(...),
    relative_path: str | None = Form(None),
    upload_id: uuid.UUID | None = Form(None),
    expected_scope: str | None = Form(None),
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    """Archive original bytes; optional extraction never changes the original."""
    case = db.query(ExperimentCase).filter(ExperimentCase.id == case_id).first()
    if not case:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Case {case_id} does not exist")
    assert_case_edit(db, case, principal)
    if expected_scope is not None and expected_scope != draft_scope(case, principal):
        raise HTTPException(409, "工程或登录身份已变更。请回到发起上传的工程与账号，再重试未确认文件。")

    try:
        name, logical_path = archive_location(file.filename, relative_path)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    mode = file_mode(name)
    suffix = Path(name).suffix.lower() if mode != "archive" else ".bin"
    root = Path(settings.upload_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{uuid.uuid4()}{suffix}"
    mime = (mimetypes.guess_type(name)[0] if mode != "archive" else None) or "application/octet-stream"
    key = str(upload_id) if upload_id else None
    created = False
    committed = False
    try:
        with path.open("xb") as handle:
            created = True
            checksum = write_upload(file, handle, settings.max_upload_size)
        existing = _receipt(db, case_id, key, logical_path, checksum)
        if existing:
            return existing
        if mode == "archive":
            with path.open("rb") as original:
                identified = image_mime(original)
                if identified:
                    mime = identified
                elif tabular_text(text_sample(original)):
                    mime = "text/plain"
        item = Attachment(case_id=case_id, filename=name, relative_path=logical_path, upload_id=key,
                          filepath=str(path), file_type=mime, content_hash=checksum)
        db.add(item)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            existing = _receipt(db, case_id, key, logical_path, checksum)
            if existing:
                return existing
            raise
        if suffix in TEXT_EXTENSIONS:
            try:
                with db.begin_nested():
                    create_attachment_source(db, item)
            except Exception:
                logger.warning("Attachment %s could not be indexed", item.id, exc_info=True)
                item.index_note = "原件已归档；文字索引未建立，可下载原件或补充可读副本。"
        record_audit(db, action="attachment.upload", resource_type="attachment", resource_id=str(item.id))
        db.commit()
        committed = True
        return item
    finally:
        if created and not committed:
            path.unlink(missing_ok=True)


@router.get("/attachments/{attachment_id}/preview")
def preview_attachment(attachment_id: int, offset: int = Query(0, ge=0), limit: int = Query(4000, ge=100, le=20000),
                       db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    item = db.get(Attachment, attachment_id)
    if item is None or item.case is None:
        raise HTTPException(404, "文件记录不存在。")
    assert_case_view(db, item.case, principal)
    result = read_file(item, offset, limit)
    if images := result.pop("_images", []):
        result["image_url"] = images[0]["data_url"]
    return result


@router.put("/attachments/{attachment_id}/original", response_model=AttachmentResponse)
def restore_original(attachment_id: int, file: UploadFile = File(...), db: Session = Depends(get_db),
                     principal: Principal = Depends(get_current_principal)):
    item = db.get(Attachment, attachment_id)
    if item is None or item.case is None:
        raise HTTPException(404, "原件记录不存在。")
    assert_case_edit(db, item.case, principal)
    return restore_file(db, item, file, settings.max_upload_size, "attachment", principal)


@router.post("/attachments/{attachment_id}/analyze", response_model=dict)
async def analyze_attachment_image(
    attachment_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    """Analyze an uploaded case image and save the result as generated content."""
    attachment = db.query(Attachment).filter(Attachment.id == attachment_id).first()
    if not attachment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Attachment {attachment_id} does not exist")
    case = db.query(ExperimentCase).filter(ExperimentCase.id == attachment.case_id).first()
    if case:
        assert_case_edit(db, case, principal)
    if file_mode(attachment.filename, attachment.file_type) != "image":
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Only image attachments can be analyzed")
    result = await run_in_threadpool(read_file, attachment, 0, 0, raw_image=True)
    if not result.get("_images"):
        code = {"original_mismatch":409, "missing_original":404, "storage_error":503}.get(result.get("failure_code"), 422)
        raise HTTPException(code, result["message"])
    image = result["_images"][0]
    mime, image_data, display_copy = image["mime"], image["data"], image["display_copy"]
    context = {
        "filename": attachment.filename,
        "relative_path": attachment.relative_path or attachment.filename,
        "source_sha256": result["content_hash"],
        "mime_type": mime,
        "original_mime_type": attachment.file_type,
        "representation": "First-frame display copy, high-bit-depth grayscale stretched per frame; use original values for quantitative analysis" if display_copy else "Original image",
        "case": {
            "id": case.id,
            "title": case.title,
            "description": case.description,
            "cavity_type": case.cavity_type,
            "goal": case.goal,
            "parameters": case.parameters or {},
            "symptoms": case.symptoms or [],
        } if case else None,
    }
    content = await get_ai_provider().analyze_image(image_data, mime, context)
    content["attachment_id"] = attachment.id
    content["source_sha256"] = result["content_hash"]
    content["representation"] = context["representation"]
    generated = GeneratedContent(
        case_id=attachment.case_id,
        content_type="image_analysis",
        content=content,
        prompt_version="image_analysis_v1",
    )
    db.add(generated)
    try:
        db.flush()
    except OriginalRemoved as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    create_generated_content_source(db, generated)
    record_audit(db, action="attachment.analyze_image", resource_type="attachment", resource_id=str(attachment.id))
    db.commit()
    db.refresh(generated)
    return {"generated_content_id": generated.id, "content": generated.content}


@router.get("/cases/{case_id}/attachments", response_model=list[AttachmentResponse])
async def list_attachments(
    case_id: int,
    attachment_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    """List attachments for a case."""
    case = db.query(ExperimentCase).filter(ExperimentCase.id == case_id).first()
    if not case:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Case {case_id} does not exist")
    assert_case_view(db, case, principal)
    files = db.query(Attachment).filter_by(case_id=case_id)
    if attachment_id is not None:
        files = files.filter_by(id=attachment_id)
    return files.all()


@router.get("/attachments/{attachment_id}")
def download_attachment(
    attachment_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    """Download an attachment."""
    attachment = db.query(Attachment).filter(Attachment.id == attachment_id).first()
    if not attachment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Attachment {attachment_id} does not exist")
    case = db.query(ExperimentCase).filter(ExperimentCase.id == attachment.case_id).first()
    if case:
        assert_case_view(db, case, principal)
    return download_file(db, attachment, "attachment", attachment.uploaded_at)


@router.delete("/attachments/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attachment(
    attachment_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    """Delete an attachment and its indexed knowledge."""
    attachment = db.query(Attachment).filter(Attachment.id == attachment_id).first()
    if not attachment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Attachment {attachment_id} does not exist")
    case = db.query(ExperimentCase).filter(ExperimentCase.id == attachment.case_id).first()
    if case:
        assert_case_edit(db, case, principal)
    use = retained_by(db, attachment)
    if use:
        db.rollback()
        raise HTTPException(409, f"这份原件已被{use}引用，复核时需要保留。可另行上传更正后的文件。")
    path = Path(attachment.filepath)
    record_audit(db, action="attachment.delete", resource_type="attachment", resource_id=str(attachment.id))
    db.delete(attachment)
    db.commit()
    # A failed database commit must leave the original readable. The stored
    # name is unique to this upload, so removal cannot touch a replacement.
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Removed attachment %s; could not clean up its stored file", attachment_id, exc_info=True)
    return None

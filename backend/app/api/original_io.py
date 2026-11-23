"""Shared HTTP handling for archived project evidence."""
import hashlib
import os
import tempfile
from contextlib import ExitStack, contextmanager
from datetime import timezone
from email.utils import format_datetime
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import update

from ..attachment_files import OriginalMismatch, verified_original
from ..observability.audit import record_audit


def write_upload(file, target, max_size):
    digest, size = hashlib.sha256(), 0
    while chunk := file.file.read(1024 * 1024):
        size += len(chunk)
        if size > max_size:
            raise HTTPException(413, f"文件超过单件上限（{max_size} 字节）。")
        target.write(chunk)
        digest.update(chunk)
    return digest.hexdigest()


def restore_file(db, item, file, max_size, resource, principal):
    expected = item.content_hash
    if not expected:
        raise HTTPException(409, "此旧原件没有归档指纹，无法确认备份身份。请作为独立原件上传。")
    destination = Path(item.filepath)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".restore-") as folder:
            candidate = Path(folder) / "candidate"
            with candidate.open("xb") as target:
                actual = write_upload(file, target, max_size)
            if actual != expected.lower():
                raise HTTPException(409, "这份备份与归档时的 SHA-256 不符。请另选备份，或将新测量作为独立原件上传。")
            model = type(item)
            locked = db.execute(update(model).where(model.id == item.id, model.content_hash == expected)
                                .values(content_hash=model.content_hash))
            if not locked.rowcount:
                raise HTTPException(409, "原件记录已发生变化，请重新打开实验资料。")
            os.replace(candidate, destination)
    except OSError as exc:
        raise HTTPException(503, "未能写入归档位置，请检查可用空间或稍后重试。") from exc
    record_audit(db, action=resource + ".restore", resource_type=resource, resource_id=str(item.id),
                 user_id=principal.user_id, metadata={"sha256":actual})
    db.commit()
    return item


@contextmanager
def checked_original(item):
    with ExitStack() as scope:
        try:
            checked = scope.enter_context(verified_original(item))
        except FileNotFoundError as exc:
            raise HTTPException(404, f"找不到归档原件 {item.filename}（#{item.id}），请从备份恢复。") from exc
        except OriginalMismatch as exc:
            raise HTTPException(409, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(503, "读取归档失败，请检查存储是否可用。") from exc
        yield checked


class _OriginalDownload(FileResponse):
    """Retain the verified snapshot through range responses and disconnects."""

    def __init__(self, path, owner, **options):
        super().__init__(path, **options)
        self.owner = owner

    async def __call__(self, scope, receive, send):
        try:
            extensions = {k:v for k,v in scope.get("extensions", {}).items() if k != "http.response.pathsend"}
            await super().__call__({**scope, "extensions":extensions}, receive, send)
        finally:
            self.owner.close()


def download_file(db, item, resource, archived_at):
    with ExitStack() as scope:
        path, digest = scope.enter_context(checked_original(item))
        record_audit(db, action=resource + ".download", resource_type=resource, resource_id=str(item.id))
        db.commit()
        headers = {"X-Content-Type-Options":"nosniff", "ETag":f'"{digest}"'}
        if archived_at:
            utc = archived_at.astimezone(timezone.utc) if archived_at.tzinfo else archived_at.replace(tzinfo=timezone.utc)
            headers["Last-Modified"] = format_datetime(utc, usegmt=True)
        return _OriginalDownload(path, scope.pop_all(), filename=item.filename, media_type=item.file_type, headers=headers)

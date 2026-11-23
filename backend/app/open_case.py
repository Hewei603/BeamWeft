"""Restore an exported case into its own editable workspace, preserving scientific references."""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import DateTime, MetaData, create_engine
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError

from .attachment_files import transfer
from .case_package import FIELDS, FORMAT, HISTORY, METADATA_LIMIT, TOTAL_LIMIT, VIEW, PackageError, fields
from .database import Base
from .native_document import MARKER, document_workspace
from .workbench.assemblies import part_groups
from .models import (
    AgentChatMessage, AgentChatSession, AgentStep, AgentTask, AgentToolCall, Attachment, CaseComponentItem,
    CaseModule, CaseModuleFile, CoatingSpec, ComponentModelRevision, ExperimentCase, GeneratedContent,
    InventoryItem, KnowledgeChunk, KnowledgeSource,
)

def _id(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise PackageError("案例包记录编号无效。")
    return value


def _insert(conn, model, source, **local):
    values = {k: source[k] for k in fields(model) if k in source}
    values.update(local)
    _id(values.get("id"))
    for column in model.__table__.columns:
        if isinstance(column.type, DateTime) and values.get(column.name):
            values[column.name] = datetime.fromisoformat(values[column.name])
    conn.execute(model.__table__.insert().values(**values))


def _read_json(archive, name, checksum=None):
    member = archive.getinfo(name)
    if member.file_size > METADATA_LIMIT:
        raise PackageError("案例包的记录表超过 128 MiB 读取上限。")
    data = archive.read(member)
    if checksum is not None and hashlib.sha256(data).hexdigest() != checksum:
        raise PackageError(f"{name} 的指纹与案例清单不符。")
    return json.loads(data)


def opening_revision(archive):
    try:
        member = archive.getinfo(VIEW)
    except KeyError:
        return None
    if member.file_size > 1024:
        raise PackageError("工程文件的起始视图记录过大。")
    try:
        return _id(json.loads(archive.read(member))["revision_id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise PackageError("工程文件的起始光路版本无效。") from exc


def _store_file(archive, row, member, stage, final, group, legacy):
    if not member:
        raise PackageError(f"案例包缺少原件 {row['filename']}。请在源工作台恢复原件后重新导出。")
    identifier = _id(row["id"])
    suffix = Path(row["filename"]).suffix
    safe_suffix = suffix.lower() if re.fullmatch(r"\.[a-zA-Z0-9]{1,12}", suffix) else ".bin"
    relative = Path("uploads") / group / (str(identifier) + safe_suffix)
    target = stage / relative
    target.parent.mkdir(exist_ok=True, parents=True)
    with archive.open(member) as src, target.open("xb") as dest:
        digest = transfer(src, dest)
    expected = row.get("content_hash")
    if (expected or not legacy) and digest != expected:
        raise PackageError(f"{row['filename']}（#{identifier}）内容与案例清单不符。请重新获取案例包。")
    return {"filepath": str(final / relative), "content_hash": digest}


def _fill(conn, archive, manifest, history, stage, final, legacy):
    case = manifest["case"]
    case_id = _id(case["id"])
    _insert(conn, ExperimentCase, case, visibility="private")
    for row in history.get("inventory_specs", []):
        _insert(conn, InventoryItem, row, quantity=0, location="源案例规格 · 实物待核对")
    missing = set(history.get("unavailable_inventory_ids", []))
    if legacy:
        for generated in manifest["generated_contents"]:
            if generated["content_type"] in {"optical_project", "optical_snapshot"}:
                for _, _, parts in part_groups(generated["content"]["project"]):
                    missing.update(p["inventory_id"] for p in parts if p.get("inventory_id") is not None)
    for identifier in missing:
        _insert(conn, InventoryItem, {"id": identifier, "category": "other", "name": f"来源库存 #{identifier} · 规格未随包提供"},
                quantity=0, condition="uncertain", parse_confidence="needs_review", location="源案例引用 · 实物待核对")
    for row in history.get("coatings", []):
        _insert(conn, CoatingSpec, row)
    for row in history.get("component_models", []):
        _insert(conn, ComponentModelRevision, row, owner_key="local")
    attachments = {}
    for row in manifest["attachments"]:
        stored = _store_file(archive, row, row.get("bundle_path"), stage, final, "case-files", legacy)
        _insert(conn, Attachment, row, case_id=case_id, **stored)
        attachments[row["id"]] = stored["filepath"]

    for row in manifest["generated_contents"]:
        _insert(conn, GeneratedContent, row, case_id=case_id)
    for row in manifest["case_modules"]:
        _insert(conn, CaseModule, row, case_id=case_id)
        for item in row.get("files", []):
            member = item.get("bundle_path") or f"modules/{row['id']}/{item['file_role']}/{item['filename']}"
            stored = _store_file(archive, item, member, stage, final, f"case_modules/{_id(row['id'])}", legacy)
            _insert(conn, CaseModuleFile, item, module_id=row["id"], **stored)
    for row in manifest["component_items"]:
        _insert(conn, CaseComponentItem, row, case_id=case_id)

    for row in manifest["knowledge_sources"]:
        source = dict(row)
        if legacy:
            for prefix, key in (("attachment:", "attachment_id"), ("generated_content:", "generated_content_id")):
                uri = source.get("uri") or ""
                if uri.startswith(prefix) and uri[len(prefix):].isdigit():
                    source[key] = int(uri[len(prefix):])
        meta = dict(source.get("metadata_json") or {})
        meta.pop("filepath", None)
        if source.get("attachment_id"):
            meta["filepath"] = attachments[source["attachment_id"]]
        _insert(conn, KnowledgeSource, source, case_id=case_id, metadata_json=meta)
    for row in history.get("chunks", []):
        _insert(conn, KnowledgeChunk, row)
    for row in history.get("sessions", []):
        _insert(conn, AgentChatSession, row, case_id=case_id)
    for row in history.get("tasks", []):
        if row["mode"] not in {"workbench", "design"} or row["status"] in {"pending", "running"}:
            raise PackageError("案例包包含未完成或未知类型的实验任务。请在源工作台重新导出。")
        _insert(conn, AgentTask, row, case_id=case_id)
    for name, model in (("messages", AgentChatMessage), ("steps", AgentStep), ("tool_calls", AgentToolCall)):
        for row in history.get(name, []):
            _insert(conn, model, row)
    return case_id


def _legacy_limit(node):
    """Old exports have no namespace watermark. Reserve their stored numeric references."""
    if isinstance(node, dict):
        result = 0
        for key, value in node.items():
            if key == "id" or key.endswith(("_id", "_ids")):
                values = value if isinstance(value, list) else [value]
                result = max([result, *(v for v in values if type(v) is int and 0 < v < 2**63 - 1)])
            result = max(result, _legacy_limit(value))
        return result
    if isinstance(node, list):
        return max((_legacy_limit(item) for item in node), default=0)
    return 0


def _restore(archive, stage, final, digest):
    entries = archive.infolist()
    if sum(e.file_size for e in entries) > TOTAL_LIMIT or len(entries) > 150_000:
        raise PackageError("案例包超过本版本的读取容量。")
    if any(n > 1 for n in Counter(e.filename for e in entries).values()):
        raise PackageError("同一成员在案例包中出现多次，请重新导出。")
    manifest = _read_json(archive, "manifest.json")
    selected = opening_revision(archive)
    if selected is not None and not any(
        row["id"] == selected and row["content_type"] in {"optical_project", "optical_snapshot"}
        for row in manifest["generated_contents"]
    ):
        raise PackageError("工程文件指定的起始光路版本缺失。请从发送方重新保存该工程。")
    legacy = "package_format" not in manifest
    limits = manifest.get("source_id_limits", {})
    history = {}
    if not legacy:
        fmt = manifest["package_format"]
        if fmt.get("name") != FORMAT or fmt.get("version") != 1:
            raise PackageError("当前软件不支持该案例包版本。请更新 BeamWeft 后重新打开。")
        if not re.fullmatch(r"[a-f0-9]{64}", manifest.get("history_sha256") or ""):
            raise PackageError("案例包缺少工作台历史的校验指纹。")
        history = _read_json(archive, HISTORY, manifest["history_sha256"])
        for key in ("tasks", "sessions", "messages", "steps", "tool_calls", "inventory_specs", "coatings", "chunks", "component_models"):
            if not isinstance(history[key], list):
                raise PackageError(f"案例包数据表 {key} 无效。")
    else:
        ceiling = _legacy_limit(manifest)
        limits = {m.__tablename__: ceiling for m in FIELDS}
    schema = MetaData()
    portable_tables = {model.__tablename__ for model in FIELDS}
    for table in Base.metadata.sorted_tables:
        copied = table.to_metadata(schema)
        if table.name in portable_tables:
            copied.dialect_options["sqlite"]["autoincrement"] = True
    engine = create_engine(URL.create("sqlite", database=str(stage / "laserclaw.db")))
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            schema.create_all(conn)
            case_id = _fill(conn, archive, manifest, history, stage, final, legacy)
            for model in FIELDS:
                name = model.__tablename__
                limit = limits.get(name, 0)
                if limit:
                    _id(limit)
                    updated = conn.exec_driver_sql("UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name=?", (limit, name))
                    if not updated.rowcount:
                        conn.exec_driver_sql("INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)", (name, limit))
    finally:
        engine.dispose()
    marker = {"sha256": digest, "case_id": case_id, "source_id_limits": limits, "opening_revision_id": selected,
              "legacy": legacy or bool(manifest["package_format"].get("legacy_origin")),
              "source_version": (manifest.get("package_format") or {}).get("app_version")}
    (stage / MARKER).write_text(json.dumps(marker), encoding="utf-8")
    return marker


def _existing(directory, digest):
    try:
        marker = json.loads((directory / MARKER).read_text(encoding="utf-8"))
        if marker["sha256"] == digest and (directory / "laserclaw.db").is_file():
            _id(marker["case_id"])
            return marker
    except (OSError, KeyError, ValueError, TypeError):
        pass
    raise PackageError(f"现有接收目录缺少完整工程记录。请保留该目录，并选择新的接收目录。\n{directory}")


def open_package(package, root):
    """Materialize once. Reopening the same package reuses local committed edits."""
    root = Path(root).resolve()
    root.mkdir(exist_ok=True, parents=True)
    with Path(package).open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
        try:
            folder, _ = document_workspace(root / digest, digest)
        except ValueError as exc:
            raise PackageError(str(exc)) from exc
        return open_stream(source, folder.parent, digest)


def open_stream(source, root, digest):
    """Open the same source handle that was hashed by the desktop launcher."""
    directory = root / digest
    if directory.exists():
        return directory, _existing(directory, digest)
    source.seek(0)
    try:
        with TemporaryDirectory(dir=root, prefix=".opening-") as tmp:
            stage = Path(tmp) / "workspace"
            stage.mkdir()
            with zipfile.ZipFile(source) as archive:
                marker = _restore(archive, stage, directory, digest)
            try:
                stage.rename(directory)
            except OSError:
                if directory.is_dir():
                    return directory, _existing(directory, digest)
                raise
        return directory, marker
    except PackageError:
        raise
    except (KeyError, ValueError, TypeError, AttributeError, zipfile.BadZipFile, SQLAlchemyError, EOFError, OverflowError, RuntimeError) as exc:
        raise PackageError("案例包的记录、编号关联或原件不完整。请从源案例重新导出。") from exc

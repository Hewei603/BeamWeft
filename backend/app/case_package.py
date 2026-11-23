"""Portable, case-scoped records. Local database and account settings stay with the installation."""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

from sqlalchemy import func

from .attachment_files import bundle_paths, transfer
from .native_document import MARKER
from .storage_paths import PACKAGE_FORMAT as FORMAT
from .models import (
    AgentChatMessage, AgentChatSession, AgentStep, AgentTask, AgentToolCall, Attachment,
    CaseComponentItem, CaseModule, CaseModuleFile, CoatingSpec, ComponentModelRevision, ExperimentCase,
    GeneratedContent, InventoryItem, KnowledgeChunk, KnowledgeSource,
)
from .version import APP_VERSION
from .workbench.assemblies import part_groups


class PackageError(ValueError):
    pass


# An explicit format contract in both directions. Adding an ORM field never
# implicitly exports it or grants a package permission to write it.
FIELDS = {
    ExperimentCase: "title description cavity_type goal status schema_version tags parameters symptoms measurements safety_notes conclusions created_at updated_at",
    Attachment: "filename relative_path file_type content_hash upload_id index_note evidence_use uploaded_at",
    GeneratedContent: "content_type content operation_key model prompt_version input_tokens output_tokens latency_ms cost_estimate generated_at",
    KnowledgeSource: "attachment_id generated_content_id source_type title uri content_hash governance_status version metadata_json reviewed_at created_at updated_at",
    KnowledgeChunk: "source_id chunk_index text token_count metadata_json embedding created_at",
    CaseModule: "module_type title status config_json result_json created_at updated_at",
    CaseModuleFile: "filename file_type file_role content_hash metadata_json created_at",
    CaseComponentItem: "module_id category name specification quantity unit purpose required owned procurement_status vendor part_number notes source created_at updated_at",
    AgentChatSession: "title created_at updated_at",
    AgentChatMessage: "session_id role content metadata_json created_at",
    AgentTask: "goal mode status risk_level final_content_id created_at updated_at",
    AgentStep: "task_id step_index title status rationale result_summary created_at updated_at",
    AgentToolCall: "task_id step_id tool_name input_json output_json status latency_ms error_message created_at",
    InventoryItem: "name category diameter_mm roc_mm roc_is_flat focal_length_mm thickness_mm dimensions cut_angle_theta_deg cut_angle_phi_deg cut_axis doping_pct material crystal_spec vendor raw_spec parse_confidence condition condition_manual",
    CoatingSpec: "item_id surface wl_min_nm wl_max_nm function value_type comparator value_pct raw_fragment",
    ComponentModelRevision: "family version receipt snapshot",
}
HISTORY = "workbench-history.json"
VIEW = "workbench-view.json"
METADATA_LIMIT = 128 * 1024**2
TOTAL_LIMIT = 8 * 1024**3


def fields(model):
    return ("id", *FIELDS[model].split())


def record(obj):
    row = {key: getattr(obj, key) for key in fields(type(obj))}
    if isinstance(row.get("metadata_json"), dict):
        row["metadata_json"] = {k: v for k, v in row["metadata_json"].items() if k != "filepath"}
    if isinstance(obj, AgentToolCall) and obj.tool_name in {"workbench_request", "design_request"}:
        row["input_json"] = {k: v for k, v in (obj.input_json or {}).items() if k not in {"principal", "execution_owner"}}
    return row


def _write_file(archive, obj, member):
    if isinstance(obj, CaseModuleFile):
        origin = f"模块「{obj.module.title}」的文件 {obj.filename}（#{obj.id}）"
        recovery = "请在「更多记录与工具」选择「模块」，从备份恢复此文件后再保存。"
    else:
        origin = f"原件 {obj.filename}（#{obj.id}）"
        recovery = "请到「实验资料」恢复原件后再保存。"
    if not obj.content_hash:
        recovery = "此历史文件没有归档指纹，无法验证替换件。请从完整工程备份中找回原件。"
    try:
        with open(obj.filepath, "rb") as source, archive.open(member, "w", force_zip64=True) as target:
            digest = transfer(source, target)
    except (FileNotFoundError, PermissionError) as exc:
        raise PackageError(f"无法读取{origin}。{recovery}") from exc
    if obj.content_hash and obj.content_hash.lower() != digest:
        raise PackageError(f"{origin}的内容与归档指纹不符。{recovery}")
    return {**record(obj), "content_hash": digest, "bundle_path": member}


def _optical_inputs(generated):
    from .workbench.observation import OpticalLayout
    from .workbench.study_layouts import study_layout_inputs

    for row in generated:
        if row.content_type in {"optical_project", "optical_snapshot"}:
            yield (row.content or {}).get("project") or {}
        elif row.content_type == "scientific_study":
            for source in study_layout_inputs(row.id, row.content or {}):
                if source["kind"] == "frozen":
                    frozen = row.content["inputs"][source["input_key"]]
                    try:
                        layout = OpticalLayout.model_validate(frozen)
                    except ValueError:
                        # The study is still exportable when an old extension used a different state schema.
                        continue
                    yield layout.model_dump(mode="json")


def _refs(generated):
    inventory, models = set(), set()
    for project in _optical_inputs(generated):
        for _, _, parts in part_groups(project):
            inventory.update(p["inventory_id"] for p in parts if p.get("inventory_id") is not None)
        paths = [project.get("system") or {}, *project.get("incident_paths", [])]
        for elements in [*(p.get("elements", []) for p in paths), (project.get("network") or {}).get("components", [])]:
            for elem in elements:
                origin = elem.get("component_origin")
                if origin:
                    models.add(origin["model_revision_id"])
    return inventory, models


def _history(db, case, generated, sources, principal):
    from .design import service as design_service
    from .workbench import service as workbench_service

    def rows(model, relation):
        return db.query(model).filter(relation).order_by(model.id).all()

    tasks = rows(AgentTask, (AgentTask.case_id == case.id) & AgentTask.mode.in_(["design", "workbench"]))
    active = design_service.ACTIVE_RUNS | workbench_service.ACTIVE_RUNS
    if any(task.status in {"pending", "running"} or task.id in active for task in tasks):
        raise PackageError("案例有正在运行的任务。请等待完成，或在工作台停止任务后导出。")
    task_ids = [task.id for task in tasks]
    calls = rows(AgentToolCall, AgentToolCall.task_id.in_(task_ids))
    params = case.parameters or {}
    sessions = {params.get("workbench_session_id"), params.get("design_session_id")}
    sessions.update((c.input_json or {}).get("session_id") for c in calls if c.tool_name == "workbench_request")
    shared = rows(AgentChatSession, (AgentChatSession.case_id == case.id) & AgentChatSession.id.in_(list(sessions)))
    inv, model_ids = _refs(generated)
    owner = "local" if principal.user_id is None else f"user:{principal.user_id}"
    inventory = rows(InventoryItem, InventoryItem.id.in_(inv))
    refs = rows(ComponentModelRevision, (ComponentModelRevision.id.in_(model_ids)) & (ComponentModelRevision.owner_key == owner))
    tables = {
        "sessions": shared,
        "messages": rows(AgentChatMessage, AgentChatMessage.session_id.in_([s.id for s in shared])),
        "tasks": tasks,
        "steps": rows(AgentStep, AgentStep.task_id.in_(task_ids)),
        "tool_calls": calls,
        "chunks": rows(KnowledgeChunk, KnowledgeChunk.source_id.in_([s.id for s in sources])),
        "inventory_specs": inventory,
        "coatings": rows(CoatingSpec, CoatingSpec.item_id.in_(inv)),
        "component_models": refs,
    }
    history = {name: [record(row) for row in values] for name, values in tables.items()}
    history["unavailable_inventory_ids"] = sorted(inv - {item.id for item in inventory})
    return history


def _write_json(archive, member, data):
    encoded = json.dumps(data, ensure_ascii=False, default=lambda value: value.isoformat()).encode("utf-8")
    if len(encoded) > METADATA_LIMIT:
        raise PackageError("案例记录超过当前单个数据表的 128 MiB 上限。")
    archive.writestr(member, encoded)
    return hashlib.sha256(encoded).hexdigest()


def write_package(db, case, filename, principal, opening_revision_id=None):
    def case_rows(model):
        return db.query(model).filter_by(case_id=case.id).order_by(model.id).all()

    generated, sources = case_rows(GeneratedContent), case_rows(KnowledgeSource)
    if opening_revision_id is not None and not any(
        row.id == opening_revision_id and row.content_type in {"optical_project", "optical_snapshot"}
        for row in generated
    ):
        raise PackageError("所选光路版本不在这个工程中。请重新打开要交接的光路，再保存文件。")
    history = _history(db, case, generated, sources, principal)
    manifest = {
        "package_format": {"name": FORMAT, "version": 1, "app_version": APP_VERSION},
        "case": record(case), "attachments": [], "case_modules": [],
        "generated_contents": [record(item) for item in generated],
        "knowledge_sources": [record(item) for item in sources],
        "component_items": [record(item) for item in case_rows(CaseComponentItem)],
    }
    # A historical citation can refer outside the exported case. New edits in
    # a receiving workspace must never assign those source identifiers again.
    manifest["source_id_limits"] = {
        model.__tablename__: db.query(func.max(model.id)).scalar() or 0 for model in FIELDS
    }
    bind = db.get_bind()
    url = bind.engine.url
    if url.get_backend_name() == "sqlite" and url.database:
        marker = Path(url.database).parent / MARKER
        if marker.is_file():
            origin = json.loads(marker.read_text(encoding="utf-8"))
            if origin["case_id"] == case.id and origin["legacy"]:
                manifest["package_format"]["legacy_origin"] = True
            if origin.get("source_id_limits"):
                for name, limit in manifest["source_id_limits"].items():
                    manifest["source_id_limits"][name] = max(limit, origin["source_id_limits"].get(name, 0))
    from .attachment_evidence import issued_id_limit
    manifest["source_id_limits"]["attachments"] = max(manifest["source_id_limits"]["attachments"], issued_id_limit(db.connection()))
    with zipfile.ZipFile(filename, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in generated:
            kind = re.sub(r"[^a-zA-Z0-9_.-]", "_", item.content_type)
            _write_json(archive, f"generated/{kind}-{item.id}.json", item.content)
        attachments = case_rows(Attachment)
        paths = bundle_paths(attachments)
        for item in attachments:
            manifest["attachments"].append(_write_file(archive, item, paths[item.id]))
        for module in case_rows(CaseModule):
            files = [_write_file(archive, item, f"modules/{module.id}/{item.id}/{Path(item.filename).name}")
                     for item in sorted(module.files, key=lambda f: f.id)]
            manifest["case_modules"].append({**record(module), "files": files})
        manifest["history_sha256"] = _write_json(archive, HISTORY, history)
        if opening_revision_id is not None:
            _write_json(archive, VIEW, {"revision_id": opening_revision_id})
        _write_json(archive, "manifest.json", manifest)
        if sum(i.file_size for i in archive.infolist()) > TOTAL_LIMIT:
            raise PackageError("案例展开后超过当前可移交的 8 GiB 上限。")

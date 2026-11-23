"""Keep source identities and bytes stable for saved results and Agent reads."""
from sqlalchemy import BigInteger, cast, event, func, select, update

from .models.agent import AgentTask, AgentToolCall
from .models.attachment import Attachment, AttachmentIdWatermark
from .models.audit import AuditLog
from .models.experiment_case import ExperimentCase
from .models.generated_content import GeneratedContent
from .models.knowledge import KnowledgeSource


def issued_id_limit(connection):
    """Includes deleted IDs on old installations and reserved IDs in portable files."""
    sqlite = connection.dialect.name == "sqlite"
    if sqlite:
        last = connection.scalar(select(AttachmentIdWatermark.last_id).where(AttachmentIdWatermark.id == 1))
        if last is not None:
            return last
    audited = connection.scalar(select(func.max(cast(AuditLog.resource_id, BigInteger))).where(
        AuditLog.action.in_(("attachment.upload", "attachment.delete"))))
    if not sqlite:
        return audited or 0
    has_sequence = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
    ).scalar()
    reserved = connection.exec_driver_sql(
        "SELECT seq FROM sqlite_sequence WHERE name='attachments'"
    ).scalar() if has_sequence else 0
    return max(audited or 0, reserved or 0)


def next_sqlite_id(connection):
    # The first write takes SQLite's writer lock. Two concurrent uploads cannot
    # observe the same last ID, including when both initialize an old database.
    connection.exec_driver_sql("UPDATE attachment_id_watermark SET last_id=last_id WHERE 0")
    last = issued_id_limit(connection)
    present = connection.scalar(select(func.max(Attachment.id))) or 0
    number = max(last, present) + 1
    connection.exec_driver_sql(
        "INSERT INTO attachment_id_watermark(id,last_id) VALUES(1,?) "
        "ON CONFLICT(id) DO UPDATE SET last_id=excluded.last_id", (number,))
    return number


# Select compact source descriptors only. In particular, plotting a file or
# taking an inventory of the directory is different from citing every file.
OBSERVATIONS = ("image_analysis", "beam_observation")
CAUSTICS = ("beam_caustic_study", "beam_width_summary", "beam_caustic_check")
RESULT_FIELDS = (
    (OBSERVATIONS, ("attachment_id",)),
    (("power_measurement",), ("source_file", "attachment_id")),
    (("beam_comparison", "prediction_check"), ("source", "attachment_id")),
    (CAUSTICS, ("metadata", "files")),
    (("scientific_study",), ("originals",)),
    (("scientific_study",), ("connections",)),
    (("plan",), ("engineering_design", "intake_files")),
    (("plan",), ("engineering_design", "facts")),
)


def result_ids(path, value):
    if path[-1] == "attachment_id":
        return [value] if type(value) is int else []
    if path[-1] in {"facts", "connections"} and not isinstance(value, dict):
        return []
    if path[-1] == "facts":
        found = []
        for fact in value.values():
            if not isinstance(fact, dict):
                continue
            descriptor = fact.get("source_file")
            identifier = descriptor.get("attachment_id") if isinstance(descriptor, dict) else None
            if type(identifier) is int:
                found.append(identifier)
            elif isinstance(source := fact.get("source"), str):
                kind, _, number = source.partition(":")
                if kind in {"image", "file"} and number.isascii() and number.isdecimal() and len(number) <= 19:
                    found.append(int(number))
        return found
    if path[-1] == "connections":
        values = (binding.get("table") for binding in value.values() if isinstance(binding, dict))
    else:
        values = value if isinstance(value, list) else ()
    return [src["attachment_id"] for src in values if isinstance(src, dict) and type(src.get("attachment_id")) is int]


def argument_ids(args):
    args = args if isinstance(args, dict) else {}
    values = [args.get("attachment_id")]
    for name in ("attachment_ids", "source_attachment_ids"):
        if isinstance(args.get(name), list):
            values.extend(args[name])
    return {i for i in values if type(i) is int and i > 0}


def citation_ids(citations):
    return {c["source_id"] for c in citations or () if isinstance(c, dict) and type(c.get("source_id")) is int}


def retained_by(db, item):
    # Take the same row/write lock as the save hooks before checking older,
    # unindexed receipts. All locks are held through deletion or rollback.
    db.execute(update(Attachment).where(Attachment.id == item.id).values(evidence_use=Attachment.evidence_use))
    note = db.scalar(select(Attachment.evidence_use).where(Attachment.id == item.id))
    if note:
        return note
    facts = db.scalar(select(ExperimentCase.parameters["design_facts"]).where(ExperimentCase.id == item.case_id))
    if item.id in result_ids(("facts",), facts):
        return "工程中的设计参数"
    for kinds, path in RESULT_FIELDS:
        column = GeneratedContent.content[path]
        query = select(GeneratedContent.id, column).where(
            GeneratedContent.case_id == item.case_id, GeneratedContent.content_type.in_(kinds))
        if path[-1] == "attachment_id":
            query = query.where(column.as_integer() == item.id)
        for identifier, value in db.execute(query).yield_per(128):
            if item.id in result_ids(path, value):
                return f"已保存的结果 #{identifier}"
    args = AgentToolCall.input_json
    query = select(AgentToolCall.task_id, args["attachment_id"], args["attachment_ids"], args["source_attachment_ids"]).join(
        AgentTask, AgentTask.id == AgentToolCall.task_id).where(
            AgentTask.case_id == item.case_id, AgentToolCall.status != "failed")
    for task, scalar, selected, sources in db.execute(query).yield_per(128):
        if item.id in argument_ids(dict(attachment_id=scalar, attachment_ids=selected, source_attachment_ids=sources)):
            return f"Agent 任务 #{task}"
    source_ids = set(db.scalars(select(KnowledgeSource.id).where(KnowledgeSource.attachment_id == item.id)))
    if source_ids:
        citations = select(AgentToolCall.task_id, AgentToolCall.output_json["citations"]).join(AgentTask).where(
            AgentTask.case_id == item.case_id, AgentToolCall.tool_name == "search_knowledge")
        for task, refs in db.execute(citations).yield_per(128):
            if source_ids & citation_ids(refs):
                return f"Agent 任务 #{task} 的资料引用"
    return None


def pin(connection, case_id, identifiers, note, *, required):
    for identifier in sorted(identifiers):
        result = connection.execute(update(Attachment).where(
            Attachment.case_id == case_id, Attachment.id == identifier
        ).values(evidence_use=func.coalesce(Attachment.evidence_use, note)))
        if required and not result.rowcount:
            raise OriginalRemoved(f"原件 #{identifier} 已被移除，无法保存关联结果。请重新选择原件。")


class OriginalRemoved(ValueError):
    """A concurrent deletion won before the result could reserve its evidence."""


@event.listens_for(GeneratedContent, "after_insert")
def retain_result_sources(mapper, connection, record):
    identifiers = set()
    for kinds, path in RESULT_FIELDS:
        if record.content_type not in kinds:
            continue
        value = record.content
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        identifiers.update(result_ids(path, value))
    pin(connection, record.case_id, identifiers, f"已保存的结果 #{record.id}", required=True)


@event.listens_for(AgentToolCall, "after_insert")
@event.listens_for(AgentToolCall, "after_update")
def retain_agent_sources(mapper, connection, call):
    # Tool traces also log rejected inputs. Those must remain loggable even if
    # their submitted attachment number never existed or belongs to another case.
    if call.status in {"running", "failed"}:
        return
    ids = argument_ids(call.input_json)
    refs = citation_ids((call.output_json or {}).get("citations")) if call.tool_name == "search_knowledge" else set()
    if not ids and not refs:
        return
    case_id = connection.scalar(select(AgentTask.case_id).where(AgentTask.id == call.task_id))
    if refs:
        ids.update(connection.scalars(select(KnowledgeSource.attachment_id).where(
            KnowledgeSource.case_id == case_id, KnowledgeSource.id.in_(refs), KnowledgeSource.attachment_id.is_not(None))))
    pin(connection, case_id, ids, f"Agent 任务 #{call.task_id}", required=call.tool_name in {"workbench_request", "design_request"})

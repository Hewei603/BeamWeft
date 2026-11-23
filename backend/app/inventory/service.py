"""Shared inventory mutations for the UI and laboratory Agent.

Callers own the transaction. Inventory edits never rewrite saved optical projects.
"""
from copy import deepcopy

from ..auth.acl import can_manage_knowledge
from ..models import CoatingSpec, InventoryItem
from ..observability.audit import record_audit
from .crystals import CrystalSpec, ManualItemCreate, ManualItemPatch, normalized_manual_values


class InventoryPermissionError(ValueError):
    pass


class InventoryItemMissing(ValueError):
    pass


def _check_permission(principal):
    if not can_manage_knowledge(principal):
        raise InventoryPermissionError("新增或编辑元件需要 reviewer 或 admin 权限")


def _source_notes(provenance):
    if not provenance:
        return []
    refs = ",".join(str(i) for i in provenance.get("source_attachment_ids", [])) or "无"
    notes = [f"Agent来源：案例#{provenance['case_id']}，任务#{provenance['task_id']}，附件#{refs}；用户原话：{provenance['source_quote']}"]
    notes.extend("待核验："+note for note in provenance.get("uncertainty_notes", []))
    if provenance.get("unverified_fields"):
        notes.append("待核验：以下字段未作为确定参数保存："+"、".join(provenance["unverified_fields"]))
    if not provenance.get("stock_confirmed", True):
        notes.append("待核验：实物归属或数量未确认，可用库存暂记为0。")
    return notes


def _set_item(item, payload, provenance=None, *, replace_coatings=True):
    values = normalized_manual_values(payload)
    coating_rows = values.pop("coatings")
    for key, value in values.items():
        setattr(item, key, value)
    if replace_coatings:
        item.coatings = [CoatingSpec(**row) for row in coating_rows]
    notes = []
    if payload.category == "gain_crystal" and payload.crystal_spec is None:
        notes.append("晶体分段尚未录入；可先保存元件，补充材料和段长后再替换并仿真。")
    if payload.crystal_spec:
        incomplete = [str(i) for i, s in enumerate(payload.crystal_spec.segments, 1)
                      if s.material is None or s.role is None or s.length_mm is None]
        if incomplete:
            notes.append("第 " + "、".join(incomplete) + " 段的材料、作用或长度尚待补充；已有记录保留。")
        if any(s.uncertain for s in payload.crystal_spec.segments):
            notes.append("部分晶体标签或物性尚未确认；分段记录中的uncertain和note保留原始不确定性。")
        if any(s.role == "saturable_absorber" for s in payload.crystal_spec.segments):
            notes.append("含可饱和吸收体；初始透过率属于材料参数，未生成输出镜镀膜记录。")
        if payload.crystal_spec.coating_note in ("", "未知", "待确认"):
            notes.append("晶体端面镀膜未知。")
    if payload.category == "mirror" and not payload.roc_is_flat and payload.roc_mm is None:
        notes.append("曲率未知；不能把空值作为平面镜用于替换。")
    # A physical edit is independent of review of the original imported workbook.
    imported = bool(item.source_file and item.source_file not in {"手工录入","Agent录入"})
    notes.extend(n for n in (item.parse_notes or []) if imported or n.startswith(("Agent来源：","待核验：")))
    notes.extend(_source_notes(provenance))
    item.parse_notes = list(dict.fromkeys(notes))
    if not (imported and item.parse_confidence in {"needs_review","partial"}):
        item.parse_confidence = "partial" if notes else "parsed"


def create_item(db, payload: ManualItemCreate, principal, *, provenance=None):
    _check_permission(principal)
    item = InventoryItem(source_file="Agent录入" if provenance else "手工录入", condition="ok")
    _set_item(item, payload, provenance)
    db.add(item)
    db.flush()
    record_audit(db, action="inventory.create", resource_type="inventory", resource_id=str(item.id),
                 actor=principal.actor, user_id=principal.user_id,
                 metadata={"name": item.name, "category": item.category,
                           "source": "agent" if provenance else "manual", **deepcopy(provenance or {})})
    return item


def _update_specification(item, changes, provenance):
    previous = {key: getattr(item, key) for key in ManualItemCreate.model_fields if key != "coatings"}
    previous["quantity"] = previous["quantity"] if previous["quantity"] is not None else 1
    previous["roc_is_flat"] = bool(previous["roc_is_flat"])
    previous["coatings"] = [{key: getattr(c, key) for key in ("surface", "wl_min_nm", "wl_max_nm", "function", "value_type", "comparator", "value_pct", "raw_fragment")} for c in item.coatings]
    if item.crystal_spec:
        old = CrystalSpec.model_validate(item.crystal_spec)
        # Separate the independently supplied record from summaries regenerated on every save.
        if previous["dimensions"] == old.dimension_summary(previous["thickness_mm"]):
            previous["dimensions"] = None
        if old.overall_length_mm is None and previous["thickness_mm"] == old.total_length_mm:
            previous["thickness_mm"] = None
        if old.material_label is None and previous["material"] == old.material_summary:
            previous["material"] = None
        if previous["doping_pct"] == old.gain_doping:
            previous["doping_pct"] = None
    spec = changes.get("crystal_spec", item.crystal_spec)
    if spec is not None:
        spec = dict(spec)
        for summary, source in (("thickness_mm","overall_length_mm"), ("material","material_label")):
            if "crystal_spec" in changes and source in spec:
                if summary in changes and changes[summary] != spec[source]:
                    raise ValueError(f"元件字段 {summary} 和分段记录 {source} 对应同一项整件读数，请核对后保存。")
                changes[summary] = spec[source]
            elif summary in changes:
                spec[source] = changes[summary]
        changes["crystal_spec"] = spec
    if changes.get("roc_is_flat") is True and "roc_mm" not in changes:
        changes["roc_mm"] = None
    if changes.get("roc_mm") is not None and "roc_is_flat" not in changes:
        changes["roc_is_flat"] = False
    validated = ManualItemCreate.model_validate({**previous, **changes})
    _set_item(item, validated, provenance, replace_coatings="coatings" in changes and changes["coatings"] != previous["coatings"])


def update_item(db, item_id: int, payload: ManualItemPatch, principal, *, provenance=None):
    _check_permission(principal)
    item = db.get(InventoryItem, item_id)
    if item is None:
        raise InventoryItemMissing("元件不存在")
    changes = payload.supplied_changes()
    changed_fields = sorted(changes)
    if set(changes) <= {"location", "keeper", "quantity", "vendor", "name"}:
        # Moving a part does not re-parse its prescription or review its source.
        for key, value in changes.items():
            setattr(item, key, value)
        if provenance:
            notes = _source_notes(provenance)
            item.parse_notes = list(dict.fromkeys([*(item.parse_notes or []), *notes]))
            if item.parse_confidence == "parsed" and any(n.startswith("待核验：") for n in notes):
                item.parse_confidence = "partial"
    else:
        _update_specification(item, changes, provenance)
    db.flush()
    record_audit(db, action="inventory.update", resource_type="inventory", resource_id=str(item.id),
                 actor=principal.actor, user_id=principal.user_id,
                 metadata={"fields": changed_fields, "existing_project_revisions_unchanged": True,
                           "source": "agent" if provenance else "manual", **deepcopy(provenance or {})})
    return item

"""Case-owned optical projects. Every edit saves a new, reproducible revision."""
from copy import deepcopy
from contextlib import nullcontext
from threading import RLock
from types import SimpleNamespace
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import Field, model_validator
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from ..models import AgentChatMessage, AgentChatSession, AgentTask, ExperimentCase, GeneratedContent, InventoryItem, InventoryLoan
from ..inventory.crystals import CrystalSpec, crystal_geometry
from .optics import StrictInput, air_gap
from .authoring import EditableLayout, analyze_editable
from .station_outputs import with_station_links
from .component_selection import assess_stock, coating_note, locate_mount, read_medium_projection, stock_at_wavelength, stock_medium_spec, stock_token, validate_mounted_medium
from .stock_references import reconcile_stock_references, station_changes
from .stock_spacing import mount_spacing
from .crystal_indices import restore_crystal_indices
from .mounted_snapshots import forget_mount_warnings, installation_note, reconcile_legacy_assumptions, reconcile_removed_mounts
from .component_selection import restore_sample_index_basis
from .network import PortNetwork
from .assemblies import Assembly, Part, legacy_assemblies, object_keys, part_groups, validate_assemblies, validate_parts

_lock = RLock()


class ProjectRevisionConflict(ValueError):
    def __init__(self, message="Project changed since it was read. Reload the latest revision before saving."):
        super().__init__(message)



class OpticalProject(EditableLayout):
    title: str = Field(min_length=1, max_length=200)
    parts: list[Part] = Field(default_factory=list)
    assemblies: list[Assembly] = Field(default_factory=list, description="Declare consecutive primitives of each physical component on its path. Supply [] for independent pieces, including adjacent copies of the same stock item.")
    assumptions: list[str] = Field(default_factory=list)
    notes: str = ""
    component_context: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def previous_sample_evidence(cls, data):
        if not isinstance(data, dict) or not isinstance(data.get("component_context"), dict):
            return data
        context = data["component_context"]
        system = restore_sample_index_basis(data.get("system"), context)
        paths = data.get("incident_paths", [])
        by_path = context.get("path_contexts", {})
        if not isinstance(paths, list) or not isinstance(by_path, dict):
            return {**data, "system": system} if system is not data.get("system") else data
        recovered = [restore_sample_index_basis(p, by_path.get(p["uid"])) if isinstance(p, dict)
                     and isinstance(p.get("uid"), str) else p for p in paths]
        if system is not data.get("system") or any(a is not b for a, b in zip(paths, recovered)):
            return {**data, "system": system, "incident_paths": recovered}
        return data

    @model_validator(mode="after")
    def indices(self):
        uids = set()
        for element in self.system.elements:
            if not element.uid:
                element.uid = str(uuid4())
            if element.uid in uids:
                raise ValueError("Element identities must be unique")
            uids.add(element.uid)
        for path in self.incident_paths:
            for element in path.elements:
                if not element.uid:
                    element.uid = str(uuid4())
        validate_parts(self.system.elements, self.parts)
        validate_assemblies(self)
        return self


class SaveProject(StrictInput):
    base_revision_id: int | None = None
    project: OpticalProject
    change_note: str = Field(min_length=1, max_length=2000)
    unlock_quote: str = Field(default="", description="Only when explicitly authorized to change locked hardware: literal quote from current user request.")


class ReadProject(StrictInput):
    revision_id: int | None = Field(default=None, gt=0)
    case_head: bool = Field(default=False, description="Read latest official engineering revision instead of the input attached to this question.")

    @model_validator(mode="after")
    def unambiguous_model(self):
        if self.case_head and self.revision_id is not None:
            raise ValueError("请选择具体版本或当前主工程。")
        return self


class ProjectLookup(ReadProject):
    list_versions: bool = Field(default=False, description="List this case's saved projects and study snapshots with IDs, names and change notes.")
    before_id: int | None = Field(default=None, gt=0)
    variant_name: str | None = Field(default=None, min_length=1, max_length=160, description="Filter the version directory by part of a design alternative's name.")

    @model_validator(mode="after")
    def lookup_mode(self):
        if (self.before_id or self.variant_name) and not self.list_versions:
            raise ValueError("筛选版本目录时需选择 list_versions。")
        if self.list_versions and (self.case_head or self.revision_id):
            raise ValueError("读取版本目录时无需同时指定一个模型。")
        return self


class ReadInventoryItem(StrictInput):
    inventory_id: int = Field(gt=0)


class ReplaceCrystal(StrictInput):
    base_revision_id: int = Field(gt=0)
    inventory_id: int = Field(gt=0)
    orientation: Literal["as_listed", "reversed"] = "as_listed"
    spacing_policy: Literal["keep_gaps", "fixed_entrance", "fixed_exit"] = Field(default="keep_gaps",
        description="Mechanical mounting datum. keep_gaps retains external gaps; fixed_entrance compensates thickness in the following free air gap, fixed_exit in the preceding one. Coordinates are path-local.")
    change_note: str = Field(min_length=1, max_length=2000)


class ReplaceComponent(ReplaceCrystal):
    preview_token: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    target_path_uid: str = Field(default="primary", min_length=1, max_length=100,
                                 description="Owning path of the physical element: primary or a saved incident path UID.")
    target_role: Literal["crystal", "pump_input", "laser_output"] | None = None
    target_element_uid: str | None = Field(default=None, min_length=1, max_length=100,
        description="Any saved project element identity. A bound composite crystal selects its complete physical group.")

    @model_validator(mode="after")
    def has_target(self):
        if self.target_role is None and self.target_element_uid is None:
            raise ValueError("Select a target_element_uid or a compatible target_role")
        return self


class ReplaceComponentTool(ReplaceComponent):
    source_quote: str = Field(min_length=1, max_length=2000, description="Literal replacement instruction from this run's human request; do not quote an old chat or generated suggestion.")


class PreviewReplacement(ReplaceComponent):
    change_note: str = Field(default="预览库存元件替换", min_length=1, max_length=2000)


class ReplacementCandidates(StrictInput):
    revision_id: int = Field(gt=0)
    target_path_uid: str = Field(default="primary", min_length=1, max_length=100)
    target_element_uid: str | None = Field(default=None, min_length=1, max_length=100)
    target_role: Literal["crystal", "pump_input", "laser_output"] | None = None
    search: str = Field(default="", max_length=300)
    skip: int = Field(default=0, ge=0)
    limit: int = Field(default=30, ge=1, le=100)


class ReplaceCrystalTool(ReplaceCrystal):
    source_quote: str = Field(min_length=1, max_length=2000, description="Literal replacement instruction from this run's human request.")


def _project_history(db, case_id, *, include_studies=False):
    kinds = ("optical_project", "optical_snapshot") if include_studies else ("optical_project",)
    return (db.query(GeneratedContent).filter_by(case_id=case_id)
            .filter(GeneratedContent.content_type.in_(kinds)).order_by(GeneratedContent.id.desc()))


def revisions(db, case_id):
    return _project_history(db, case_id).all()


def revision_summaries(db, case_id, *, include_studies=False, before=None, limit=None, variant_name=None):
    query = _project_history(db, case_id, include_studies=include_studies)
    if before is not None:
        query = query.filter(GeneratedContent.id < before)
    label = GeneratedContent.content["variant_name"].as_string()
    if variant_name:
        query = query.filter(label.icontains(variant_name, autoescape=True))
    rows = query.with_entities(
        GeneratedContent.id,
        GeneratedContent.content[("project", "title")].as_string().label("title"),
        GeneratedContent.content["change_note"].as_string().label("change_note"),
        GeneratedContent.generated_at.label("created_at"),
    )
    if include_studies:
        rows = rows.add_columns(GeneratedContent.content_type.label("kind"), label.label("variant_name"),
                                GeneratedContent.content["parent_revision_id"].as_integer().label("parent_revision_id"),
                                GeneratedContent.content["base_revision_id"].as_integer().label("base_revision_id"))
    if limit is not None:
        rows = rows.limit(limit)
    return [{**row._mapping, "created_at": row.created_at.isoformat() if row.created_at else None} for row in rows]


def model_version_index(db, case_id, before=None, variant_name=None):
    rows = revision_summaries(db, case_id, include_studies=True, before=before, limit=51, variant_name=variant_name)
    return {"items": rows[:50], "next_before_id": rows[49]["id"] if len(rows) > 50 else None}


def _with_assemblies(project):
    return project if "assemblies" in project else {**project, "assemblies":legacy_assemblies(project)}


_ANALYSIS_SUMMARY_FIELDS = ("mode", "status", "input_issues", "scope_warnings", "solver_receipt")


def _input_query(query, summary=False):
    r = GeneratedContent
    fields = [r.id.label("revision_id"), r.content_type, r.content["project"].label("project"),
              r.content["variant_name"].as_string().label("variant_name"),
              r.content["study_input"].label("study_input"),
              r.content["base_revision_id"].as_integer().label("base_revision_id"),
              r.content["source_revision_id"].as_integer().label("source_revision_id")]
    if summary:
        fields.append(r.content["change_note"].as_string().label("change_note"))
        fields.extend(r.content[("analysis",key)].label(key) for key in _ANALYSIS_SUMMARY_FIELDS)
    return query.with_entities(*fields)


def _input_record(row, summary=False):
    result = {"revision_id":row.revision_id, "model_kind":"draft" if row.content_type=="optical_snapshot" else "saved",
              "project":_with_assemblies(row.project), "variant_name":row.variant_name,
              "source_revision_id":row.source_revision_id, "base_revision_id":row.base_revision_id}
    if row.study_input is not None:
        result["study_input"] = row.study_input
    if summary:
        result["change_note"] = row.change_note
        result["analysis"] = {k:row._mapping[k] for k in _ANALYSIS_SUMMARY_FIELDS if row._mapping[k] is not None}
    return result


def current_project_input(db, case_id, *, summary=False):
    row = _input_query(_project_history(db, case_id), summary).first()
    if row is None:
        return None
    return _input_record(row, summary)


def read_project_inputs(db, case_id, ids, *, summary=False):
    """Fetch only modeling inputs and lineage, including when the archived field samples are large."""
    ids = set(ids) - {None}
    if not ids:
        return {}
    rows = _input_query(_project_history(db, case_id, include_studies=True).order_by(None).filter(GeneratedContent.id.in_(ids)), summary)
    models = {row.revision_id:_input_record(row, summary) for row in rows}
    if missing := ids-models.keys():
        raise ValueError("本工程的光路版本目录中找不到 " + "、".join(f"#{rid}" for rid in sorted(missing)) + "。")
    return models


def read_project(db, case_id, revision_id=None):
    row = db.get(GeneratedContent, revision_id) if revision_id else _project_history(db, case_id).first()
    if row is None:
        if revision_id:
            raise ValueError("Project revision does not exist")
        return None
    if row.case_id != case_id:
        raise ValueError("Project revision does not belong to this case")
    if row.content_type not in {"optical_project", "optical_snapshot"}:
        raise ValueError(f"#{row.id} 是本工程的 {row.content_type} 记录。可用 read_artifact 读取该记录，或在光路版本目录中选择工程输入。")
    content = row.content
    return {"revision_id": row.id, **content, "project":_with_assemblies(content["project"])}


def normalize_project(project, source=None):
    if "network" not in project.model_fields_set and source and source["project"].get("network") is not None:
        project = project.model_copy(update={"network": PortNetwork.model_validate(source["project"]["network"])})
    if "assemblies" not in project.model_fields_set:
        groups = source["project"]["assemblies"] if source else legacy_assemblies(project.model_dump())
        if source and groups:
            present = {"primary": {element.uid for element in project.system.elements},
                       **{p.uid: {element.uid for element in p.elements} for p in project.incident_paths}}
            groups = [g for g in groups if present.get(g.get("path_uid", "primary"), set()).intersection(g["element_uids"])]
        project = project.model_copy(update={"assemblies": [Assembly.model_validate(g) for g in groups]})
        validate_assemblies(project)
    if source:
        project = reconcile_removed_mounts(project, source["project"])
        project = reconcile_legacy_indices(project, source["project"])
    return project


def reconcile_legacy_indices(layout, original):
    """An editing client that predates wavelength tags may echo scalars from a saved project."""
    old_paths = {p["uid"]: p for p in original.get("incident_paths", [])}
    context = original.get("component_context") or {}
    main = restore_crystal_indices(layout.system, original["system"], original.get("parts", ()), context,
                                   getattr(layout, "parts", None))
    paths = list(layout.incident_paths)
    for i, path in enumerate(paths):
        old = old_paths.get(path.uid)
        if not old:
            continue
        local = (context.get("path_contexts") or {}).get(path.uid, {})
        paths[i] = path = restore_crystal_indices(path, old, old.get("parts", ()), local,
                                                   path.parts if "parts" in path.model_fields_set else None)
        if old.get("wavelength_nm") == path.wavelength_nm or "shared_index_wavelength_nm" in path.model_fields_set:
            continue
        old_values = {old["target_uid"]: old.get("target_n"),
                      **{s["target_uid"]: s.get("n") for s in old.get("downstream", [])}}
        retained = [uid for uid, value in path.shared_index_values().items() if value == old_values.get(uid)]
        if retained:
            paths[i] = path.model_copy()
            paths[i].expire_shared_indices(retained)
    if main is not layout.system or paths != layout.incident_paths:
        return layout.model_copy(update={"system":main,"incident_paths":paths})
    return layout


def evaluate_project(db, project, source=None):
    project = normalize_project(project,source)
    project = reconcile_legacy_assumptions(db, project, source)
    for parts in [project.parts, *(path.parts for path in project.incident_paths)]:
        for part in parts:
            if part.inventory_id is not None and db.get(InventoryItem, part.inventory_id) is None:
                raise ValueError("Inventory binding does not exist")
    _validate_component_snapshots(project)
    document = project.model_dump()
    return {"project": document, "analysis": calculate_project_outputs(project, document), "summary": project.title}


def calculate_project_outputs(layout, document):
    """Use frozen modeling inputs and their original object identities, including pre-ID archives."""
    output = analyze_editable(layout)
    context = document.get("component_context") or {}
    warnings = list(dict.fromkeys([
        *(context.get("scope_warnings") or []),
        *(["建模假设中含历史库存安装文字，缺少可核对的原始换件版本。请按当前实物绑定核对，原文已保留。"]
          if context.get("unverified_installation_notes") else []),
        *(note for path in layout.incident_paths for note in
          context.get("path_contexts", {}).get(path.uid, {}).get("scope_warnings", [])),
        *output.get("model_warnings", []),
        *output.get("sections", {}).get("model_warnings", []),
    ]))
    if warnings:
        output["scope_warnings"] = warnings
        output["scope"] = list(dict.fromkeys([*output["scope"], *warnings]))
    return with_station_links(document, output)


def _unchanged_element(old, element):
    if old.get("uid") and old["uid"] != element.uid:
        return False
    try:
        return type(element).model_validate({**old, "uid": element.uid}).model_dump() == element.model_dump()
    except ValueError:
        return False


def _unchanged_legacy_train(old_elements, old_parts, elements, parts):
    """A new note or an edit on a different path does not identify or exchange any old hardware."""
    if len(old_elements) != len(elements) or len(old_parts) != len(parts):
        return False
    try:
        if not all(_unchanged_element(old, element) for old, element in zip(old_elements, elements, strict=True)):
            return False
        return {p["element_index"]: Part.model_validate(p) for p in old_parts} == {p.element_index: p for p in parts}
    except ValueError:
        # An unrecognised historical property cannot prove the physical train stayed identical.
        return False


def _legacy_locked_part(old, part, old_elements, old_parts, elements, parts):
    index = part["element_index"]
    known = {e["uid"] for e in old_elements if e.get("uid")}
    locks = part["locked_parameters"]
    binding = part.get("role", ""), part.get("inventory_id")

    def matches(element, role, stock, locked):
        return (element.get("uid") not in known and element.get("kind") == old.get("kind")
                and (role, stock) == binding
                and all(key in locked and key in old and key in element and old[key] == element[key] for key in locks))

    # An old part's only recorded location is its index on this particular path.
    # Validate both directions so two anonymous copies cannot claim the same successor.
    original = [p["element_index"] for p in old_parts
                if matches(old_elements[p["element_index"]], p.get("role", ""), p.get("inventory_id"), p.get("locked_parameters", []))]
    possible = [p for p in parts if matches(elements[p.element_index].model_dump(), p.role, p.inventory_id, p.locked_parameters)]
    if original != [index] or len(possible) != 1 or possible[0].element_index != index:
        raise ValueError(f"旧工程的锁定元件「{old['name']}」（本光路第 {index + 1} 个元件）没有独立标识，"
                         "无法确认修改后仍是同一件实物。请保留原顺序、角色、库存和锁定参数先保存一版；"
                         "若存在多件无法区分的实物，请在工程编辑器中核对身份后明确解锁。")
    return possible[0]


def validate_locked_parts(before, project):
    paths = {"primary": (project.system.elements, project.parts),
             **{p.uid: (p.elements, p.parts) for p in project.incident_paths}}
    promoted = {}
    for path_uid, old_elements, old_parts in part_groups(before):
        elements, parts = paths.get(path_uid, ([], []))
        by_uid = {elements[p.element_index].uid: p for p in parts}
        by_index = {p.element_index: p for p in parts}
        unchanged = None
        for part in old_parts:
            if not part.get("locked_parameters"):
                continue
            old = old_elements[part["element_index"]]
            if old.get("uid"):
                updated = by_uid.get(old["uid"])
            else:
                if unchanged is None:
                    unchanged = _unchanged_legacy_train(old_elements, old_parts, elements, parts)
                updated = (by_index[part["element_index"]] if unchanged else
                           _legacy_locked_part(old, part, old_elements, old_parts, elements, parts))
            if updated is None:
                raise ValueError(f"Cannot remove a locked part without explicit unlock: {old['name']}")
            if updated.role != part.get("role", "") or updated.inventory_id != part.get("inventory_id"):
                raise ValueError("Cannot change locked part role or inventory binding")
            now = elements[updated.element_index].model_dump()
            for key in part["locked_parameters"]:
                if key not in old or key not in updated.locked_parameters or key not in now or now[key] != old[key]:
                    raise ValueError(f"Locked hardware parameter changed: {old['name']}, {key}")
            if path_uid == "primary" and not old.get("uid") and _unchanged_element(old, elements[updated.element_index]):
                promoted[part["element_index"]] = now
    # Case-level material and coating facts use the previous geometry to detect a changed part.
    # Carry only verified equivalents into that comparison. The archived model is untouched.
    if not promoted:
        return before
    originals = before["system"]["elements"]
    return {**before, "system": {**before["system"], "elements": [promoted.get(i, el) for i, el in enumerate(originals)]}}


def save_project(db, case_id, args, *, allow_unlock=False, commit=True, snapshot_lineage=None):
    project = args.project
    with _lock:
        previous = current_project_input(db, case_id)
        if args.base_revision_id != (previous["revision_id"] if previous else None):
            raise ProjectRevisionConflict()
        previous_geometry = previous["project"] if previous else None
        if previous and not allow_unlock:
            previous_geometry = validate_locked_parts(previous_geometry, project)
        data = evaluate_project(db, project, previous)
        data.update(parent_revision_id=args.base_revision_id, change_note=args.change_note)
        if snapshot_lineage:
            data.update(snapshot_lineage)
        # Separate server workers have separate Python locks. Exactly one successor
        # may claim a given base, including the first revision of an empty case.
        parent_key = f"optical_project:parent:{args.base_revision_id or 0}"
        row = GeneratedContent(case_id=case_id, content_type="optical_project", content=data,
                               prompt_version="optical-project-v1", operation_key=parent_key)
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            if db.query(GeneratedContent.id).filter_by(case_id=case_id, operation_key=parent_key).first():
                raise ProjectRevisionConflict() from None
            raise
        case = db.get(ExperimentCase, case_id)
        if case is not None:
            _sync_saved_project(case, data["project"], previous_geometry, row.id,
                                data["analysis"].get("scope_warnings", []))
        if commit:
            db.commit()
        return {"status": "completed", "revision_id": row.id, **data}


_CRYSTAL_FACT_KEYS = {
    "gain_material", "crystal_width_mm", "crystal_height_mm", "crystal_length_mm", "active_length_mm",
    "front_cap_mm", "rear_cap_mm", "doping_pct", "cut_axis", "ar_wavelengths_nm", "crystal_part",
    "crystal_index", "pump_index", "crystal_inventory_id", "pump_focus_depth_mm",
    "absorber_length_mm", "absorber_material", "absorber_transmission_pct", "absorber_initial_transmission_pct",
    "absorber_transmission_wavelength_nm", "absorber_initial_transmission_wavelength_nm",
    "has_saturable_absorber",
}


def _refresh_selected_facts(case, item, role, context):
    """A newly selected physical part supersedes old part facts, not the goal."""
    from ..design.brief import FIELDS
    params = deepcopy(case.parameters or {})
    facts = dict(params.get("design_facts") or {})
    quote = item.raw_spec or item.name

    def put(key, value):
        if key in FIELDS and value is not None:
            facts[key] = {"value": value, "source": f"inventory:{item.id}" if item.id is not None else "current_optical_project", "quote": quote,
                          "label": FIELDS[key][0], "unit": FIELDS[key][1], "verification": "user_selected_inventory" if item.id is not None else "project_model"}

    selection = dict(params.get("component_selection") or {})
    if item.id is not None:
        selection[role] = item.id
    else:
        selection.pop(role, None)
    params["component_selection"] = selection
    if role == "crystal":
        facts = {key: value for key, value in facts.items() if key not in _CRYSTAL_FACT_KEYS}
        spec = context["crystal_spec"]
        segments = spec["segments"]
        gain_indices = [i for i, s in enumerate(segments) if s["role"] == "gain"]
        complete_roles = bool(segments) and all(s.get("role") is not None for s in segments)
        gains = [segments[i] for i in gain_indices]
        material = ("+".join(dict.fromkeys(s["material"] for s in gains))
                    if complete_roles and gains and all(s.get("material") for s in gains) else None)
        put("gain_material", material)
        if material is not None and any(s.get("uncertain") for s in gains):
            facts["gain_material"]["verification"] = "conditional_inventory"
        put("crystal_inventory_id", item.id)
        put("crystal_part", item.name)
        put("crystal_width_mm", spec["width_mm"])
        put("crystal_height_mm", spec["height_mm"])
        put("crystal_length_mm", spec.get("overall_length_mm") or (sum(s["length_mm"] for s in segments) if segments else None))
        if complete_roles and gains:
            put("active_length_mm", sum(s["length_mm"] for s in gains))
            put("front_cap_mm", sum(s["length_mm"] for s in segments[:gain_indices[0]] if s["role"] == "undoped"))
            put("rear_cap_mm", sum(s["length_mm"] for s in segments[gain_indices[-1]+1:] if s["role"] == "undoped"))
        if len(gains) == 1 and complete_roles and not gains[0]["uncertain"]:
            put("doping_pct", gains[0].get("doping_pct"))
        if item.cut_axis:
            put("cut_axis", item.cut_axis)
        group_specs = [group["spec"] for group in context.get("crystal_groups",[])] or [spec]
        absorbers = [s for group_spec in group_specs for s in group_spec["segments"] if s["role"] == "saturable_absorber"]
        all_segments = [s for group_spec in group_specs for s in group_spec["segments"]]
        if absorbers or (all(group_spec["segments"] for group_spec in group_specs) and all(s.get("role") is not None for s in all_segments)):
            put("has_saturable_absorber", bool(absorbers))
        if absorbers:
            put("absorber_length_mm", sum(s["length_mm"] for s in absorbers))
            if all(s.get("material") for s in absorbers):
                put("absorber_material", "+".join(dict.fromkeys(s["material"] for s in absorbers)))
                if any(s.get("uncertain") for s in absorbers):
                    facts["absorber_material"]["verification"] = "conditional_inventory"
            if len(absorbers) == 1 and not absorbers[0]["uncertain"]:
                put("absorber_initial_transmission_pct", absorbers[0].get("initial_transmission_pct"))
                put("absorber_transmission_wavelength_nm", absorbers[0].get("initial_transmission_wavelength_nm"))
        if len(group_specs) > 1:
            for key in ("has_saturable_absorber","absorber_length_mm","absorber_material","absorber_initial_transmission_pct","absorber_transmission_wavelength_nm"):
                if key in facts:
                    facts[key].update(source="current_optical_project:crystal_groups",verification="project_snapshot",
                                      quote="当前工程逐块保存的晶体实物分段快照；并非单块增益晶体的参数。")
        if material is not None:
            params["gain_medium"] = material
        else:
            params.pop("gain_medium", None)
        params["current_crystal"] = {"inventory_id": item.id, "name": item.name,
                                     "orientation": context["crystal_orientation"], "spec": deepcopy(spec),
                                     "scope_warnings": list(context["scope_warnings"])}
        # The legacy scalar cavity adapter must not keep an old crystal cached.
        params.pop("crystal", None)
    else:
        prefix = "input" if role == "pump_input" else "output"
        inventory_key = "M1_inventory_id" if role == "pump_input" else "M2_inventory_id"
        for key in (f"{prefix}_roc_mm", f"{prefix}_mirror_flat", f"{prefix}_mirror_owned", inventory_key, "mirror_coatings_user_confirmed"):
            facts.pop(key, None)
        put(inventory_key, item.id)
        put(f"{prefix}_mirror_owned", True)
        put(f"{prefix}_mirror_flat", bool(item.roc_is_flat))
        if not item.roc_is_flat:
            put(f"{prefix}_roc_mm", item.roc_mm)
        if role == "laser_output":
            facts.pop("oc_transmission_pct", None)
            wavelength = context.get("wavelength_nm")
            exact = [c for c in item.coatings if c.surface in {"S1", "both"} and c.function == "PR"
                     and c.value_type == "T" and c.comparator == "=" and c.value_pct is not None
                     and wavelength is not None and c.wl_min_nm <= wavelength <= c.wl_max_nm]
            if len(exact) == 1:
                put("oc_transmission_pct", exact[0].value_pct)
        # Existing legacy mirror parameters refer to the same selected surfaces.
        params["R1_mm" if role == "pump_input" else "R2_mm"] = "flat" if item.roc_is_flat else item.roc_mm
    params["design_facts"] = facts
    params["current_component_context"] = deepcopy(context)
    case.parameters = params


def _role_signature(project, role):
    if not project:
        return []
    return [(project["system"]["elements"][p["element_index"]], p.get("inventory_id"), p.get("ownership", "proposed"))
            for p in sorted(project.get("parts", []), key=lambda p:p["element_index"]) if p.get("role", "") == role]


def _validate_component_snapshots(project):
    """Keep inventory metadata consistent with the selected physical parts."""
    _validate_path_snapshots(project, project.system, project.parts, project.component_context, "primary")
    contexts = project.component_context.get("path_contexts", {})
    for path in project.incident_paths:
        if path.uid in contexts:
            _validate_path_snapshots(project, path, path.parts, contexts[path.uid], path.uid)


def _validate_path_snapshots(project, optics, bindings, context, path_uid):
    if context:
        selected = {key:value for key,value in (context.get("component_selection") or {}).items()
                    if key.startswith("element:") and any(e.uid == key[8:] for e in optics.elements)}
        components = deepcopy(context.get("components") or {})
        for role in ("crystal", "pump_input", "laser_output"):
            parts = [p for p in bindings if p.role == role]
            if role == "crystal" and context.get("crystal_element_uids"):
                parts = [p for p in parts if optics.elements[p.element_index].uid in context["crystal_element_uids"]]
            ids = {p.inventory_id for p in parts}
            inventory_id = next(iter(ids)) if len(ids) == 1 else None
            if inventory_id is not None:
                selected[role] = inventory_id
            frozen = components.get(role)
            if frozen and frozen.get("inventory_id") != inventory_id:
                components.pop(role,None)
                forget_mount_warnings(context, role)
            elif frozen and role != "crystal" and len(parts) == 1:
                element = optics.elements[parts[0].element_index]
                frozen_radius = None if frozen.get("roc_is_flat") else frozen.get("roc_mm")
                if element.kind != "mirror" or element.radius_mm != frozen_radius:
                    raise ValueError("工程腔镜曲率与所选元件快照不一致；请先在元件库编辑并重新选用，不能沿用旧镜片参数。")
        context["components"] = components
        context["component_selection"] = selected
        generic_components = {key:value for key,value in components.items() if key.startswith("element:")}
        for key,frozen in generic_components.items():
            if not isinstance(frozen, dict):
                raise ValueError("库存实物快照格式不完整，请恢复完整版本或重新选用实物。")
            spec = read_medium_projection(frozen, key) if "medium_projection" in frozen else None
            index = next((i for i,e in enumerate(optics.elements) if e.uid == key[8:]),None)
            part = next((p for p in bindings if p.element_index == index),None)
            if index is None or part is None or part.inventory_id != frozen.get("inventory_id"):
                if spec is not None:
                    if any(optics.elements[p.element_index].uid in frozen["medium_projection"]["element_uids"]
                           and p.inventory_id == frozen.get("inventory_id") for p in bindings):
                        raise ValueError("库存介质只有部分分段仍关联原实物，请按同一实物管理各段。")
                components.pop(key,None)
                selected.pop(key,None)
                if warnings_by_role := context.get("warnings_by_role"):
                    warnings_by_role.pop(key, None)
                    context["scope_warnings"] = list(dict.fromkeys(w for rows in warnings_by_role.values() for w in rows))
                continue
            element = optics.elements[index]
            if spec is not None:
                warnings_by_role = context.setdefault("warnings_by_role", {})
                warnings_by_role[key] = validate_mounted_medium(project, optics, bindings, path_uid, frozen, spec)
                context["wavelength_nm"] = optics.wavelength_nm
                context["scope_warnings"] = list(dict.fromkeys(w for rows in warnings_by_role.values() for w in rows))
            if element.kind == "lens" and element.focal_mm != frozen.get("focal_length_mm"):
                raise ValueError("工程透镜焦距与库存快照不一致；请先编辑库存并重新选用该实物。")
            if element.kind == "mirror" and element.radius_mm != (None if frozen.get("roc_is_flat") else frozen.get("roc_mm")):
                raise ValueError("工程反射镜曲率与库存快照不一致；请先编辑库存并重新选用该实物。")
            selected[key] = part.inventory_id
    if not context.get("crystal_spec") and not context.get("crystal_groups"):
        return
    warnings = []
    if context.get("crystal_spec"):
        sections, warnings = _validate_crystal_group(project,optics,bindings,path_uid, context["crystal_spec"], context.get("crystal_element_uids"), context.get("crystal_inventory_id"))
        context["crystal_sections"] = sections
    context["wavelength_nm"] = optics.wavelength_nm
    for group in context.get("crystal_groups",[]):
        if group["element_uids"] == context.get("crystal_element_uids") and group["spec"] == context.get("crystal_spec") and group.get("inventory_id") == context.get("crystal_inventory_id"):
            continue
        _, group_warnings = _validate_crystal_group(project,optics,bindings,path_uid,group["spec"],group["element_uids"],group.get("inventory_id"))
        warnings += group_warnings
    keys = object_keys(project.model_dump(), optics.model_dump() if path_uid != "primary" else None)
    if len({keys[p.element_index] for p in bindings if p.role == "crystal"}) > 1:
        warnings.append("当前工程包含多块分离晶体：已逐块保留几何；单块晶体的库存事实不能代表整条光路，输出功率与动态未联合求解。")
    by_role = deepcopy(context.get("warnings_by_role") or {})
    if (frozen := context.get("components", {}).get("crystal")) and "coatings" in frozen:
        warnings.append(coating_note(optics.wavelength_nm, "gain_crystal", frozen["coatings"]))
    by_role["crystal"] = list(dict.fromkeys(warnings))
    context["warnings_by_role"] = by_role
    context["scope_warnings"] = list(dict.fromkeys(w for rows in by_role.values() for w in rows))


def _validate_crystal_group(project,optics,bindings,path_uid,raw_spec,element_uids,inventory_id):
    spec = CrystalSpec.model_validate(raw_spec)
    expected, sections, warnings = crystal_geometry(spec, optics.wavelength_nm,
                                              single_pass=path_uid != "primary" or optics.mode == "single_pass")
    parts = [p for p in sorted(bindings, key=lambda p:p.element_index) if p.role == "crystal"
             and (element_uids is None or optics.elements[p.element_index].uid in element_uids)]
    if element_uids is not None and {optics.elements[p.element_index].uid for p in parts} != set(element_uids):
        raise ValueError("晶体实物段身份与工程快照不一致，请重新选用；不能把另一块分离晶体并入该实物。")
    if element_uids:
        group = next((g for g in project.assemblies if g.path_uid == path_uid and element_uids[0] in g.element_uids), None)
        if (group.element_uids if group else element_uids[:1]) != element_uids:
            raise ValueError("工程中的实物边界与库存分段快照不一致，请核对所选实物。")
    actual = [optics.elements[p.element_index] for p in parts if optics.elements[p.element_index].length_mm > 0]
    cursor = 0
    for segment, section in zip(expected, sections, strict=True):
        length = 0.0
        while length < segment["length_mm"] - 1e-8 and cursor < len(actual):
            element = actual[cursor]
            if element.kind != "space" or (segment.get("n") is not None and element.n is not None
                                           and abs(element.n-segment["n"]) > 1e-7):
                raise ValueError("工程的晶体材料/折射率与分段快照不一致；请在元件库编辑并重新选用，不能保留矛盾的库存绑定。")
            if element.medium_role is not None and element.medium_role != segment["medium_role"]:
                raise ValueError("介质用途与所用器件的分段记录不一致，请核对该段来源。")
            ref = element.index_reference
            if ref and (ref.model != section["index_model"] or ref.wavelength_nm != optics.wavelength_nm):
                raise ValueError("晶体材料的色散分支或折射率标定波长与库存分段快照不一致，请核对所选实物。")
            length += element.length_mm
            cursor += 1
        if abs(length-segment["length_mm"]) > 1e-8:
            raise ValueError("工程的晶体段长与分段快照不一致；请通过元件库编辑/选用确认新结构，不能只改段长而沿用旧晶体参数。")
    if cursor != len(actual):
        raise ValueError("工程晶体总长与分段快照不一致，未保存。")
    ids = {p.inventory_id for p in parts}
    if ids != {inventory_id}:
        raise ValueError("晶体库存绑定与分段快照不一致；请重新选择元件，不能沿用另一晶体的属性。")
    return sections, warnings


def _sync_saved_project(case, project, previous, revision_id, scope_warnings):
    """Every save/restore owns one current snapshot; inventory remains mutable."""
    from ..design.brief import FIELDS
    params = deepcopy(case.parameters or {})
    old_selected = dict(params.get("component_selection") or {})
    context = deepcopy(project.get("component_context") or {})
    selection = {k:v for k,v in old_selected.items() if k not in {"crystal", "pump_input", "laser_output"} and not k.startswith("element:")}
    group_anchors = {uid:g["element_uids"][0] for g in project["assemblies"] if g["path_uid"] == "primary" for uid in g["element_uids"]}
    for part in project["parts"]:
        if part["role"] not in {"crystal", "pump_input", "laser_output"} and part.get("inventory_id") is not None:
            uid = project["system"]["elements"][part["element_index"]]["uid"]
            selection[f"element:{group_anchors.get(uid, uid)}"] = part["inventory_id"]
    for role in ("crystal", "pump_input", "laser_output"):
        role_parts = [p for p in project["parts"] if p["role"] == role]
        if role == "crystal" and context.get("crystal_element_uids"):
            role_parts = [p for p in role_parts if project["system"]["elements"][p["element_index"]]["uid"] in context["crystal_element_uids"]]
        ids = {p["inventory_id"] for p in role_parts if p.get("inventory_id") is not None}
        if len(ids) == 1:
            selection[role] = next(iter(ids))
    params.update(current_optical_project_revision_id=revision_id, current_optical_system=deepcopy(project["system"]),
                  current_component_context=context, component_selection=selection,
                  current_optical_scope_warnings=scope_warnings)
    case.parameters = params
    previous_context = (previous or {}).get("component_context") or {}
    crystal_changed = ((previous is not None and _role_signature(previous,"crystal") != _role_signature(project,"crystal"))
                       or previous_context.get("crystal_spec") != context.get("crystal_spec")
                       or (params.get("current_crystal") or {}).get("spec") != context.get("crystal_spec"))
    if crystal_changed:
        if context.get("crystal_spec"):
            frozen = (context.get("components") or {}).get("crystal") or {}
            record = SimpleNamespace(id=context.get("crystal_inventory_id"),
                name=frozen.get("name") or context.get("crystal_name") or "当前分段晶体", raw_spec=frozen.get("raw_spec"),
                cut_axis=frozen.get("cut_axis"))
            _refresh_selected_facts(case,record,"crystal",context)
        else:
            params = deepcopy(case.parameters)
            facts = {k:v for k,v in (params.get("design_facts") or {}).items() if k not in _CRYSTAL_FACT_KEYS}
            length = sum(e["length_mm"] for e,_,_ in _role_signature(project,"crystal"))
            if length > 0:
                facts["crystal_length_mm"] = {"value":length, "source":f"optical_project:{revision_id}",
                    "verification":"project_geometry", "quote":"当前工程中明确标为晶体的物理段长之和",
                    "label":FIELDS["crystal_length_mm"][0], "unit":"mm"}
            params["design_facts"] = facts
            params.pop("current_crystal",None)
            params.pop("gain_medium",None)
            params.pop("crystal",None)
            case.parameters = params
    for role,prefix,key in (("pump_input","input","M1_inventory_id"),("laser_output","output","M2_inventory_id")):
        signatures = _role_signature(project,role)
        prior_mirrors = _role_signature(previous,role)
        wavelength_changed = previous is not None and project["system"].get("wavelength_nm") != previous["system"].get("wavelength_nm")
        changed = ((previous is not None and prior_mirrors != signatures)
                   or old_selected.get(role) != selection.get(role)
                   or (role == "laser_output" and signatures and wavelength_changed))
        if not changed:
            continue
        params = deepcopy(case.parameters)
        facts = dict(params.get("design_facts") or {})
        for field in (f"{prefix}_roc_mm",f"{prefix}_mirror_flat",f"{prefix}_mirror_owned",key,"mirror_coatings_user_confirmed"):
            facts.pop(field,None)
        if role == "laser_output":
            facts.pop("oc_transmission_pct",None)
        scalar = "R1_mm" if role == "pump_input" else "R2_mm"
        if len(signatures) == 1 and signatures[0][0]["kind"] == "mirror":
            element,inventory_id,ownership = signatures[0]
            for field,value in ((f"{prefix}_mirror_flat",element["radius_mm"] is None),
                (f"{prefix}_roc_mm",element["radius_mm"]),(f"{prefix}_mirror_owned",ownership=="owned"),(key,inventory_id)):
                if value is not None:
                    facts[field] = {"value":value,"source":f"optical_project:{revision_id}","verification":"project_geometry",
                                    "quote":"当前工程的腔镜表面与库存绑定","label":FIELDS[field][0],"unit":FIELDS[field][1]}
            params[scalar] = element["radius_mm"] if element["radius_mm"] is not None else "flat"
            frozen = (context.get("components") or {}).get(role) or {}
            wavelength = project["system"]["wavelength_nm"]
            known_T = [c for c in frozen.get("coatings",[]) if c.get("surface") in {"S1","both"} and c.get("function")=="PR"
                       and c.get("value_type")=="T" and c.get("comparator")=="=" and c.get("value_pct") is not None
                       and wavelength is not None and c["wl_min_nm"] <= wavelength <= c["wl_max_nm"]]
            if role == "laser_output" and len(known_T)==1:
                facts["oc_transmission_pct"] = {"value":known_T[0]["value_pct"],"source":f"optical_project:{revision_id}",
                    "verification":"project_snapshot","quote":known_T[0].get("raw_fragment") or "该工程保存的输出镜T数值",
                    "label":FIELDS["oc_transmission_pct"][0],"unit":"%"}
        elif len(prior_mirrors) == 1 and prior_mirrors[0][0]["kind"] == "mirror":
            previous_radius = prior_mirrors[0][0].get("radius_mm")
            if params.get(scalar) == (previous_radius if previous_radius is not None else "flat"):
                params.pop(scalar, None)
        params["design_facts"] = facts
        case.parameters = params


def replace_component(db, case_id, request: ReplaceComponent, *, active_task_id=None, working=None, preview=False):
    """Explicit replacement changes only the selected role and appends a revision.

    The old crystal's UID and hardware lock are not reused for a different piece
    of hardware. All unrelated element identities, values and bindings survive.
    """
    with nullcontext() if preview else _lock:
        case_query = db.query(ExperimentCase).filter_by(id=case_id)
        case = (case_query if preview else case_query.with_for_update()).first()
        if case is None:
            raise ValueError("案例不存在")
        running = db.query(AgentTask).filter(AgentTask.case_id == case_id,
            AgentTask.status.in_(["pending", "running"]), AgentTask.mode.in_(["workbench", "design"]))
        if active_task_id is not None:
            running = running.filter(AgentTask.id != active_task_id)
        if not preview and running.first():
            raise ValueError("案例正在运行Agent任务，请完成或停止当前任务后再替换元件。")
        if working and working.get("revision_id") != request.base_revision_id:
            raise ValueError("请选择本轮正在分析的工程版本，或从其他版本重新开始。")
        if db.get(GeneratedContent, request.base_revision_id) is None:
            raise ProjectRevisionConflict()
        selected = read_project(db, case_id, request.base_revision_id)
        study_id = selected["revision_id"] if selected.get("model_kind") == "draft" else None
        if study_id is not None:
            from .study_inputs import basis
            working = basis(selected)
        previous = selected if study_id is not None or preview else read_project(db, case_id)
        if previous is None:
            raise ValueError("还没有已保存的光路，请先保存工程。")
        if previous["revision_id"] != request.base_revision_id:
            raise ProjectRevisionConflict()
        item_query = db.query(InventoryItem).options(selectinload(InventoryItem.coatings)).populate_existing().filter_by(id=request.inventory_id)
        item = (item_query if preview else item_query.with_for_update()).first()
        if item is None:
            raise ValueError("所选库存元件不存在，请刷新元件库。")
        project = deepcopy(previous["project"])
        original = previous["project"]
        path_uid = request.target_path_uid
        mount = locate_mount(project, path_uid, request.target_element_uid, request.target_role)
        optics, bindings, role, targets = mount.optics, mount.bindings, mount.role, mount.indices
        parts = bindings.setdefault("parts", [])
        elements = optics["elements"]
        old_optics = original["system"] if path_uid == "primary" else next(p for p in original["incident_paths"] if p["uid"] == path_uid)
        old_parts = original.get("parts", []) if path_uid == "primary" else old_optics.get("parts", [])
        loaned = float(db.query(func.coalesce(func.sum(InventoryLoan.quantity),0)).filter(
            InventoryLoan.item_id==item.id, InventoryLoan.returned_at.is_(None)).scalar() or 0)
        token = stock_token(request, item, loaned, mount)
        if request.preview_token and token != request.preview_token:
            raise ProjectRevisionConflict("库存资料、可用数量或安装条件与预览不一致。请重新预览该版本，再确认替换。")
        if reason := assess_stock(mount, item, loaned):
            raise ValueError(reason)
        def installed_uid(position):
            return str(uuid5(NAMESPACE_URL, f"beamweft:stock-install:{case_id}:{token}:{position}"))

        project_context = project.setdefault("component_context", {})
        context = project_context if path_uid == "primary" else project_context.setdefault("path_contexts", {}).setdefault(path_uid, {})
        old_total = sum(elements[i]["length_mm"] for i in targets)
        before_spacing = sum(e["length_mm"] for e in elements)
        warnings = []
        segmented = mount.category in {"gain_crystal", "medium"}
        if segmented:
            if targets != list(range(targets[0], targets[-1] + 1)):
                raise ValueError("所选实物的分段之间还有独立元件，请先整理实物边界。")
            spec = stock_medium_spec(item) if mount.category == "medium" else CrystalSpec.model_validate(item.crystal_spec)
            replacements, sections, warnings = crystal_geometry(spec, optics["wavelength_nm"], request.orientation,
                                                                 gain_mount=mount.category == "gain_crystal",
                                                                 single_pass=path_uid != "primary" or optics["mode"] == "single_pass")
            actual_spec = spec.model_dump()
            actual_spec["segments"] = [s.model_dump() for s in (list(reversed(spec.segments)) if request.orientation == "reversed" else spec.segments)]
            start, stop = targets[0], targets[-1] + 1
            new_parts = []
            for offset, element in enumerate(replacements):
                element["uid"] = installed_uid(offset)
                element["index_reference"] = {"model":sections[offset]["index_model"], "wavelength_nm":optics["wavelength_nm"]}
                new_parts.append({"element_index": start+offset, "role": role, "ownership": "owned", "inventory_id": item.id,
                    "source": f"用户明确选择库存#{item.id}：{item.name}；结构化分段与原始标签记录已保留。",
                    "locked_parameters": ["kind", "length_mm"],
                    "unknowns": sections[offset]["unknowns"]})
            delta = len(replacements) - (stop-start)
            preserved = []
            for part in parts:
                if part["element_index"] in targets:
                    continue
                if part["element_index"] >= stop:
                    part["element_index"] += delta
                preserved.append(part)
            optics["elements"] = elements[:start] + replacements + elements[stop:]
            bindings["parts"] = sorted(preserved+new_parts, key=lambda p:p["element_index"])
        if role == "crystal":
            groups = deepcopy(context.get("crystal_groups") or [])
            previous_uids = context.get("crystal_element_uids")
            if context.get("crystal_spec") and not previous_uids:
                previous_uids = [e["uid"] for e in old_optics["elements"] if any(
                    p["role"] == "crystal" and old_optics["elements"][p["element_index"]]["uid"] == e["uid"] for p in old_parts)]
            if context.get("crystal_spec") and previous_uids and not any(set(g["element_uids"]) == set(previous_uids) for g in groups):
                groups.append({"element_uids":previous_uids,"inventory_id":context.get("crystal_inventory_id"),
                               "name":context.get("crystal_name"),"spec":context["crystal_spec"],"orientation":context.get("crystal_orientation")})
            for group in groups:
                frozen = (context.get("components") or {}).get("crystal")
                if frozen and group["element_uids"] == previous_uids and frozen.get("inventory_id") == group.get("inventory_id"):
                    group["stock_snapshot"] = deepcopy(frozen)
            target_uids = {old_optics["elements"][i]["uid"] for i in targets}
            groups = [g for g in groups if not target_uids.intersection(g["element_uids"])]
            new_uids = [e["uid"] for e in replacements]
            groups.append({"element_uids":new_uids,"inventory_id":item.id,"name":item.name,
                           "spec":actual_spec,"orientation":request.orientation})
            context.update(crystal_inventory_id=item.id, crystal_spec=actual_spec, crystal_orientation=request.orientation,
                           crystal_name=item.name, crystal_sections=sections, crystal_element_uids=new_uids,
                           crystal_groups=groups, scope_warnings=warnings)
        elif item.category == "lens":
            index = targets[0]
            replacement = {"kind":"lens", "uid":installed_uid(0), "name":item.name[:100],
                           "aoi_deg":elements[index].get("aoi_deg", 0), "focal_mm":item.focal_length_mm, "length_mm":0.}
            elements[index] = replacement
            if not any(p["element_index"] == index for p in parts):
                parts.append({"element_index":index, "role":role, "source":"用户选择库存透镜", "ownership":"owned"})
            for part in parts:
                if part["element_index"] == index:
                    part.update(ownership="owned", inventory_id=item.id, source=f"用户明确选择库存#{item.id}：{item.name}",
                                locked_parameters=["kind","focal_mm"], unknowns=["当前为所给焦距的近轴薄透镜模型，实际主平面/像差/口径截光未求解。"])
            warnings = ["以库存焦距在原安装参考面复算薄透镜传播。实际主面、像差和焦距随波长的变化需镜片处方或实测数据。"]
        elif item.category == "mirror":
            index = targets[0]
            label = {"pump_input":"M1 输入镜", "laser_output":"M2 输出镜"}.get(role,"反射镜")
            replacement = {"kind":"mirror","uid":installed_uid(0), "name":f"{label}：{item.name}"[:100],
                           "aoi_deg":elements[index].get("aoi_deg", 0), "length_mm":0.,
                           "radius_mm":None if item.roc_is_flat else item.roc_mm}
            elements[index] = replacement
            if not any(p["element_index"] == index for p in parts):
                parts.append({"element_index":index, "role":role, "source":"用户选择库存反射镜", "ownership":"owned"})
            for part in parts:
                if part["element_index"] == index:
                    part.update(ownership="owned", inventory_id=item.id, source=f"用户明确选择库存#{item.id}：{item.name}",
                                locked_parameters=["kind", "radius_mm", "aoi_deg"],
                                unknowns=["镀膜用途和数值以元件库明确记录为准，未知值没有补全。"])
            warnings.append("近轴模型采用该实物的反射面曲率与原安装角。镀膜 T/R 资料随实物保存，未计算其对输出功率的影响。")
            if path_uid == "primary" and role == "pump_input" and optics["mode"] != "single_pass":
                wavelength = optics["wavelength_nm"]
                reflection = f"{wavelength:g} nm 的反射面" if wavelength is not None else "波长待定的反射面"
                warnings.append(f"支路透过输入镜时需另给基底、厚度和两面曲率。当前主光路按{reflection}建模。")
            context["scope_warnings"] = warnings
        new_uid = optics["elements"][targets[0]]["uid"]
        old_ids = {old_optics["elements"][i]["uid"] for i in targets}
        project["assemblies"] = [g for g in project["assemblies"] if g["path_uid"] != path_uid or not old_ids.intersection(g["element_uids"])]
        installed = [e["uid"] for e in replacements] if segmented else [new_uid]
        if segmented:
            project["assemblies"].append({"uid": installed_uid("assembly"), "name": item.name[:120], "path_uid": path_uid, "element_uids": installed})
        context_key = role if role in {"crystal","pump_input","laser_output"} else f"element:{new_uid}"
        old_context_keys = {f"element:{uid}" for uid in old_ids}
        for name in ("components","warnings_by_role","component_selection"):
            context[name] = {k:v for k,v in (context.get(name) or {}).items() if k not in old_context_keys}
        context["wavelength_nm"] = optics["wavelength_nm"]
        context["components"] = {**context.get("components",{}), context_key:{
            "inventory_id":item.id,"name":item.name,"category":item.category,"raw_spec":item.raw_spec,"cut_axis":item.cut_axis,
            "roc_mm":item.roc_mm,"roc_is_flat":item.roc_is_flat,"focal_length_mm":item.focal_length_mm,
            "coatings":[{key:getattr(c,key) for key in ("surface","wl_min_nm","wl_max_nm","function","value_type","comparator","value_pct","raw_fragment")} for c in item.coatings]}}
        if mount.category == "medium":
            frozen = context["components"][context_key]
            frozen.update({field:getattr(item, field) for field in ("thickness_mm", "material", "diameter_mm", "dimensions",
                          "cut_angle_theta_deg", "cut_angle_phi_deg", "source_file", "crystal_spec")})
            frozen["medium_projection"] = {"spec":actual_spec, "element_uids":installed, "orientation":request.orientation,
                                           "sections":sections, "wavelength_nm":optics["wavelength_nm"]}
        warnings.append(stock_at_wavelength(mount, item))
        reference_notes = reconcile_stock_references(project, path_uid, [old_optics["elements"][i] for i in targets],
                                                     [e for e in optics["elements"] if e["uid"] in installed])
        warnings_by_role = deepcopy(context.get("warnings_by_role") or {})
        warnings_by_role[context_key] = warnings
        context["warnings_by_role"] = warnings_by_role
        warnings = list(dict.fromkeys(w for rows in warnings_by_role.values() for w in rows))
        context["scope_warnings"] = warnings
        context["component_selection"] = {**context.get("component_selection", {}), context_key:item.id}
        spacing = mount_spacing(project, mount, optics, old_optics, old_parts, installed, request.spacing_policy)
        spacing["affected_planes"] = station_changes(original, project, path_uid)
        reference_notes.extend(p["note"] for p in spacing["affected_planes"])
        after_spacing = sum(e["length_mm"] for e in optics["elements"])
        mirror_pair = (path_uid == "primary" and optics["mode"] == "standing_wave"
                       and optics["elements"][0]["kind"] == optics["elements"][-1]["kind"] == "mirror"
                       and sum(e["kind"] == "mirror" for e in optics["elements"]) == 2)
        replacement_info = {"target_path_uid": path_uid, "target_role":role, "target_element_uid":new_uid, "reference_notes": reference_notes,
            "previous_target_element_uid":request.target_element_uid, "inventory_id":item.id, "inventory_name":item.name,
            "previous_revision_id":previous["revision_id"], "old_component_length_mm":old_total,
            "new_component_length_mm":sum(e["length_mm"] for e in optics["elements"] if e["uid"] in installed),
            "old_path_length_mm":before_spacing, "new_path_length_mm":after_spacing,
            **({"old_mirror_spacing_mm":before_spacing, "new_mirror_spacing_mm":after_spacing} if mirror_pair else {}),
            "orientation":request.orientation, **spacing}
        context["last_replacement"] = replacement_info
        project_context["last_replacement"] = replacement_info
        # Check every unrelated hardware value and binding before scoped unlock.
        new_by_uid = {e["uid"]:e for e in optics["elements"]}
        old_target_uids = {old_optics["elements"][i]["uid"] for i in targets}
        gap = spacing["compensating_gap"]
        for element in old_optics["elements"]:
            expected = element
            if gap and element["uid"] == gap["uid"]:
                expected = {**element, "length_mm": gap["new_length_mm"]}
            if element["uid"] not in old_target_uids and new_by_uid.get(element["uid"]) != expected:
                raise ValueError("替换意外影响其他元件，未保存工程。")
        for part in old_parts:
            old_element = old_optics["elements"][part["element_index"]]
            if old_element["uid"] in old_target_uids:
                continue
            updated = next((p for p in bindings["parts"] if optics["elements"][p["element_index"]]["uid"] == old_element["uid"]), None)
            if updated is None or {k:v for k,v in updated.items() if k != "element_index"} != {k:v for k,v in part.items() if k != "element_index"}:
                raise ValueError("替换意外影响其他元件绑定或锁定，未保存工程。")
        label = "主光路" if path_uid == "primary" else optics["name"]
        note = installation_note(label, replacement_info)
        project["notes"] = "\n".join(filter(None, (project.get("notes"), request.change_note, note)))
        try:
            updated = SaveProject(base_revision_id=request.base_revision_id, project=project, change_note=request.change_note)
            if preview:
                data = evaluate_project(db, updated.project, previous)
                return {**data, "replacement": replacement_info, "scope_warnings": list(dict.fromkeys([
                        *data["analysis"].get("scope_warnings", []), *reference_notes])), "preview_token": token}
            if study_id is not None:
                from .study_inputs import SaveVariant, save_variant
                saved = save_variant(db, case_id, SaveVariant(project=updated.project, source_revision_id=study_id,
                                     base_revision_id=working.get("base_revision_id"), variant_name=working.get("variant_name")),
                                     note=request.change_note)
                return {**saved, "replacement":replacement_info, "scope_warnings": list(dict.fromkeys([
                        *saved["analysis"].get("scope_warnings", []), *reference_notes]))}
            saved = save_project(db, case_id, updated, allow_unlock=True, commit=False)
            context = deepcopy(saved["project"]["component_context"])
            warnings = saved["analysis"].get("scope_warnings", [])
            if path_uid == "primary" and role in {"crystal","pump_input","laser_output"}:
                _refresh_selected_facts(case, item, role, context)
            case.parameters = {**case.parameters, "current_optical_project_revision_id":saved["revision_id"],
                               "current_optical_system":deepcopy(saved["project"]["system"]),
                               "current_optical_scope_warnings":warnings, **({"L_mm": after_spacing} if mirror_pair else {})}
            if active_task_id is None:
                params = case.parameters or {}
                sid = params.get("workbench_session_id") or params.get("design_session_id")
                session = db.get(AgentChatSession, sid) if sid else None
                if session is None or session.case_id != case_id:
                    session = AgentChatSession(case_id=case_id,title=f"实验任务 · {case.title}"[:255])
                    db.add(session)
                    db.flush()
                    case.parameters = {**case.parameters,"workbench_session_id":session.id}
                db.add(AgentChatMessage(session_id=session.id, role="user",
                    content=f"我在光学工程中将{role}替换为库存#{item.id}「{item.name}」。{request.change_note}",
                    metadata_json={"source":"component_picker", "project_revision_id":saved["revision_id"], "replacement":replacement_info}))
            db.commit()
        except Exception:
            db.rollback()
            raise
        return {**saved, "replacement":replacement_info, "scope_warnings":list(dict.fromkeys([*warnings, *reference_notes]))}


def project_from_design(design, candidate, previous=None):
    """Preserve M1/M2 roles and physical crystal segments when importing a design."""
    v = design["resolved_inputs"]
    crystal = candidate["crystal"]
    front, total, n = crystal["position_mm"], crystal["thickness_mm"], crystal["n"]
    material = v.get("gain_material", "增益晶体")
    cap = "YVO4" if "yvo4" in material.lower() else "无掺杂"
    crystal_owned = v.get("crystal_inventory_id") is not None
    elements = [{"name": "M1 输入镜", "kind": "mirror", "radius_mm": candidate.get("R1_mm")},
                air_gap("M1 → 晶体前端面", front),
                {"name": f"{cap} 前端帽", "kind": "space", "medium_role": "undoped", "length_mm": v.get("front_cap_mm", 0), "n": n},
                {"name": f"{material} 掺杂段", "kind": "space", "medium_role": "gain", "length_mm": v["active_length_mm"], "n": n},
                {"name": f"{cap} 后端帽", "kind": "space", "medium_role": "undoped", "length_mm": v.get("rear_cap_mm", 0), "n": n},
                air_gap("晶体后端面 → M2", candidate["length_mm"]-front-total),
                {"name": "M2 输出镜", "kind": "mirror", "radius_mm": candidate["R2_mm"]}]
    parts = []
    for idx, prefix, role in ((0, "input", "pump_input"), (6, "output", "laser_output")):
        inventory_id = v.get(f"{'M1' if idx == 0 else 'M2'}_inventory_id")
        owned = v.get(f"{prefix}_mirror_owned") or inventory_id is not None
        part = {"element_index": idx, "role": role, "ownership": "owned" if owned else "proposed",
                "source": "用户确认已有腔镜" if owned else "设计候选", "locked_parameters": ["kind", "radius_mm", "aoi_deg"] if owned else [],
                "unknowns": ["镀膜具体 R/T、基底和口径待核实"]}
        if prefix == "input" and candidate.get("R1_mm"):
            part["unknowns"].append(f"泵浦透射需要 M1 两面曲率、厚度和折射率。此工程采用腔内 {v['laser_wavelength_nm']:g} nm 模型。")
        if inventory_id is not None:
            part["inventory_id"] = int(inventory_id)
        parts.append(part)
    for idx in (2, 3, 4):
        parts.append({"element_index": idx, "role": "crystal", "ownership": "owned" if crystal_owned else "proposed",
                      "source": "已绑定库存晶体，折射率按所列模型" if crystal_owned else "尺寸取自设计记录，实物归属尚待确认",
                      "inventory_id": int(v["crystal_inventory_id"]) if v.get("crystal_inventory_id") else None,
                      "locked_parameters": ["kind", "length_mm"] if crystal_owned else [], "unknowns": []})
    if previous:
        old = previous["project"]
        for part in parts:
            matches = [p for p in old["parts"] if p["role"] == part["role"] and (part["role"] != "crystal" or old["system"]["elements"][p["element_index"]]["name"] == elements[part["element_index"]]["name"])]
            if len(matches) == 1:
                elements[part["element_index"]]["uid"] = old["system"]["elements"][matches[0]["element_index"]]["uid"]
    for element in elements:
        element.setdefault("uid", str(uuid4()))
    ids = [elements[p["element_index"]]["uid"] for p in parts if p["role"] == "crystal"]
    prior_group = next((g for g in (previous or {}).get("project", {}).get("assemblies", [])
                        if g["path_uid"] == "primary" and g["element_uids"] == ids), None)
    assembly = prior_group or {"uid": str(uuid4()), "name": f"{material} 晶体", "element_uids": ids}
    return OpticalProject(title=f"{v['laser_wavelength_nm']:g} nm 腔内光学工程 · M1/M2 实物约束", system={"mode": "standing_wave", "wavelength_nm": v["laser_wavelength_nm"], "elements": elements},
                          assemblies=[assembly, *(g for g in (previous or {}).get("project", {}).get("assemblies", []) if g["path_uid"] != "primary")],
                          parts=parts, assumptions=["冷腔、近轴正入射；不计算增益饱和或输出功率。", "晶体折射率按当前偏振模型；未实测热透镜。"],
                          single_pass_power=previous["project"].get("single_pass_power") if previous else None,
                          incident_paths=previous["project"].get("incident_paths", []) if previous else [],
                          observation_planes=previous["project"].get("observation_planes", []) if previous else [],
                          notes="z=0 为 M1 腔内反射面，沿 M1→M2 展开，距离单位 mm。腔内模型不包含输入镜的泵浦透射，也不预测输出镜背面的出射传播。")

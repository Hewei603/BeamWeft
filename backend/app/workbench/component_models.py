"""Reusable optical assemblies, captured and placed through the same library by UI and Agent."""
from copy import deepcopy
from hashlib import sha256
from math import isclose
from uuid import uuid4

from pydantic import Field, model_validator
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..models import ComponentModelRevision
from ..physics.materials import MaterialError, load_materials, resolve_material
from .optics import StrictInput, element_catalog, material_interval
from .assemblies import membership
from .component_references import binding_descriptions, capture_bindings, capture_planes, place_references
from .network import Identifier, NetworkComponent
from .projects import OpticalProject, SaveProject, normalize_project, read_project
from .study_inputs import save_working_project


class LibraryQuery(StrictInput):
    model_revision_id: int | None = Field(default=None, gt=0)
    before_id: int | None = Field(default=None, gt=0)
    limit: int = Field(default=50, ge=1, le=100)
    include_builtin: bool = Field(default=False, description="Include dispersion models with wavelength ranges and polarization/axis basis, installed device models, input schemas and editable presets.")


class CaptureModel(StrictInput):
    project_revision_id: int = Field(gt=0)
    path_uid: str = Field(default="primary", min_length=1, max_length=100)
    first_uid: str | None = Field(default=None, min_length=1, max_length=100)
    last_uid: str | None = Field(default=None, min_length=1, max_length=100)
    network_component_uids: list[Identifier] | None = Field(default=None, min_length=1, max_length=128,
        description="Select network components, either alone or together with an ordered interval. Connections with both ends in the selection travel together.")
    reference_plane_uids: list[Identifier] | None = Field(default=None, max_length=48,
        description="Planes to register on selected physical elements. Explicitly selected bench-fixed stations become element-relative in the copy. Mixed selections default to attached planes only.")
    name: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=1200, description="Portable parameter provenance and applicability to show in recipient cases.")
    include_power: bool = Field(default=False, description="Also copy matching stage laws and their source text, never input power or measurements.")
    previous_model_revision_id: int | None = Field(default=None, gt=0, description="Append to this family only if this is still its latest version.")

    @model_validator(mode="after")
    def selection(self):
        if (self.first_uid is None) != (self.last_uid is None):
            raise ValueError("请选择顺序光路的起止元件。")
        if self.first_uid is None and (not self.network_component_uids or self.include_power
                                       or self.path_uid != "primary" or self.reference_plane_uids):
            raise ValueError("请选择顺序光路的起止元件，或单独选取端口网络中的元件。")
        for ids in (self.network_component_uids, self.reference_plane_uids):
            if ids and len(ids) != len(set(ids)):
                raise ValueError("同一元件或观察面只能选取一次。")
        return self


class Placement(StrictInput):
    model_revision_id: int = Field(gt=0)
    path_uid: str = Field(default="primary", min_length=1, max_length=100)
    before_uid: str | None = Field(default=None, min_length=1, max_length=100, description="Insert immediately before this element. Omit to append, including to an empty incident arm.")
    geometry_only: bool = Field(default=False, description="Explicitly use only geometry when the preset contains single-pass loss laws.")


class PlaceModel(Placement):
    project_revision_id: int = Field(gt=0)


class PlacementPreview(Placement):
    project_revision_id: int | None = Field(default=None, gt=0)
    draft: OpticalProject | None = Field(default=None, description="Current unsaved engineering input. Omit the revision only before this case has a saved project.")

    @model_validator(mode="after")
    def has_project(self):
        if self.project_revision_id is None and self.draft is None:
            raise ValueError("请选择工程版本或提供当前编辑的工程。")
        return self


class LibraryConflict(ValueError):
    pass


def _owned(db, principal):
    if principal.user_id is None and get_settings().require_auth:
        raise ValueError("请先选择已登录的用户账户，再打开个人模型库。")
    owner = "local" if principal.user_id is None else f"user:{principal.user_id}"
    return db.query(ComponentModelRevision).filter_by(owner_key=owner), owner


def _model(query, revision_id):
    row = query.filter_by(id=revision_id).one_or_none()
    if not row:
        raise ValueError("个人模型库中找不到此版本。请重新选择。")
    return row


def _boundary_ports(network):
    linked = {(t["component"], t["port"]) for link in network["links"] for t in (link["a"], link["b"])}
    return [{"component":c["uid"], "name":c["name"], "port":port}
            for c in network["components"] for port in NetworkComponent.model_validate(c).ports()
            if (c["uid"], port) not in linked]


def _portable_medium(element):
    if element["kind"] != "space":
        return False
    ref = element.get("index_reference")
    if not ref:
        return material_interval(element, {})
    if not ref.get("model"):
        return True
    try:
        return load_materials()[resolve_material(ref["model"])].get("index_basis") == "scalar"
    except MaterialError:
        return False


def _cross_wavelength_geometry(snapshot):
    elements = snapshot.get("elements")
    return bool(elements) and "network" not in snapshot and all(_portable_medium(el) for el in elements)


def _entry(row, *, detail=False):
    snapshot = row.snapshot
    result = {"model_revision_id": row.id, "family": row.family, "version": row.version,
              "name": snapshot["name"], "note": snapshot["note"], "wavelength_nm": snapshot["wavelength_nm"],
              "representation": snapshot.get("representation", "ordered"), "element_names": []}
    if "elements" in snapshot:
        result.update(element_names=[el["name"] for el in snapshot["elements"]],
                      cross_wavelength_geometry=_cross_wavelength_geometry(snapshot),
                      length_mm=sum(el.get("length_mm", 0) for el in snapshot["elements"]),
                      power_stages=len(snapshot["laws"]))
    if "reference_planes" in snapshot:
        result["reference_planes"] = [p["name"] for p in snapshot["reference_planes"]]
    if "network" in snapshot:
        network = snapshot["network"]
        result["element_names"].extend(c["name"] for c in network["components"])
        result.update(internal_links=len(network["links"]),
                      boundary_ports=snapshot["boundary_ports"] if "boundary_ports" in snapshot else _boundary_ports(network))
    if "bindings" in snapshot:
        result.update(internal_feeds=len(snapshot["bindings"]), network_wavelength_nm=snapshot["network_wavelength_nm"],
                      input_references=binding_descriptions(snapshot))
    if detail:
        result["snapshot"] = snapshot
    return result


def library(db, principal, args):
    query, _ = _owned(db, principal)
    if args.model_revision_id is not None:
        return _entry(_model(query, args.model_revision_id), detail=True)
    if args.before_id:
        query = query.filter(ComponentModelRevision.id < args.before_id)
    rows = query.order_by(ComponentModelRevision.id.desc()).limit(args.limit + 1).all()
    return {"models": [_entry(row) for row in rows[:args.limit]],
            "next_before_id": rows[args.limit - 1].id if len(rows) > args.limit else None,
            **({"element_catalog": element_catalog()} if args.include_builtin else {})}


def _path(project, uid):
    if uid == "primary":
        return project["system"], project, "single_pass_power", "host"
    arm = next((p for p in project.get("incident_paths", []) if p["uid"] == uid), None)
    if arm is None:
        raise ValueError("工程中找不到所选光路，请重新选择。")
    return arm, arm, "power_budget", "arm"


def _index(elements, uid):
    for index, element in enumerate(elements):
        if element["uid"] == uid:
            return index
    raise ValueError("所选元件已不在这条光路中，请重新选择。")


def _same_wave(first, second):
    return first is not None and second is not None and isclose(first, second, rel_tol=1e-12)


def _ordered_snapshot(project, args):
    path, parent, budget_key, scope = _path(project, args.path_uid)
    elements = path["elements"]
    first, last = _index(elements, args.first_uid), _index(elements, args.last_uid)
    if first > last:
        raise ValueError("终点应在起点或其下游。")
    wavelength = path.get("wavelength_nm")
    if wavelength is None:
        raise ValueError("请先在所选光路中填写建模波长，再保存模型。")
    selected = elements[first:last + 1]
    slots = {el["uid"]: i for i, el in enumerate(selected)}
    laws = []
    budget = parent.get(budget_key)
    if args.include_power and scope == "host" and path["mode"] != "single_pass":
        raise ValueError("主光路是谐振腔，此处不能保存单程传输预算。请仅保存近轴参数或选择入射支路。")
    if args.include_power and budget:
        for stage in budget["stages"]:
            if stage["scope"] == scope and stage["element_uid"] in slots:
                if not _same_wave(budget.get("wavelength_nm"), wavelength):
                    raise ValueError("传输参数波长与光路不一致。请更正预算或只保存近轴模型。")
                laws.append({"index": slots[stage["element_uid"]], "law": deepcopy(stage["law"])})
    if args.include_power and not laws:
        raise ValueError("所选范围没有可保存的逐件传输参数。请取消勾选或先填写参数。")
    assemblies = [{"name": group["name"], "basis": group.get("basis", "declared"), "slots": [slots[uid] for uid in group["element_uids"]]}
                  for group in project["assemblies"] if group["path_uid"] == args.path_uid
                  and all(uid in slots for uid in group["element_uids"])]
    return selected, {"wavelength_nm": wavelength, "laws": laws, "assemblies": assemblies, "source_path_uid": args.path_uid,
                "elements": [{k: deepcopy(v) for k, v in el.items() if k not in {"uid", "component_origin"}}
                             for el in selected]}


def _network_snapshot(project, args):
    network = project.get("network") or {}
    chosen = set(args.network_component_uids)
    selected = [c for c in network.get("components", []) if c["uid"] in chosen]
    if len(selected) != len(chosen):
        raise ValueError("所选端口元件已不在这个工程版本中，请重新选择。")
    slots = {c["uid"]:str(i+1) for i,c in enumerate(selected)}
    components = [{**{k:deepcopy(v) for k,v in c.items() if k not in {"component_origin","diagram_xy"}},
                   "uid":slots[c["uid"]]} for c in selected]
    links = []
    for link in network.get("links", []):
        if all(link[end]["component"] in slots for end in ("a","b")):
            copied = deepcopy(link)
            for end in ("a","b"):
                copied[end]["component"] = slots[link[end]["component"]]
            links.append(copied)
    assembly = {"components":components, "links":links}
    return selected, {"representation":"port_network", "network":assembly, "boundary_ports":_boundary_ports(assembly),
                      "wavelength_nm":network.get("wavelength_nm") or project["system"]["wavelength_nm"]}


def _capture_snapshot(project, args):
    elements, nodes, snapshot = [], [], {}
    if args.first_uid is not None:
        elements, snapshot = _ordered_snapshot(project, args)
        if args.reference_plane_uids is not None or args.network_component_uids:
            snapshot["reference_planes"] = capture_planes(project, args.path_uid, elements, args.reference_plane_uids)
    if args.network_component_uids:
        nodes, network = _network_snapshot(project, args)
        if elements:
            bindings = capture_bindings(project, args.path_uid, elements, nodes, snapshot["reference_planes"])
            occupied = {(network["network"]["components"][b["component"]]["uid"], b["port"]) for b in bindings}
            snapshot.update(network=network["network"], network_wavelength_nm=network["wavelength_nm"],
                            representation="assembly", bindings=bindings, boundary_ports=[
                                p for p in network["boundary_ports"] if (p["component"], p["port"]) not in occupied])
        else:
            snapshot = network
    ordered_wave = snapshot["wavelength_nm"]
    for selected, wave in ((elements, ordered_wave), (nodes, snapshot.get("network_wavelength_nm", ordered_wave))):
        if any(el.get("component_origin") and not _same_wave(el["component_origin"]["wavelength_nm"], wave)
               and not (el.get("index_reference") and _portable_medium(el))
               for el in selected) and not args.note.strip():
            raise ValueError("所选元件的模型来源波长不同。请在适用条件中注明新波长下的参数依据或假设，再建立版本。")
    snapshot.update(name=args.name.strip(), note=args.note.strip(), source_project_revision_id=args.project_revision_id,
                    source_element_uids=[el["uid"] for el in elements + nodes])
    return snapshot, nodes


def _capture_receipt(case_id, args, snapshot, nodes):
    # Retain receipt keys for both kinds of existing single-domain captures.
    exclude = set()
    updates = {}
    if nodes:
        updates["network_component_uids"] = [c["uid"] for c in nodes]
    else:
        exclude.add("network_component_uids")
    if snapshot.get("reference_planes") or snapshot.get("representation") == "assembly":
        updates["reference_plane_uids"] = [p["source_plane"]["uid"] for p in snapshot.get("reference_planes", [])]
    else:
        exclude.add("reference_plane_uids")
    request = args.model_copy(update=updates).model_dump_json(exclude=exclude)
    return sha256(f"{case_id}:{request}".encode()).hexdigest()


def capture_model(db, principal, case_id, args):
    query, owner = _owned(db, principal)
    project = read_project(db, case_id, args.project_revision_id)["project"]
    if not args.name.strip():
        raise ValueError("请给模型命名。")
    snapshot, nodes = _capture_snapshot(project, args)
    receipt = _capture_receipt(case_id, args, snapshot, nodes)
    existing = query.filter_by(receipt=receipt).one_or_none()
    if existing:
        return _entry(existing)
    family, version = str(uuid4()), 1
    if args.previous_model_revision_id:
        prev = _model(query, args.previous_model_revision_id)
        if query.filter_by(family=prev.family).order_by(ComponentModelRevision.version.desc()).first().id != prev.id:
            raise LibraryConflict("模型库已有新版本。请刷新并核对最新版本后再保存。")
        family, version = prev.family, prev.version + 1
    row = ComponentModelRevision(owner_key=owner, receipt=receipt, family=family, version=version, snapshot=snapshot)
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        existing = query.filter_by(receipt=receipt).one_or_none()
        if existing:
            return _entry(existing)
        raise LibraryConflict("模型库已由另一操作更新，请刷新后重试。") from exc
    return _entry(row)


def _check_boundary(project, elements, index, path_uid):
    if path_uid == "primary" and project["system"]["mode"] == "standing_wave" and index in (0, len(elements)):
        raise ValueError("请在驻波腔的两面端镜之间选择插入位置。")
    if not 0 < index < len(elements):
        return
    left, right = elements[index - 1]["uid"], elements[index]["uid"]
    groups = membership(project["assemblies"], path_uid)
    if left in groups and groups[left] == groups.get(right):
        raise ValueError(f"这个位置位于「{groups[left]['name']}」内部，请选择该实物的外侧。")
    if path_uid != "primary":
        return
    for arm in project.get("incident_paths", []):
        if arm.get("through_uid"):
            continue
        route = [arm["target_uid"], *(seg["target_uid"] for seg in arm.get("downstream", []))]
        if (left, right) in zip(route, route[1:]):
            raise ValueError("这里是入射支路已经绑定的连续介质。请选择整组介质之前或之后。")


def _instantiate(items, row, instance_uid, wavelength, offset=0):
    model = row.snapshot
    result = []
    for ordinal, item in enumerate(items, offset + 1):
        origin = {"model_revision_id":row.id, "version":row.version, "name":model["name"],
                  "wavelength_nm":wavelength, "note":model["note"], "instance_uid":instance_uid,
                  "ordinal":ordinal}
        result.append({**deepcopy(item), "uid":str(uuid4()), "component_origin":origin})
    return result


def _place_ordered(project, args, row, instance_uid):
    path, parent, budget_key, scope = _path(project, args.path_uid)
    model = row.snapshot
    if not _same_wave(path.get("wavelength_nm"), model["wavelength_nm"]):
        if not _cross_wavelength_geometry(model):
            raise ValueError(f"模型适用波长为 {model['wavelength_nm']:g} nm。请选用同波长模型，或依据新波长的数据另建版本。")
        if model["laws"] and not args.geometry_only:
            raise ValueError(f"逐件传输参数标定于 {model['wavelength_nm']:g} nm。请选择仅插入近轴参数，或提供目标波长的传输标定。")
    elements = path["elements"]
    index = _index(elements, args.before_uid) if args.before_uid else len(elements)
    _check_boundary(project, elements, index, args.path_uid)
    for part in parent.get("parts", []):
        if part["element_index"] >= index:
            part["element_index"] += len(model["elements"])
    if len(elements) + len(model["elements"]) > 80:
        raise ValueError("插入后将超过单条光路的 80 个模型元素上限。")
    inserted = _instantiate(model["elements"], row, instance_uid, model["wavelength_nm"])
    for element in inserted:
        if not element.get("index_reference") and _portable_medium(element):
            element["index_reference"] = {"model": None, "wavelength_nm": model["wavelength_nm"]}
    elements[index:index] = inserted
    for group in model.get("assemblies", []):
        project["assemblies"].append({"uid": str(uuid4()), "name": group["name"], "path_uid": args.path_uid, "basis": group.get("basis", "declared"),
                                     "element_uids": [inserted[i]["uid"] for i in group["slots"]]})
    if model["laws"] and not args.geometry_only:
        if args.path_uid == "primary" and path["mode"] != "single_pass":
            raise ValueError("腔内循环功率需要其他模型。勾选“仅插入近轴参数”即可在腔内使用这组元件。")
        budget = parent.get(budget_key)
        if budget is None:
            budget = parent[budget_key] = {"wavelength_nm": model["wavelength_nm"], "stages": []}
        if not _same_wave(budget.get("wavelength_nm"), model["wavelength_nm"]):
            raise ValueError("当前功率预算波长不同。请先核对预算，或仅插入近轴参数。")
        for stage in model["laws"]:
            budget["stages"].append({"scope": scope, "element_uid": inserted[stage["index"]]["uid"],
                                     "law": deepcopy(stage["law"])})
    return inserted


def _place_network(project, args, row, instance_uid):
    if "elements" not in row.snapshot and (args.path_uid != "primary" or args.before_uid is not None or args.geometry_only):
        raise ValueError("端口模型放入工程的端口网络，其外部端口由用户接线。请移除顺序光路的插入位置或传输预算选项。")
    model = row.snapshot
    wavelength = model.get("network_wavelength_nm", model["wavelength_nm"])
    network = project.get("network")
    if network is None:
        network = project["network"] = {"components":[], "links":[], "drives":[], "wavelength_nm":wavelength}
    if not _same_wave(network.get("wavelength_nm") or project["system"]["wavelength_nm"], wavelength):
        raise ValueError(f"模型适用波长为 {wavelength:g} nm。请核对工程端口网络的波长。")
    source = model["network"]
    inserted = _instantiate(source["components"], row, instance_uid, wavelength, len(model.get("elements", [])))
    identities = {old["uid"]: new["uid"] for old,new in zip(source["components"], inserted)}
    network["components"].extend(inserted)
    for link in source["links"]:
        copy = deepcopy(link)
        for end in ("a","b"):
            copy[end]["component"] = identities[link[end]["component"]]
        network["links"].append(copy)
    return inserted


def place_model(db, principal, case_id, args, *, preview=False, working=None):
    query, _ = _owned(db, principal)
    row = _model(query, args.model_revision_id)
    temporary_id = working["revision_id"] if working and working["kind"] == "draft" else None
    current = read_project(db, case_id, temporary_id)
    if (current["revision_id"] if current else None) != args.project_revision_id:
        raise LibraryConflict("工程已有新版本，请读取最新工程后重新选择插入位置。")
    draft = args.draft if preview and isinstance(args, PlacementPreview) else None
    project = normalize_project(draft, current).model_dump() if draft is not None else deepcopy(current["project"])
    instance_uid = str(uuid4())
    model = row.snapshot
    elements = _place_ordered(project, args, row, instance_uid) if "elements" in model else []
    nodes = _place_network(project, args, row, instance_uid) if "network" in model else []
    place_references(project, model, args.path_uid, elements, nodes, instance_uid)
    count = len(elements) + len(nodes)
    project = OpticalProject.model_validate(project)
    note = f"插入模型「{row.snapshot['name']}」v{row.version}（{count} 个元素）"
    if preview:
        return {"project": project.model_dump(), "change_note": note, "instance_uid": instance_uid}
    return save_working_project(db, case_id, SaveProject(project=project, base_revision_id=args.project_revision_id, change_note=note), working)

"""Identify a physical component on its owning path and evaluate replacement stock."""
import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import func, or_
from sqlalchemy.orm import selectinload

from ..inventory.crystals import CrystalSegment, CrystalSpec, crystal_geometry
from ..models import InventoryItem, InventoryLoan
from ..schemas import InventoryItemResponse
from .assemblies import object_keys, part_groups
from .optics import OpticalElement, material_interval


@dataclass
class Mount:
    path_uid: str
    optics: dict
    bindings: dict
    role: str
    indices: list
    occupied: Counter
    category: str

    @property
    def categories(self):
        if self.category == "medium":
            return ["window", "nonlinear_crystal", "crystal_other", "gain_crystal", "other"]
        return [self.category]


def medium_position(element, part):
    return element["length_mm"] > 0 and not element.get("clear_aperture") and material_interval(element, part)


def stock_medium_spec(item):
    """Build a propagation input; the original stock evidence remains independent."""
    if item.roc_mm is not None or item.focal_length_mm is not None:
        raise ValueError("库存标有有限曲率或光焦度，平行平面介质模型无法表达该面形。请使用带界面处方的元件。")
    if item.crystal_spec:
        return CrystalSpec.model_validate(item.crystal_spec)
    if item.category in {"gain_crystal", "other"}:
        raise ValueError("该实物尚未登记介质结构。请登记透射介质的各段，或确认其整件厚度及结构后再装配。")
    if item.thickness_mm is None:
        raise ValueError("缺少沿光束方向的厚度或分段长度，无法确定该实物的两个端面。")
    return CrystalSpec(overall_length_mm=item.thickness_mm,
                       segments=[CrystalSegment(material=item.material, length_mm=item.thickness_mm)])


def read_medium_projection(frozen, key):
    projection = frozen["medium_projection"]
    if not isinstance(projection, dict):
        raise ValueError("库存介质的投影记录不完整，请恢复完整版本或重新选用该实物。")
    uids = projection.get("element_uids")
    if (not isinstance(uids, list) or not 1 <= len(uids) <= 80 or not all(isinstance(uid, str) and uid.strip() for uid in uids)
            or len(set(uids)) != len(uids) or key != f"element:{uids[0]}" or not isinstance(frozen.get("inventory_id"), int)):
        raise ValueError("库存介质的实物标识或有序分段标识不完整。快照应以首段标识关联实物，请恢复完整版本或重新选用。")
    try:
        return CrystalSpec.model_validate(projection.get("spec"))
    except ValidationError as exc:
        raise ValueError("库存介质缺少有效的分段结构快照，请恢复完整版本或重新选用实物。") from exc


def restore_sample_index_basis(optics, context):
    """Old stock receipts recorded an out-of-band entered value beside its unavailable catalog curve."""
    if not isinstance(optics, dict) or not isinstance(context, dict) or not isinstance(context.get("components"), dict):
        return optics
    elements = optics.get("elements")
    if not isinstance(elements, list):
        return optics
    positions = {e["uid"]: i for i, e in enumerate(elements) if isinstance(e, dict) and isinstance(e.get("uid"), str)}
    recovered = None
    for item in context["components"].values():
        projection = item.get("medium_projection") if isinstance(item, dict) else None
        if not isinstance(projection, dict) or projection.get("wavelength_nm") is None:
            continue
        sections, uids = projection.get("sections"), projection.get("element_uids")
        if not isinstance(sections, list) or not isinstance(uids, list) or len(sections) != len(uids):
            continue
        for uid, section in zip(uids, sections):
            if not isinstance(uid, str) or uid not in positions or not isinstance(section, dict):
                continue
            element = elements[positions[uid]]
            ref = element.get("index_reference")
            if (isinstance(ref, dict) and "model_index" in section and section["model_index"] is None and section.get("project_index") is not None
                    and section.get("project_index") == element.get("n") and section.get("index_source")
                    and section.get("index_model") and section["index_model"] == ref.get("model")
                    and ref.get("wavelength_nm") == projection["wavelength_nm"]):
                if recovered is None:
                    recovered = list(elements)
                recovered[positions[uid]] = {**element, "index_reference": {**ref, "model": None}}
    return {**optics, "elements": recovered} if recovered is not None else optics


def validate_mounted_medium(project, optics, bindings, path_uid, frozen, spec):
    """Resolve the frozen prescription against this path, never against today's stock row."""
    projection = frozen["medium_projection"]
    uids = projection["element_uids"]
    members = {optics.elements[p.element_index].uid:(optics.elements[p.element_index], p) for p in bindings}
    assembly = next((g for g in project.assemblies if g.path_uid == path_uid and uids[0] in g.element_uids), None)
    if (assembly.element_uids if assembly else uids[:1]) != uids:
        raise ValueError("工程中的介质实物边界与库存快照不一致，请重新选用实物。")
    expected, sections, warnings = crystal_geometry(spec, optics.wavelength_nm, gain_mount=False)
    if len(uids) != len(expected):
        raise ValueError("介质分段数与库存快照不一致，请重新选用实物。")
    for uid, model, section in zip(uids, expected, sections, strict=True):
        if uid not in members or members[uid][1].inventory_id != frozen["inventory_id"]:
            raise ValueError("工程介质的分段绑定与库存实物不一致，请核对每一段的来源。")
        element, part = members[uid]
        reference = element.index_reference
        has_sample = (reference is not None and reference.model is None and model["n"] is None)
        if (element.kind != "space" or element.length_mm != model["length_mm"] or element.medium_role != model["medium_role"]
                or reference is None or (reference.model != section["index_model"] and not has_sample)
                or element.index_reference.wavelength_nm != optics.wavelength_nm):
            raise ValueError("工程的介质结构、材料分支或波长依据与所选库存快照不一致，请重新选用实物。")
        if model["n"] is not None and (element.n is None or not math.isclose(element.n, model["n"], abs_tol=1e-9)):
            raise ValueError("工程介质折射率与该波长处已选用的材料模型不一致，请核对材料或建立独立的假设模型。")
        section["project_index"] = element.n
        if model["n"] is None and element.n is not None:
            if optics.wavelength_nm is None:
                raise ValueError("请先登记此光路的波长，再填写该波长处的介质折射率。")
            section["index_source"] = f"工程填写（{optics.wavelength_nm:g} nm），材料身份以库存证据为准。"
            warning = f"第{section['section']}段采用工程在 {optics.wavelength_nm:g} nm 填写的 n={element.n:g}。"
            warnings = [note for note in warnings if not (note.startswith(f"第{section['section']}段：") and "折射率" in note)]
            section["unknowns"] = [note for note in section["unknowns"] if "折射率" not in note]
            warnings.append(warning)
        part.unknowns = list(section["unknowns"])
    projection.update(wavelength_nm=optics.wavelength_nm, sections=sections)
    warnings.append(coating_note(optics.wavelength_nm, "medium", frozen.get("coatings", [])))
    return warnings


def locate_mount(project, path_uid, element_uid, role=None):
    arm = next((p for p in project.get("incident_paths", []) if p["uid"] == path_uid), None)
    if path_uid != "primary" and arm is None:
        raise ValueError("所选支路已移除或属于其他版本，请重新选择工程中的元件。")
    optics, bindings = (arm, arm) if arm is not None else (project["system"], project)
    elements, parts = optics["elements"], bindings.get("parts", [])
    by_index = {p["element_index"]: p for p in parts}
    keys = object_keys(project, arm)
    if element_uid:
        index = next((i for i, element in enumerate(elements) if element["uid"] == element_uid), None)
        if index is None:
            raise ValueError("目标元件不属于所选光路及版本，请重新选择元件并核对是否使用了旧版本标识。")
        selected_role = by_index.get(index, {}).get("role") or "optical_element"
        if role is not None and role != selected_role:
            raise ValueError("目标角色与指定元件指向不同实物，请重新选择。")
        role, key = selected_role, keys[index]
    else:
        objects = {keys[i] for i, part in by_index.items() if part["role"] == role}
        if len(objects) != 1:
            raise ValueError("此角色对应多件独立实物或未登记，请通过 target_element_uid 选择具体元件。")
        key, = objects
    indices = [i for i, k in enumerate(keys) if k == key]
    group_roles = {by_index[i]["role"] for i in indices if by_index.get(i, {}).get("role")}
    if role == "crystal":
        if any(by_index.get(i, {}).get("role") != role or elements[i]["kind"] != "space" for i in indices):
            raise ValueError("该组合含其他类型的独立元件，不能整组替换为分段晶体。")
        category = "gain_crystal"
    elif len(indices) == 1 and elements[indices[0]]["kind"] in {"lens", "mirror"}:
        category = elements[indices[0]]["kind"]
        if role in {"pump_input", "laser_output"} and category != "mirror":
            raise ValueError("端镜角色与该元件的薄透镜模型不一致，请先核对工程。")
    elif (len(group_roles) <= 1 and not group_roles.intersection({"crystal", "pump_input", "laser_output"})
          and all(medium_position(elements[i], by_index.get(i, {})) for i in indices)):
        category = "medium"
        role = next(iter(group_roles), "medium")
    else:
        raise ValueError("此位置需要独立的薄透镜、反射镜、平行平面介质或已登记的晶体。空气间隔、光阑及包含其他元件的组合无法整体替换为该库存件。")
    occupied = Counter()
    for uid, path_elements, path_parts in part_groups(project):
        arm_data = None if uid == "primary" else {"uid": uid, "elements": path_elements}
        group_keys = object_keys(project, arm_data)
        objects = {(part["inventory_id"], group_keys[part["element_index"]]) for part in path_parts
                   if part.get("inventory_id") is not None and not (uid == path_uid and part["element_index"] in indices)}
        occupied.update(stock_id for stock_id, _ in objects)
    return Mount(path_uid, optics, bindings, role, indices, occupied, category)


def assess_stock(mount, item, loaned):
    occupied = mount.occupied[item.id]
    available = max(0., float(item.quantity or 0) - loaned)
    if item.effective_condition == "damaged":
        return "所选库存元件已标记损坏，请选择其他实物。"
    if available < occupied + 1:
        return f"库存#{item.id}可用数量不足：库存{float(item.quantity or 0):g}、借出{loaned:g}、可用{available:g}，当前工程另占{occupied:g}件；不能把同一件实物同时用于多个位置。"
    if item.category not in mount.categories:
        return "库存类别与所选元件的物理模型不相容。"
    if item.category == "lens" and not item.focal_length_mm:
        return "透镜焦距未知或为零，请提供该波长下的有效焦距。"
    if item.category == "mirror" and not item.roc_is_flat and not item.roc_mm:
        return "该镜片曲率未知，请填写非零曲率半径或明确选择平面镜。"
    if mount.category in {"medium", "gain_crystal"}:
        if mount.category == "gain_crystal" and not item.crystal_spec:
            return "请先在元件库登记晶体分段材料和段长。"
        try:
            spec = stock_medium_spec(item) if mount.category == "medium" else CrystalSpec.model_validate(item.crystal_spec)
            crystal_geometry(spec, mount.optics.get("wavelength_nm"), gain_mount=mount.category == "gain_crystal")
        except ValueError as exc:
            return str(exc)
    else:
        parameter = {"focal_mm": item.focal_length_mm} if item.category == "lens" else {
            "radius_mm": None if item.roc_is_flat else item.roc_mm}
        try:
            OpticalElement(kind=item.category, name=item.name[:100], **parameter)
        except ValidationError as exc:
            return exc.errors()[0]["msg"]
    return None


def stock_at_wavelength(mount, item):
    return coating_note(mount.optics.get("wavelength_nm"), mount.category, [
        {"wl_min_nm":c.wl_min_nm, "wl_max_nm":c.wl_max_nm, "function":c.function} for c in item.coatings])


def coating_note(wl, category, coatings):
    if wl is None:
        return "该光路波长待补，尚未核对镀膜适用范围。"
    bands = [c for c in coatings if c["wl_min_nm"] <= wl <= c["wl_max_nm"]]
    if not bands:
        return f"没有覆盖 {wl:g} nm 的镀膜记录，几何传播不会据此推断实际反射率或透过率。"
    functions = {c["function"] for c in bands}
    note = f"{wl:g} nm 落在 {len(bands)} 条镀膜记录中，标注功能为" + "、".join(sorted(functions)) + "。"
    operation = {"HR", "PR"} if category == "mirror" else {"AR", "HT"}
    if not functions.intersection(operation):
        note += "请核实工作面的实测反射率或透过率能否满足设计目标。"
    return note


def stock_candidates(db, project, path_uid, element_uid, role, search, skip, limit):
    mount = locate_mount(project, path_uid, element_uid, role)
    query = db.query(InventoryItem).filter(InventoryItem.category.in_(mount.categories))
    if search:
        term = search.strip()
        lookup = "%" + term.replace("\\","\\\\").replace("%","\\%").replace("_","\\_") + "%"
        conditions = [field.ilike(lookup, escape="\\") for field in (
            InventoryItem.name, InventoryItem.raw_spec, InventoryItem.location, InventoryItem.material, InventoryItem.vendor)]
        stock_number = term.removeprefix("#")
        if stock_number.isascii() and stock_number.isdigit() and len(stock_number) <= 18:
            conditions.append(InventoryItem.id == int(stock_number))
        query = query.filter(or_(*conditions))
    total = query.count()
    items = query.options(selectinload(InventoryItem.coatings)).order_by(InventoryItem.id).offset(skip).limit(limit).all()
    borrowed = (dict(db.query(InventoryLoan.item_id, func.sum(InventoryLoan.quantity)).filter(
        InventoryLoan.item_id.in_([item.id for item in items]), InventoryLoan.returned_at.is_(None)).group_by(InventoryLoan.item_id).all())
        if items else {})
    rows = []
    for item in items:
        loaned = float(borrowed.get(item.id, 0))
        reason = assess_stock(mount, item, loaned)
        rows.append({**InventoryItemResponse.model_validate(item).model_dump(mode="json"), "on_loan_qty": loaned,
                     "available_qty": max(0., float(item.quantity or 0)-loaned), "occupied_quantity": mount.occupied[item.id],
                     "eligible": reason is None, "reason": reason, "wavelength_note": stock_at_wavelength(mount, item)})
    return {"category": mount.category, "categories": mount.categories, "default_category": mount.categories[0],
            "target_path_uid": path_uid, "target_element_uid": element_uid,
            "wavelength_nm": mount.optics.get("wavelength_nm"), "items": rows, "total": total}


def stock_token(request, item, loaned, mount):
    catalog = InventoryItemResponse.model_validate(item).model_dump(mode="json", exclude={"on_loan_qty", "available_qty"})
    catalog["coatings"].sort(key=lambda band: band["id"])
    data = [request.base_revision_id, request.target_path_uid, mount.indices, request.orientation, loaned, mount.occupied[item.id],
            catalog]
    if request.spacing_policy != "keep_gaps":
        data.append(request.spacing_policy)
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def replacement_optical_results(analysis):
    output = {key: analysis[key] for key in ("status", "mode", "wavelength_nm", "stable_both_planes", "input_issues", "model_warnings")
              if key in analysis}
    output["axes"] = {axis: {key: plane[key] for key in ("stable", "m", "output", "message") if key in plane}
                      for axis, plane in analysis.get("planes", {}).items()}
    output["observation_planes"] = []
    for row in (analysis.get("sections") or {}).get("planes", []):
        station = {key: row[key] for key in ("uid", "name", "z_mm", "message") if key in row}
        station["beams"] = [{key: beam[key] for key in ("uid", "name", "wavelength_nm", "radii_mm", "width_basis", "notes")
                             if key in beam} for beam in row.get("beams", [])]
        output["observation_planes"].append(station)
    return output

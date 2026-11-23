"""Physical objects on ordered paths. Membership is independent of optical primitive and stock number."""
from itertools import groupby
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field

from ..study_api import StrictInput


class Part(StrictInput):
    element_index: int = Field(ge=0, description="Index within this ordered path.")
    role: str = ""
    ownership: str = "proposed"
    inventory_id: int | None = None
    source: str = Field(min_length=1)
    locked_parameters: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)


def validate_parts(elements, parts):
    seen = set()
    for part in parts:
        if part.element_index >= len(elements) or part.element_index in seen:
            raise ValueError("Part index missing from path or duplicated")
        seen.add(part.element_index)
        fields = elements[part.element_index].model_dump()
        if any(key not in fields for key in part.locked_parameters):
            raise ValueError("Locked parameter is not an element property")


def part_groups(project):
    """Path-local primitives and their physical bindings in an archived project."""
    yield "primary", (project.get("system") or {}).get("elements", []), project.get("parts", [])
    for path in project.get("incident_paths", []):
        yield path["uid"], path.get("elements", []), path.get("parts", [])


class Assembly(StrictInput):
    uid: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=120)
    path_uid: str = Field(default="primary", min_length=1, max_length=100)
    basis: Literal["declared", "legacy_inferred"] = "declared"
    element_uids: list[str] = Field(min_length=1, max_length=80, description="Consecutive optical primitives belonging to one physical component, in path order.")


def legacy_assemblies(project):
    """Decode the old implicit grouping only when the project lacks a membership list."""
    elements = project["system"]["elements"]
    parts = {p["element_index"]: p for p in project.get("parts", [])}
    context = project.get("component_context") or {}
    groups = []
    claimed = set()

    def add(ids, name, basis="declared"):
        if not ids or claimed.intersection(ids):
            return
        key = str(uuid5(NAMESPACE_URL, "laserclaw:assembly:" + ids[0]))
        groups.append({"uid": key, "name": name or "晶体", "path_uid": "primary", "element_uids": ids, "basis": basis})
        claimed.update(ids)

    for group in context.get("crystal_groups", []):
        add(group.get("element_uids"), group.get("name"))
    add(context.get("crystal_element_uids"), context.get("crystal_name"))
    def legacy_key(row):
        i, element = row
        part = parts.get(i, {})
        if element.get("uid") and element["uid"] not in claimed and part.get("role") == "crystal":
            return ("crystal", part.get("inventory_id"))
        return None

    for key, rows in groupby(enumerate(elements), key=legacy_key):
        if key is not None:
            ids = [el["uid"] for _, el in rows]
            if len(ids) > 1:
                add(ids, "复合晶体", "legacy_inferred")
    return groups


def membership(assemblies, path_uid="primary"):
    return {uid: group for group in assemblies if group["path_uid"] == path_uid for uid in group["element_uids"]}


def object_keys(project, path=None):
    path_uid, elements = (path["uid"], path["elements"]) if path else ("primary", project["system"]["elements"])
    groups = membership(project.get("assemblies", []), path_uid)
    return [("assembly", groups[e["uid"]]["uid"]) if e.get("uid") in groups else ("element", e.get("uid") or i)
            for i, e in enumerate(elements)]


def validate_assemblies(project):
    paths = {"primary": project.system, **{p.uid: p for p in project.incident_paths}}
    indices = {key: {e.uid: i for i, e in enumerate(path.elements)} for key, path in paths.items()}
    used, identities = set(), set()
    path_parts = {"primary": project.parts, **{p.uid: p.parts for p in project.incident_paths}}
    stock = {uid: {p.element_index: p.inventory_id for p in parts if p.inventory_id is not None}
             for uid, parts in path_parts.items()}
    for group in project.assemblies:
        order = indices.get(group.path_uid, {})
        positions = [order.get(uid) for uid in group.element_uids]
        if None in positions or positions != list(range(positions[0], positions[0] + len(positions))):
            raise ValueError(f"「{group.name}」的成员须在所选光路上连续，且按光路顺序排列。")
        members = {(group.path_uid, uid) for uid in group.element_uids}
        if group.uid in identities or members & used:
            raise ValueError("每个元件只能属于一个实物，实物标识须唯一。")
        group_stock = stock[group.path_uid]
        if len({group_stock[i] for i in positions if i in group_stock}) > 1:
            raise ValueError(f"「{group.name}」关联了不同的库存物品。请将它们保留为独立实物。")
        used.update(members)
        identities.add(group.uid)

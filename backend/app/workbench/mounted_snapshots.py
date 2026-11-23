"""Reconcile frozen stock descriptions with the physical objects present after a layout edit."""
from copy import deepcopy
import re

from ..models import GeneratedContent

from .assemblies import part_groups


def installation_note(label, receipt):
    return (f"「{label}」采用库存 #{receipt['inventory_id']}「{receipt['inventory_name']}」。"
            f"与被换实物有关的旧假设请对照版本 #{receipt['previous_revision_id']} 复核。")


_OLD_INSTALL_NOTE = re.compile(r"「.+」采用库存 #\d+「.+」。与被换实物有关的旧假设请对照版本 #\d+ 复核。")


def _old_install_additions(after, before):
    revision, doc = after.id, after.project
    old = before.project
    receipt = (doc.get("component_context") or {}).get("last_replacement") or {}
    if (receipt.get("previous_revision_id") != before.id or after.parent != before.id or revision == before.id
            or not {"inventory_id", "inventory_name", "target_element_uid"}.issubset(receipt)):
        return []
    owner = receipt.get("target_path_uid", "primary")
    paths = {uid: (elements, parts) for uid, elements, parts in part_groups(doc)}
    if owner not in paths:
        return []
    elements, parts = paths[owner]
    if not any(p.get("inventory_id") == receipt["inventory_id"] and
               elements[p["element_index"]].get("uid") == receipt["target_element_uid"] for p in parts):
        return []
    label = "主光路" if owner == "primary" else next(p["name"] for p in doc["incident_paths"] if p["uid"] == owner)
    note = installation_note(label, receipt)
    prior = list(dict.fromkeys(old.get("assumptions", [])))
    authored = doc.get("assumptions", [])
    # The legacy stock command appended this exact note and the model warnings
    # to the parent's assumptions, in a revision that also appended the receipt
    # and change note to the parent's notebook. Preserve every preexisting line.
    if (authored[:len(prior)+1] == [*prior, note] and
            doc.get("notes") == "\n".join(filter(None, (old.get("notes"), after.change_note, note)))):
        return authored[len(prior):]
    return []


def reconcile_legacy_assumptions(db, project, source):
    markers = {line for line in project.assumptions if _OLD_INSTALL_NOTE.fullmatch(line)}
    review_key = "unverified_installation_notes"
    if not markers and review_key not in project.component_context:
        return project
    additions = set()
    if markers and source and source.get("revision_id"):
        row = GeneratedContent
        records = db.query(row.id, row.case_id, row.content["project"].label("project"),
                           row.content["change_note"].as_string().label("change_note"),
                           row.content["parent_revision_id"].as_integer().label("parent")).filter(
                               row.content_type.in_(("optical_project", "optical_snapshot")))
        after = records.filter(row.id == source["revision_id"]).first()
        continuous = set(project.assumptions)
        seen = set()
        while after and after.parent and after.id not in seen:
            seen.add(after.id)
            continuous.intersection_update(after.project.get("assumptions", []))
            if not (markers - additions) & continuous:
                break
            before = records.filter(row.case_id == after.case_id, row.id == after.parent).first()
            if before is None:
                break
            additions.update(continuous.intersection(_old_install_additions(after, before)))
            after = before
    context = dict(project.component_context)
    if unresolved := sorted(markers - additions):
        context[review_key] = unresolved
    else:
        context.pop(review_key, None)
    return project.model_copy(update={"component_context": context,
                                      "assumptions": [line for line in project.assumptions if line not in additions]})


def forget_mount_warnings(context, key):
    by_role = context.get("warnings_by_role", {})
    removed = by_role.pop(key, [])
    if removed:
        remaining = {line for lines in by_role.values() for line in lines}
        context["scope_warnings"] = [line for line in context.get("scope_warnings", [])
                                     if line not in removed or line in remaining]


def reconcile_removed_mounts(project, previous):
    old_root = previous.get("component_context") or {}
    if not old_root:
        return project
    root = deepcopy(project.component_context)
    local_contexts = root.get("path_contexts", {})
    paths = {"primary": (project.system, project.parts),
             **{path.uid: (path, path.parts) for path in project.incident_paths}}
    receipt = root.get("last_replacement") or {}
    for owner, old_elements, old_parts in part_groups(previous):
        old = old_root if owner == "primary" else old_root.get("path_contexts", {}).get(owner, {})
        current = root if owner == "primary" else local_contexts.get(owner, {})
        if owner not in paths:
            local_contexts.pop(owner, None)
            if receipt.get("target_path_uid") == owner:
                root.pop("last_replacement", None)
            continue
        optics, bindings = paths[owner]
        present = {element.uid for element in optics.elements}
        removed = {element["uid"] for element in old_elements if element.get("uid")} - present
        if receipt.get("target_path_uid", "primary") == owner and receipt.get("target_element_uid") in removed:
            root.pop("last_replacement", None)
        if (current.get("last_replacement") or {}).get("target_element_uid") in removed:
            # The root also contains the last replacement on a private path.
            if (current["last_replacement"].get("target_path_uid", "primary") == owner):
                current.pop("last_replacement")
        if not old:
            continue
        old_active = old.get("crystal_element_uids") or []
        # Early receipts applied one crystal specification to all crystal parts
        # on this path and did not carry a separate list of member identities.
        if old.get("crystal_spec") and not old_active:
            old_active = [old_elements[p["element_index"]].get("uid") for p in old_parts if p.get("role") == "crystal"]
        crystals = [group["element_uids"] for group in old.get("crystal_groups", [])]
        if old_active:
            crystals.append(old_active)
        media = [item["medium_projection"].get("element_uids", []) for item in old.get("components", {}).values()
                 if isinstance(item, dict) and isinstance(item.get("medium_projection"), dict)]
        detached = set()
        for members in crystals + media:
            if not members or not all(members):
                continue
            identities = frozenset(members)
            if identities & removed:
                if identities & present:
                    raise ValueError("只移除了库存实物的一部分分段。请整体卸下或替换实物，保留该件所有分段的对应关系。")
                if identities <= removed:
                    detached.add(identities)
        current_groups = current.get("crystal_groups", [])
        surviving = [g for g in current_groups if frozenset(g["element_uids"]) not in detached]
        if len(surviving) != len(current_groups):
            current["crystal_groups"] = surviving
        active = current.get("crystal_element_uids") or []
        crystal_parts = {optics.elements[p.element_index].uid for p in bindings if p.role == "crystal"}
        legacy_detached = (not active and current.get("crystal_spec") and current["crystal_spec"] == old.get("crystal_spec")
                           and old_active and not crystal_parts)
        if frozenset(active) not in detached and not legacy_detached:
            continue
        for key in ("crystal_inventory_id", "crystal_spec", "crystal_orientation", "crystal_name",
                    "crystal_sections", "crystal_element_uids"):
            current.pop(key, None)
        current.get("components", {}).pop("crystal", None)
        current.get("component_selection", {}).pop("crystal", None)
        forget_mount_warnings(current, "crystal")
        if not surviving:
            current.pop("crystal_groups", None)
        elif len(surviving) == 1 and set(surviving[0]["element_uids"]) == crystal_parts:
            group = surviving[0]
            current.update(crystal_inventory_id=group.get("inventory_id"), crystal_spec=group["spec"],
                           crystal_orientation=group.get("orientation"), crystal_name=group.get("name"),
                           crystal_element_uids=group["element_uids"])
            if frozen := group.get("stock_snapshot"):
                current.setdefault("components", {})["crystal"] = deepcopy(frozen)
    return project.model_copy(update={"component_context": root}) if root != project.component_context else project

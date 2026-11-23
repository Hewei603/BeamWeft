"""Carry geometric connections across a hardware swap, retire evidence for the old piece."""

from math import isclose

from .authoring import EditableSystem
from .observation import detector_segment, host_positions
from .optics import SCAN_PARAMETERS


def _station_locations(project):
    anchors = host_positions(EditableSystem.model_validate(project["system"]))
    for plane in project["observation_planes"]:
        reference = plane.get("position_reference", "element_depth")
        position = plane["z_mm"] if reference == "fixed_z" else plane["depth_mm"]
        try:
            element, start, depth = detector_segment(anchors, plane.get("target_uid"), reference, position,
                                                      plane.get("boundary_side"))
            yield plane, start + depth, element, None
        except ValueError as exc:
            yield plane, None, None, str(exc)


def station_changes(original, candidate, path_uid):
    """Report what moved or changed medium using the solver's own registration conventions."""
    if path_uid != "primary" or not candidate.get("observation_planes"):
        return []
    previous = {p["uid"]: (z, el, issue) for p, z, el, issue in _station_locations(original)}
    changes = []
    for plane, z, el, issue in _station_locations(candidate):
        old_z, old_el, old_issue = previous[plane["uid"]]
        label = f"观察面「{plane['name']}」"
        fixed = plane.get("position_reference") == "fixed_z"
        if issue != old_issue and issue:
            note = f"{label}保留原登记位置。{issue} 请核对定位后再使用此处的预测值。"
        elif z is not None and old_z is None:
            note = f"{label}的登记位置现在位于「{el.name}」内，距主光路起点 {z:g} mm。"
        elif z is not None and not isclose(z, old_z, rel_tol=0., abs_tol=1e-9):
            note = f"{label}以所属区段入口为基准，距主光路起点从 {old_z:g} mm 变为 {z:g} mm。"
        elif fixed and el and old_el and el.uid != old_el.uid:
            note = f"{label}固定在 {z:g} mm，取样位置从「{old_el.name}」变为「{el.name}」。"
        else:
            continue
        changes.append({"uid": plane["uid"], "name": plane["name"], "old_z_mm": old_z, "new_z_mm": z,
                        "old_element_uid": old_el.uid if old_el else None,
                        "new_element_uid": el.uid if el else None, "note": note})
    return changes


def _update_shared_route(path, old_ids, new_ids):
    """A route can follow the outside faces of an assembly without identifying its internal interfaces."""
    entry = path["target_uid"]
    rows = path.get("downstream", [])
    old = set(old_ids)
    if path.get("through_uid"):
        end = path["through_uid"]
        referenced = {entry, end}.union(r["target_uid"] for r in rows)
        if old.isdisjoint(referenced):
            return []
        if entry in old:
            path["target_n"] = None
            if entry == old_ids[0]:
                path["target_uid"] = new_ids[0]
        if end == old_ids[-1]:
            path["through_uid"] = new_ids[-1]
        path["downstream"] = [r for r in rows if r["target_uid"] not in old]
    else:
        # Legacy routes are consecutive lists, and may stop at an internal interface.
        route = [{"target_uid": entry, "n": path.get("target_n")}, *rows]
        overlap = [r["target_uid"] for r in route if r["target_uid"] in old]
        if not overlap:
            return []
        if overlap == old_ids:
            start = next(i for i, row in enumerate(route) if row["target_uid"] == old_ids[0])
            route[start:start + len(old_ids)] = [{"target_uid": uid, "n": None} for uid in new_ids]
        else:
            for row in route:
                if row["target_uid"] in old:
                    row["n"] = None
        head = route[0]
        path["downstream"] = route[1:]
        path["target_uid"], path["target_n"] = head["target_uid"], head["n"]
    if path["target_uid"] in old or path.get("through_uid") in old or any(r["target_uid"] in old for r in path["downstream"]):
        return [f"支路「{path['name']}」原先连接旧元件的内部区段。请在新实物上重新指定入口和终点，再核对支路折射率。"]
    return []


def _update_planes(project, previous, installed):
    old_ids = {e["uid"] for e in previous}
    first = installed[0]
    messages = []
    for plane in project.get("observation_planes", []):
        if plane.get("target_uid") not in old_ids:
            continue
        # An element-relative depth belongs to a particular homogeneous segment.
        same_origin = plane["target_uid"] == previous[0]["uid"]
        one_segment = len(previous) == len(installed) == 1
        depth = plane["depth_mm"]
        if same_origin and first["kind"] == "space" and (depth == 0 or (one_segment and depth <= first["length_mm"])):
            plane["target_uid"] = first["uid"]
        else:
            messages.append(f"观察面「{plane['name']}」以旧分段为基准，深度为 {depth:g} mm。请在新实物上重新定位此面。")
    return messages


def reconcile_stock_references(project, path_uid, previous_elements, installed_elements):
    """Update only the candidate revision. Historical observations retain their original model."""
    old_ids = [e["uid"] for e in previous_elements]
    new_ids = [e["uid"] for e in installed_elements]
    old = set(old_ids)
    primary = path_uid == "primary"
    messages = []
    paths = project.get("incident_paths", [])
    if primary:
        for path in paths:
            previous_indices = {path["target_uid"]: path.get("target_n"),
                                **{s["target_uid"]: s.get("n") for s in path.get("downstream", [])}}
            messages.extend(_update_shared_route(path, old_ids, new_ids))
            independent = path.get("independent_index_uids", [])
            if old.intersection(independent) or any(previous_indices.get(uid) is not None for uid in old_ids):
                if path.get("through_uid"):
                    kept = [uid for uid in independent if uid not in old]
                    media = [el["uid"] for el in installed_elements if "n" in SCAN_PARAMETERS.get(el["kind"], ())]
                    path["independent_index_uids"] = list(dict.fromkeys(kept + media))
                messages.append(f"支路「{path['name']}」的旧件折射率已移出本版。请核对新实物在该支路波长、偏振下的折射率。")
        messages.extend(_update_planes(project, previous_elements, installed_elements))

    scope = "host" if primary else "arm"
    budgets = [(p["name"], p.get("power_budget")) for p in paths if primary or p["uid"] == path_uid]
    if primary:
        budgets.append(("主光路", project.get("single_pass_power")))
    for name, budget in budgets:
        if budget:
            stages = budget.get("stages", [])
            keep = [s for s in stages if s["scope"] != scope or s["element_uid"] not in old]
            if len(keep) < len(stages):
                budget["stages"] = keep
                messages.append(f"「{name}」的旧件功率传输参数保留在原版本。请为新件补充保留率或分面损耗。")

    network = project.get("network") or {}
    for drive in network.get("drives", []):
        power = drive.get("path_output") or {}
        profile = drive.get("profile") or {}
        references = [(power, power.get("path_uid")), (profile.get("element_exit") or {}, profile.get("beam_uid"))]
        for ref, beam_uid in references:
            if ref.get("scope") != scope or (not primary and beam_uid != path_uid):
                continue
            uid = ref.get("element_uid")
            if uid == old_ids[-1]:
                ref["element_uid"] = new_ids[-1]
            elif uid in old:
                terminal = drive["at"]
                messages.append(f"网络端口 {terminal['component']} / {terminal['port']} 引用旧实物的内部界面。请重新指定取样位置。")
    if any(e.get("clear_aperture") for e in previous_elements) and not any(e.get("clear_aperture") for e in installed_elements):
        messages.append("旧件的净孔径已移出本版。需填入新件的有效通光尺寸，才能计入该孔径的截光。")
    return list(dict.fromkeys(messages))

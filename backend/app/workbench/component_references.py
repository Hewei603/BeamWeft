"""Local coordinates and cross-domain connections of a portable optical assembly."""

from uuid import uuid4

from .observation import LayoutSystem, host_positions, detector_segment


def capture_planes(project, path_uid, elements, plane_uids):
    """Freeze a chosen instrument station onto its physical supporting element."""
    slots = {el["uid"]: i for i, el in enumerate(elements)}
    planes = project.get("observation_planes", [])
    if plane_uids is None:
        plane_uids = [p["uid"] for p in planes if path_uid == "primary" and p.get("target_uid") in slots]
    if not plane_uids:
        return []
    if path_uid != "primary":
        raise ValueError("工程观察面定位在主光路上。请选择含定位元件的主光路区段。")
    wanted = set(plane_uids)
    anchors = host_positions(LayoutSystem.model_validate(project["system"]))
    result = []
    for p in planes:
        if p["uid"] not in wanted:
            continue
        fixed = p.get("position_reference") == "fixed_z"
        el, _, depth = detector_segment(anchors, p.get("target_uid"), "fixed_z" if fixed else "element_depth",
                                        p["z_mm"] if fixed else p["depth_mm"], p.get("boundary_side"))
        if el.uid not in slots:
            raise ValueError(f"观察面「{p['name']}」位于所选范围之外。请扩大元件范围或取消选择该面。")
        result.append({"name": p["name"], "element": slots[el.uid], "depth_mm": depth,
                       "source_plane": p.copy()})
    if len(result) != len(wanted):
        raise ValueError("原工程版本中找不到所选观察面。请刷新后重新选择。")
    return result


def capture_bindings(project, path_uid, elements, nodes, planes):
    """Keep an edge only when both its source and its destination travel."""
    slots = {el["uid"]: i for i, el in enumerate(elements)}
    terminals = {n["uid"]: i for i, n in enumerate(nodes)}
    stations = {p["source_plane"]["uid"]: i for i, p in enumerate(planes)}
    scope = "host" if path_uid == "primary" else "arm"
    result = []
    coherence_groups = {}
    for drive in project["network"]["drives"]:
        end = drive["at"]
        if end["component"] not in terminals:
            continue
        binding = {}
        feed = drive.get("path_output") or {}
        if feed.get("path_uid") == path_uid and feed["scope"] == scope and feed["element_uid"] in slots:
            binding["power_element"] = slots[feed["element_uid"]]
        profile = drive.get("profile") or {}
        if profile.get("kind") == "layout" and profile["beam_uid"] == path_uid:
            ref = profile.get("element_exit")
            if profile.get("plane_uid") in stations:
                binding["field_plane"] = stations[profile["plane_uid"]]
            elif ref and ref["scope"] == scope and ref["element_uid"] in slots:
                binding["field_element"] = slots[ref["element_uid"]]
            elif not profile.get("plane_uid") and not ref and path_uid == "primary":
                # The old path exit becomes the physical exit face, even if a
                # different optic is already downstream in the recipient.
                last_uid = project["system"]["elements"][-1]["uid"]
                if last_uid in slots:
                    binding["field_element"] = slots[last_uid]
        if binding:
            if drive.get("coherence_group"):
                group = drive["coherence_group"]
                binding["coherence_group"] = coherence_groups.setdefault(group, len(coherence_groups) + 1)
                binding["phase_deg"] = drive.get("phase_deg")
            result.append({**binding, "component": terminals[end["component"]], "port": end["port"]})
    return result


def binding_descriptions(model):
    descriptions = []
    for binding in model["bindings"]:
        node = model["network"]["components"][binding["component"]]
        power = field = None
        if "power_element" in binding:
            power = model["elements"][binding["power_element"]]["name"] + " · 出射面"
        if "field_element" in binding:
            field = model["elements"][binding["field_element"]]["name"] + " · 出射面"
        if "field_plane" in binding:
            field = model["reference_planes"][binding["field_plane"]]["name"]
        descriptions.append({"name": node["name"], "component": node["uid"], "port": binding["port"],
                             "power_reference": power, "field_reference": field})
    return descriptions


def place_references(project, model, path_uid, elements, nodes, instance_uid):
    stations = []
    for saved in model.get("reference_planes", []):
        if path_uid != "primary":
            raise ValueError("本组件包含定位在主光路上的观察面。请选择主光路放置，或另存只含支路元件的模型。")
        station = {"uid": str(uuid4()), "name": saved["name"], "position_reference": "element_depth",
                   "target_uid": elements[saved["element"]]["uid"], "depth_mm": saved["depth_mm"]}
        stations.append(station)
        project["observation_planes"].append(station)

    scope = "host" if path_uid == "primary" else "arm"
    for binding in model.get("bindings", []):
        drive = {"at": {"component": nodes[binding["component"]]["uid"], "port": binding["port"]}}
        if "power_element" in binding:
            drive["path_output"] = {"path_uid": path_uid, "scope": scope,
                                    "element_uid": elements[binding["power_element"]]["uid"]}
        if "field_plane" in binding:
            drive["profile"] = {"kind": "layout", "beam_uid": path_uid, "plane_uid": stations[binding["field_plane"]]["uid"]}
        if "field_element" in binding:
            drive["profile"] = {"kind": "layout", "beam_uid": path_uid, "element_exit": {
                "scope": scope, "element_uid": elements[binding["field_element"]]["uid"]}}
        if "coherence_group" in binding:
            drive.update(coherence_group=f"{instance_uid}/{binding['coherence_group']}", phase_deg=binding["phase_deg"])
        project["network"]["drives"].append(drive)

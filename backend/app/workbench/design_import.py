"""Open the numerical geometry of a saved design as an editable optical model."""
import hashlib
import math
from uuid import uuid4

from .assemblies import Assembly
from .optics import air_gap
from .projects import OpticalProject, project_from_design
from .pump_geometry import project_from_candidate_pump, project_from_pump_assessment
from .study_inputs import SaveVariant, save_variant


def _assessed_project(design, cold, row):
    geometry = cold["geometry"]
    crystal = geometry["crystal"]
    values = design["resolved_inputs"]
    segments = crystal.get("segments") or [{"name": "晶体 · 均匀等效介质", "length_mm": crystal["thickness_mm"]}]
    elements = [{"name": "M1 输入镜", "kind": "mirror", "radius_mm": geometry["R1_mm"]},
                air_gap("M1 到晶体前端面", row["input_gap_mm"])]
    for segment in segments:
        elements.append({"uid": str(uuid4()), "name": segment["name"], "kind": "space", "length_mm": segment["length_mm"],
                         "medium_role": segment.get("medium_role"), "n": crystal["n"]})
    elements.extend([air_gap("晶体后端面到 M2", row["output_gap_mm"]),
                     {"name": "M2 输出镜", "kind": "mirror", "radius_mm": geometry["R2_mm"]}])
    def physical_part(index, role, binding, owned=False):
        inventory_id = values.get(binding)
        physical = owned or inventory_id is not None
        locks = ["kind", "length_mm"] if role == "crystal" else ["kind", "radius_mm"]
        return {"element_index": index, "role": role, "inventory_id": inventory_id,
                "ownership": "owned" if physical else "unspecified",
                "source": "设计记录中明确绑定的库存或已有元件" if physical else "所选冷腔的几何条件",
                "locked_parameters": locks if physical else []}

    parts = [physical_part(0, "pump_input", "M1_inventory_id", values.get("input_mirror_owned"))]
    parts.extend(physical_part(i, "crystal", "crystal_inventory_id") for i in range(2, 2 + len(segments)))
    parts.append(physical_part(len(elements) - 1, "laser_output", "M2_inventory_id", values.get("output_mirror_owned")))
    assemblies = []
    if len(segments) > 1:
        assemblies.append(Assembly(uid=str(uuid4()), name="晶体",
                                   element_uids=[e["uid"] for e in elements[2:2 + len(segments)]]))
    return OpticalProject(title=f"{geometry['wavelength_nm']:g} nm 冷腔 · 镜间距 {row['length_mm']:g} mm",
                          system={"mode": "standing_wave", "wavelength_nm": geometry["wavelength_nm"],
                                  "elements": elements},
                          network=None, parts=parts, assemblies=assemblies,
                          assumptions=[geometry["scope"]], notes=geometry["basis"])


def _coupled_project(design, cavity, row, pump):
    """Share physical segments, preserving both wavelengths and the original M1 frame."""
    body = cavity.model_dump()
    host = body["system"]["elements"]
    crystal_indices = [p.element_index for p in cavity.parts if p.role == "crystal"]
    crystal = [host[i] for i in crystal_indices]
    pump_segment, = (p.element_index for p in pump.parts if p.role == "crystal")
    material = pump.system.elements[pump_segment]
    origin = pump.component_context["design_coordinates"]["origin_z_mm"]
    train = [e.model_dump() for e in pump.system.elements[:pump_segment]]
    front = row["input_gap_mm"] if "input_gap_mm" in row else row["crystal"]["position_mm"]
    thickness = sum(e["length_mm"] for e in crystal)
    if (not math.isclose(thickness, material.length_mm, rel_tol=1e-12, abs_tol=1e-9)
            or not math.isclose(front, origin + sum(e["length_mm"] for e in train), rel_tol=1e-12, abs_tol=1e-9)):
        raise ValueError("两个计算中的晶体厚度或前表面位置不一致。请分别打开光路，核对同一实物及坐标后再合并。")

    # The main cavity starts at M1. Share its air gap downstream of the last
    # private lens so that moving the crystal does not also move the pump source.
    last_lens_z = front - train[-1]["length_mm"]
    join_z = max(0., last_lens_z)
    air_index = crystal_indices[0] - 1
    air = host[air_index]
    if join_z > 0:
        host.insert(air_index, {**air, "uid": str(uuid4()), "name": "M1 到泵浦接入面", "length_mm": join_z})
        for part in body["parts"]:
            if part["element_index"] >= air_index:
                part["element_index"] += 1
    air["length_mm"] = front - join_z
    air["name"] = "共用空气段"
    train[-1].update(name="聚焦镜到共用入口", length_mm=join_z - last_lens_z)
    branch = {"uid": str(uuid4()), "name": "泵浦光路", "wavelength_nm": pump.system.wavelength_nm,
              **pump.system.source.model_dump(), "elements": train,
              "target_uid": air["uid"], "target_n": 1., "through_uid": crystal[-1]["uid"],
              "inherit_index_at_same_wavelength": False,
              "downstream": [{"target_uid": e["uid"], "n": material.n} for e in crystal],
              "source_note": f"原设计的泵浦源面位于 z={origin:g} mm，接入面位于 z={join_z:g} mm，均相对 M1。"
                             "源参数与取值依据见原设计记录。"}
    branch["parts"] = [p.model_dump() for p in pump.parts if p.element_index < pump_segment]
    body["incident_paths"] = [branch]
    body["component_context"]["design_coordinates"] = {
        **pump.component_context["design_coordinates"], "origin_z_mm": 0., "pump_source_z_mm": origin,
    }
    targets = [("晶体几何中点", thickness / 2)]
    targets.extend((p.name, p.depth_mm) for p in pump.observation_planes)
    comparison = design.get("evidence_assessment", {}).get("cold_cavity", {}).get("target_comparison", {})
    target_depth = comparison.get("depth_from_crystal_front_mm")
    if target_depth is not None:
        targets.append(("目标模半径截面", target_depth))
    planes_at = []
    for name, depth in targets:
        existing = next((plane for z, plane in planes_at if math.isclose(z, depth, rel_tol=0, abs_tol=1e-9)), None)
        if existing:
            existing["name"] += " / " + name
            continue
        offset = 0.
        for i, segment in enumerate(crystal):
            end = offset + segment["length_mm"]
            if depth < end or i == len(crystal) - 1:
                if depth < -1e-9 or depth > end + 1e-9:
                    raise ValueError("已存观察面超出两路共用的晶体。请核对原设计截面位置。")
                plane = {"uid": str(uuid4()), "name": name, "target_uid": segment["uid"],
                         "depth_mm": max(0., min(depth - offset, segment["length_mm"]))}
                planes_at.append((depth, plane))
                break
            offset = end
    body["observation_planes"] = [p for _, p in planes_at]
    pump_label = f"{pump.system.wavelength_nm:g} nm 泵浦" if pump.system.wavelength_nm is not None else "泵浦"
    body["title"] = f"{pump_label}与 {cavity.system.wavelength_nm:g} nm 冷腔"
    body["assumptions"].extend(pump.assumptions)
    body["notes"] += (" 联合画布以 M1 腔内反射面为 z=0。两路共用同一晶体，各自使用对应波长的折射率。"
                      "泵浦束心和指向待测，截面半径可用于比较两束光的尺寸。")
    return OpticalProject.model_validate(body)


def saved_geometry(design, content_id, rank, previous=None, *, target="cold_cavity"):
    policy = design.get("assumption_policy", "unrecorded")
    cold = design.get("evidence_assessment", {}).get("cold_cavity", {})
    assessed = policy == "evidence_only"
    if target not in {"cold_cavity", "pump_path", "coupled_layout"}:
        raise ValueError("请选择记录中的可编辑光路。")
    if target == "pump_path" and assessed:
        if rank != 1:
            raise ValueError("独立泵浦光路只有一条。")
        project = project_from_pump_assessment(design)
        collection = "pump_imaging"
    else:
        entries = cold.get("rows", []) if assessed else design.get("candidates", [])
        if not 1 <= rank <= len(entries):
            raise ValueError("该设计记录没有所选的冷腔几何。请核对记录中的行号和计算状态。")
        row = entries[rank - 1]
        if target == "pump_path":
            project = project_from_candidate_pump(design, row)
            collection = "candidates.pump"
        elif assessed:
            project = _assessed_project(design, cold, row)
            collection = "cold_cavity.rows"
        else:
            project = project_from_design(design, row, previous)
            if previous is None:
                project.network = None
            project.assumptions.append("此几何取自原设计的候选搜索。其材料、尺寸及筛选条件可在原设计记录中逐项核对。")
            collection = "candidates"
        if target == "coupled_layout":
            pump = project_from_pump_assessment(design) if assessed else project_from_candidate_pump(design, row)
            project = _coupled_project(design, project, row, pump)
            collection = "cold_cavity+pump_imaging" if assessed else "candidates+pump"
    project.component_context["design_origin"] = {"content_id": content_id, "selection": rank,
        "assumption_policy": policy, "collection": collection}
    return project


def independent_design(db, case_id, design, content_id, rank, source=None, base=None, *, target="cold_cavity"):
    project = saved_geometry(design, content_id, rank, target=target)
    pump_label = "泵浦光路" if design.get("assumption_policy") == "evidence_only" else f"泵浦候选 {rank}"
    label = {"cold_cavity": f"冷腔 {rank}", "pump_path": pump_label, "coupled_layout": f"泵浦与冷腔 {rank}"}[target]
    label = f"设计 #{content_id} · {label}"
    request = SaveVariant(project=project, variant_name=label, source_revision_id=source, base_revision_id=base)
    # The immutable saved record and selection identify the calculation across double-clicks and HTTP retries.
    key = f"design-geometry:{content_id}:{rank}:{source or 0}:{base or 0}"
    if target != "cold_cavity":
        key = "design:" + hashlib.sha256(f"{key}:{target}".encode()).hexdigest()
    return save_variant(db, case_id, request, operation_key=key)

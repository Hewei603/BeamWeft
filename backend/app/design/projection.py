"""Translate a representable coupled workbench revision into scalar planner inputs."""
import math

from .brief import FIELDS


def coupled_inputs(project, revision_id, existing):
    """Read optical actions, path registration and source at the selected revision.

    Unknown model values clear stale case inputs. User measurements remain in the
    case and original receipts, independent of this numerical projection.
    """
    from ..workbench.observation import host_positions, host_route
    from ..workbench.projects import OpticalProject

    paths = project.get("incident_paths", [])
    context = project.get("component_context") or {}
    origin = context.get("design_origin") or {}
    if not paths:
        if origin.get("collection") in {"candidates+pump", "cold_cavity+pump_imaging"}:
            raise ValueError("此版本已经移除原联合设计中的入射光路。")
        return existing
    if len(paths) != 1:
        raise ValueError("当前工程有多条入射光路，专用规划器只能表示一条。")
    layout = OpticalProject.model_validate(project)
    host, path = layout.system, layout.incident_paths[0]
    if (layout.network is not None or layout.single_pass_power is not None or path.power_budget is not None
            or any(e.clear_aperture or e.prescription for e in [*host.elements, *path.elements])):
        raise ValueError("当前工程登记了端口网络、功率预算、孔径或参数化元件，专用近轴规划无法表示这些作用。")
    crystals = sorted(p.element_index for p in layout.parts if p.role == "crystal")
    if not crystals or crystals != list(range(crystals[0], crystals[-1] + 1)):
        raise ValueError("泵浦与腔模共用的晶体需要是一组连续且已标识的介质段。")
    crystal = host.elements[crystals[0]:crystals[-1] + 1]
    before, after = host.elements[1:crystals[0]], host.elements[crystals[-1]+1:-1]
    if (any(e.n != 1 for e in [*before, *after]) or any(e.kind != "space" for e in crystal)
            or len({e.n for e in crystal}) != 1):
        raise ValueError("当前腔内的外部介质或分段折射率不能归并为一块均匀介质和空气间隙。")
    anchors = host_positions(host)
    route, issue = host_route(path, anchors, host.wavelength_nm)
    if issue:
        raise ValueError(issue)
    ids = [e.uid for e in crystal]
    if ([e.uid for e, _, _ in route[-len(ids):]] != ids
            or any(e.kind != "space" or n != 1 for e, _, n in route[:-len(ids)])
            or len({n for _, _, n in route[-len(ids):]}) != 1):
        raise ValueError("入射支路没有经空气连续通过同一晶体，或各段泵浦折射率不同。")
    parts = {p.element_index: p for p in path.parts}
    lens_indices = [i for i, e in enumerate(path.elements) if e.kind != "space"]
    roles = [parts[i].role if i in parts else None for i in lens_indices]
    if (roles != ["collimator", "focus_lens"]
            or any(path.elements[i].kind != "lens" or path.elements[i].aoi_deg != 0 for i in lens_indices)
            or any(e.kind == "space" and e.n != 1 for e in path.elements)):
        raise ValueError("专用泵浦模型需要按传播顺序登记准直和聚焦两片薄透镜，镜间为均匀空气。")
    axis = path.tangential
    if (axis.model_dump(exclude={"fit_origin"}) != path.axis("sagittal").model_dump(exclude={"fit_origin"}) or axis.wavefront == "curved"
            or path.launch_ray and any(v not in (None, 0) for v in path.launch_ray.model_dump().values())):
        raise ValueError("当前源的独立双轴、波前曲率或离轴指向需要通用逐轴传播。")

    front = anchors[crystal[0].uid][2]
    start = route[0][1] - math.fsum(e.length_mm for e in path.elements)
    thickness = math.fsum(e.length_mm for e in crystal)
    values = {"cavity_type": "linear", "crystal_front_mm": front, "crystal_length_mm": thickness,
              "output_gap_mm": math.fsum(e.length_mm for e in after), "crystal_index": crystal[0].n,
              "laser_wavelength_nm": host.wavelength_nm, "pump_wavelength_nm": path.wavelength_nm,
              "pump_index": route[-1][2], "source_z_mm": start,
              "collimator_roc_mm": None, "collimator_index": None}
    # The current segment roles determine whether a gain-center target is defined.
    if any(e.medium_role for e in crystal):
        gain = [i for i, e in enumerate(crystal) if e.medium_role == "gain"]
        if (not gain or gain != list(range(gain[0], gain[-1] + 1))
                or any(e.medium_role not in {"gain", "undoped"} for e in crystal)):
            raise ValueError("当前增益、吸收或无源分段不能表示为单个连续增益区。")
        values.update(front_cap_mm=math.fsum(e.length_mm for e in crystal[:gain[0]]),
                      active_length_mm=math.fsum(e.length_mm for e in crystal[gain[0]:gain[-1]+1]),
                      rear_cap_mm=math.fsum(e.length_mm for e in crystal[gain[-1]+1:]))
    else:
        values.update(front_cap_mm=None, active_length_mm=None, rear_cap_mm=None)
    for i, focal, z_key in zip(lens_indices, ("collimator_f_mm", "focus_f_mm"), ("collimator_z_mm", "focus_lens_z_mm")):
        part = parts[i]
        values.update({focal: path.elements[i].focal_mm, z_key: start + math.fsum(e.length_mm for e in path.elements[:i]),
                       f"{part.role}_inventory_id": part.inventory_id, f"{part.role}_owned": part.ownership == "owned"})
    if not start < values["collimator_z_mm"] < values["focus_lens_z_mm"] < front:
        raise ValueError("该支路的源、两片透镜与晶体的轴向位置不满足专用传播的顺序。")
    main_parts = {p.element_index: p for p in layout.parts}
    for i, prefix, stock in ((0, "input", "M1"), (len(host.elements)-1, "output", "M2")):
        radius, part = host.elements[i].radius_mm, main_parts.get(i)
        values.update({f"{prefix}_roc_mm": radius, f"{prefix}_mirror_flat": radius is None,
                       f"{prefix}_mirror_owned": bool(part and part.ownership == "owned"),
                       f"{stock}_inventory_id": part.inventory_id if part else None})
    values["crystal_inventory_id"] = main_parts[crystals[0]].inventory_id
    # An equivalent launch inferred from a nominal core and NA is not a measured
    # second moment. A fit registered at this reference plane can supply new data.
    basis = (context.get("design_coordinates") or {}).get("source_basis")
    nominal = basis == "nominal_source_scenario" or (basis is None and origin.get("assumption_policy") == "exploratory")
    measured = axis.wavefront == "waist" and axis.second_moment_radius and (not nominal or axis.fit_origin is not None)
    radius = axis.radius_mm if measured else None
    values["pump_exit_waist_radius_um"] = radius * 1000 if radius else None
    values["pump_farfield_half_angle_mrad"] = (axis.m2 * path.wavelength_nm / (1000 * math.pi * radius)
                                                if radius and axis.m2 and path.wavelength_nm else None)
    facts = existing.copy()
    for key, value in values.items():
        if value is None:
            facts.pop(key, None)
        else:
            facts[key] = {"value": value, "source": f"optical_project:{revision_id}", "verification": "project_geometry",
                          "quote": "所选工程版本的几何、实物绑定或源面参数", "label": FIELDS[key][0], "unit": FIELDS[key][1]}
    return facts


def accepted_updates(existing, baseline, merged):
    """Publish only accepted intake edits; preserve original receipts for model-derived fields."""
    result = existing.copy()
    for key in baseline.keys() | merged.keys():
        if baseline.get(key) != merged.get(key):
            if key in merged:
                result[key] = merged[key]
            else:
                result.pop(key, None)
    return result


def record_projection(design, baseline, revision):
    if revision is None:
        return
    design["project_revision_id"] = revision
    if baseline.get("source_z_mm", {}).get("source") != f"optical_project:{revision}":
        return
    note = f"本次从工程版本 #{revision} 的入射支路和主腔读取布局，采用本轮明确修改的约束。"
    if design.get("assumption_policy") == "evidence_only" and "pump_farfield_half_angle_mrad" not in design["resolved_inputs"]:
        note += "泵浦源尚未提供可用于此计算的完整二阶矩数据。可用 analyze_observation_planes 查看该版本已定义的源模型及可算截面。"
    design["summary"] = note + (design.get("summary") or "")

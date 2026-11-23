"""Adjustable physical inputs and their owners in the intact optical project."""
from math import isfinite
from urllib.parse import quote

from .device_laws import Prescription, device_input_schema, editable_setting
from .observation import OpticalLayout, host_positions, host_route
from .optics import ELEMENT_CATALOG, SCAN_PARAMETERS


def _numbers(value, path):
    if type(value) in (float, int) and isfinite(value):
        yield path
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _numbers(item, (*path, key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _numbers(item, (*path, index))


def _fields(doc, keys, path):
    for key in keys:
        yield from _numbers(doc.get(key), (*path, key))


def _beam(source, path):
    for axis in ("tangential", "sagittal"):
        spec = source.get(axis) or {}
        fields = ["radius_mm", "m2"]
        if spec.get("wavefront") == "curved":
            fields.append("curvature_mm")
        yield from _fields(spec, fields, (*path, axis))
    yield from _fields(source, ["launch_ray"], path)


def _device(model, path, representation):
    prescription = model.get("prescription")
    if not prescription:
        return
    try:
        study = Prescription.model_validate(prescription).definition(representation)
    except ValueError:
        return  # The pinned law is unavailable. An archived example cannot declare its current inputs.
    law = study.element_law
    schema = device_input_schema(study.inputs)
    supplied = dict(prescription.get("parameters", {}))
    if law.length_input:
        supplied[law.length_input] = model.get("length_mm")
    for addr in _numbers(supplied, ()):
        if not editable_setting(schema, supplied, addr, schema):
            continue
        if addr[0] == law.length_input:
            yield (*path, "length_mm")
        elif addr[0] != law.wavelength_input:
            yield (*path, "prescription", "parameters", *addr)


def _elements(elements, path):
    for i, el in enumerate(elements):
        here = (*path, i)
        kind = el["kind"]
        fields = SCAN_PARAMETERS.get(kind, ())
        if (el.get("index_reference") or {}).get("model"):
            fields = [name for name in fields if name != "n"]
        yield from _fields(el, fields, here)
        yield from _fields(el, ["clear_aperture"], here)
        if kind == "matrix":
            yield from _fields(el, ["matrix_t", "matrix_s"], here)
        elif kind == "registered":
            yield from _device(el, here, "ray_matrix")


def _power(budget, path):
    if budget:
        def bounds(band, here):
            # Changing a measured or datasheet value in place would attach the old citation to a new number.
            if band and band.get("basis", "assumed") == "assumed":
                yield from _fields(band, ["low", "high"], here)

        yield from bounds(budget.get("input_w"), (*path, "input_w"))
        for i, stage in enumerate(budget.get("stages", ())):
            law = stage["law"]
            here = (*path, "stages", i, "law")
            for key, spec in law.items():
                if isinstance(spec, dict):
                    yield from bounds(spec, (*here, key))
            # Reference thickness is part of the supplied attenuation calibration.


def _network(network):
    path = ("network",)
    yield from _fields(network, ["wavelength_nm", "links"], path)
    for i, drive in enumerate(network.get("drives", ())):
        here = (*path, "drives", i)
        yield from _fields(drive, ["power_w", "phase_deg"], here)
        yield from _beam(drive.get("profile") or {}, (*here, "profile"))
    for i, part in enumerate(network.get("components", ())):
        model = part["model"]
        here = (*path, "components", i, "model")
        if model["kind"] == "registered":
            yield from _device(model, here, "scattering")
        else:
            # The actual discriminated scattering model contains only its selected law.
            yield from _fields(model, (k for k in model if k != "reference_wavelength_nm"), here)


def _model_paths(layout):
    system = layout["system"]
    yield from _fields(system, ["wavelength_nm"], ("system",))
    yield from _elements(system.get("elements", ()), ("system", "elements"))
    if system.get("mode") == "single_pass":
        if system.get("source") is not None:
            yield from _beam(system["source"], ("system", "source"))
        else:
            yield from _fields(system, ["input_radius_mm", "input_curvature_mm", "beam_quality_m2"], ("system",))
    routes = {}
    if layout.get("incident_paths"):
        # Only the continuously registered host route actually consumes wavelength-specific overrides.
        try:
            survey = OpticalLayout.model_validate({"system":system,"incident_paths":layout["incident_paths"]})
        except ValueError:
            survey = None
        if survey is not None:
            anchors = host_positions(survey.system)
            for arm in survey.incident_paths:
                connected, _ = host_route(arm, anchors, survey.system.wavelength_nm)
                routes[arm.uid] = {el.uid:el.kind for el, _, _ in connected}
    for i, arm in enumerate(layout.get("incident_paths", ())):
        path = ("incident_paths", i)
        yield from _beam(arm, path)
        yield from _fields(arm, ["wavelength_nm"], path)
        for j, section in enumerate([arm, *arm.get("downstream", ())]):
            kind = routes.get(arm["uid"], {}).get(section.get("target_uid"))
            if "n" in SCAN_PARAMETERS.get(kind, ()):
                if j == 0:
                    yield from _fields(arm, ["target_n"], path)
                else:
                    yield from _fields(section, ["n"], (*path, "downstream", j-1))
        yield from _elements(arm.get("elements", ()), (*path, "elements"))
        yield from _power(arm.get("power_budget"), (*path, "power_budget"))
    for i, plane in enumerate(layout.get("observation_planes", ())):
        field = "z_mm" if plane.get("position_reference") == "fixed_z" else "depth_mm"
        yield from _fields(plane, [field], ("observation_planes", i))
    yield from _power(layout.get("single_pass_power"), ("single_pass_power",))
    if layout.get("network"):
        yield from _network(layout["network"])


def _prefix_owner(path, origin, arm):
    elements = origin["element_sources"]
    if path[0] == "observation_planes":
        return elements[-1]["length_path"]
    if path[:2] == ("system", "elements"):
        el = elements[path[2]]
        if path[3] in ("length_mm", "n"):
            return el["length_path" if path[3] == "length_mm" else "n_path"]
        return [*el["source_path"], *path[3:]]
    if arm is None:
        return path
    if path == ("system", "wavelength_nm"):
        return [*arm, "wavelength_nm"]
    if path[:2] == ("system", "source"):
        return [*arm, *path[2:]]


def scan_bindings(project, origin, layout):
    """Exact numeric leaves only. Never grant write access via a model/container prefix."""
    prefix = origin.get("scope") == "layout_prefix"
    if (not prefix and origin.get("scope") != "layout") or not isinstance(layout.get("system"), dict):
        return {}
    arm = None
    if prefix and origin["path_uid"] != "primary":
        index = next(i for i, p in enumerate(project["incident_paths"]) if p["uid"] == origin["path_uid"])
        arm = ["incident_paths", index]
    locks = {(p["element_index"], name) for p in project.get("parts", ()) for name in p.get("locked_parameters", ())}
    bindings = {}
    for path in _model_paths(layout):
        owner = _prefix_owner(path, origin, arm) if prefix else path
        if owner is not None:
            if tuple(owner[:2]) == ("system", "elements") and (owner[2], owner[3]) in locks:
                continue
            node = project
            value = layout
            try:
                for key in owner:
                    node = node[key]
                for key in path:
                    value = value[key]
            except (KeyError, IndexError, TypeError):
                continue  # An old input schema may materialize a default that the project never recorded.
            if type(node) in (int, float) and node == value:
                bindings[path] = list(owner)
    return bindings


def parameter_label(path):
    if "prescription" in path:  # Extension authors own the semantics and names of these settings.
        return None
    if len(path) > 1 and path[-2] in ("matrix_t", "matrix_s") and type(path[-1]) is int:
        return ("A", "B / mm", "C / mm⁻¹", "D")[path[-1]]
    if len(path) >= 4 and path[-3] == "elements":
        return ELEMENT_CATALOG["parameters"].get(path[-1])
    return {
        "wavelength_nm":"真空波长 / nm", "input_radius_mm":"入射光束半径 / mm", "input_curvature_mm":"入射波前曲率半径 / mm",
        "radius_mm":"光束半径 / mm", "curvature_mm":"波前曲率半径 / mm", "m2":"光束质量 M²", "beam_quality_m2":"光束质量 M²",
        "target_n":"接入段折射率", "n":"折射率", "width_mm":"净孔径宽 / mm", "height_mm":"净孔径高 / mm",
        "center_t_mm":"切向中心 / mm", "center_s_mm":"弧矢中心 / mm", "angle_deg":"孔径转角 / °", "z_mm":"工程坐标 / mm",
        "length_mm":"物理长度 / mm", "phase_deg":"相位 / °", "power_w":"功率 / W", "through_fraction":"直通功率比例",
        "reflectance":"功率反射率", "power_transmission":"功率透射率", "t_offset_mm":"切向偏移 / mm",
        "s_offset_mm":"弧矢偏移 / mm", "t_angle_mrad":"切向指向 / mrad", "s_angle_mrad":"弧矢指向 / mrad",
        "shift_t_mm":"切向平移 / mm", "shift_s_mm":"弧矢平移 / mm",
    }.get(path[-1])


def parameter_origins(origin):
    """Identity of the owner of a value, independent of a list item's storage index."""
    if origin.get("scope") != "layout_prefix":
        return {}
    fixed = origin["position_reference"] == "fixed_z"
    side = {"upstream":" · 上游侧", "downstream":" · 下游侧"}.get(origin.get("boundary_side"), "")
    mounting = "固定在工程坐标" + side if fixed else "跟随定位元件"
    owners = {"observation.mount": ("观察面的定位方式", mounting)}
    for element in origin["element_sources"]:
        source = element["n_path"]
        index = element.get("shared_index") or {}
        if index.get("index_source") == "material_dispersion":
            label = f"按这束光的波长计算材料模型 {index['index_model']}"
        elif not source:
            label = "无独立折射率输入"
        elif source[0] == "system":
            label = "主光路元件的折射率"
        elif source[-1] == "target_n":
            label = "入射支路在接入处指定的折射率"
        elif source[2] == "downstream":
            label = "入射支路为共享段指定的折射率"
        else:
            label = "入射支路自身元件的折射率"
        uid = element["element_uid"]
        owners[f"index-origin/{element['scope']}/{quote(uid, safe='')}"] = (f"元件 {uid} 的折射率来源", label)
    return owners

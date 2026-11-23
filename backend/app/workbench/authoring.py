"""An editable optical layout may precede the information needed to solve it."""
from pydantic import Field, ValidationError

from ..solver_receipt import solver_receipt
from .network import evaluate_network
from .observation import IncidentPath, LayoutSystem, OpticalLayout, analyze_layout, analyze_sections
from .optics import OpticalElement, axis_geometry, axis_metadata, center_profile, describe_index, entrance_width_only, material_index_readings


class EditableElement(OpticalElement):
    allow_incomplete = True
    n: float | None = Field(default=1, ge=1, le=5, json_schema_extra=describe_index,
        description="厚透镜和 GRIN 元件省略折射率时保留未知。普通空间省略时按 n=1；介质折射率待测时填 null。")


class EditableSystem(LayoutSystem):
    require_closed_path = False
    wavelength_nm: float | None = Field(default=None, gt=0, le=1e6)
    elements: list[EditableElement] = Field(max_length=80)

    def for_propagation(self):
        return LayoutSystem.model_validate(self.model_dump())


class GeometricSystem(LayoutSystem):
    wavelength_nm: float | None = Field(default=None, gt=0, le=1e6)


class EditablePath(IncidentPath):
    elements: list[EditableElement] = Field(default_factory=list, max_length=80)


class EditableLayout(OpticalLayout):
    system: EditableSystem
    incident_paths: list[EditablePath] = Field(default_factory=list, max_length=12)


def solver_input(model, target, owner, issues):
    try:
        return target.model_validate(model.model_dump())
    except ValidationError as exc:
        for error in exc.errors(include_url=False, include_input=False):
            location = list(error["loc"])
            if parameter := error.get("ctx", {}).get("parameter"):
                location.append(parameter)
            message = error["msg"].removeprefix("Value error, ")
            if location == ["wavelength_nm"] and model.wavelength_nm is None:
                message = "计算光斑还需要主光路的真空波长。"
            elif location and location[-1] == "n":
                message = "请补充该元件在工作波长处的折射率。"
            element = model.elements[location[1]] if len(location) >= 2 and location[0] == "elements" else None
            issues.append({"owner": owner, "path": location, "element_uid": element.uid if element else None,
                           "message": (f"{element.name} · " if element else "") + message})
        return None


def layout_receipt(layout):
    return solver_receipt("evaluate_optical_layout", "BeamWeft", "1", compute=_analyze_editable, inputs=layout)


def analyze_editable(layout, *, samples_per_segment=None, include_sections=False):
    result = _analyze_editable(layout, samples_per_segment=samples_per_segment, include_sections=include_sections)
    result["profile_sampling_version"] = 2
    owners = [("primary", layout.system), *((p.uid, p) for p in layout.incident_paths)]
    if readings := material_index_readings(owners):
        result["material_indices"] = readings
    result["solver_receipt"] = layout_receipt(layout)
    return result


def _analyze_editable(layout, *, samples_per_segment=None, include_sections=False):
    network = layout.network
    network_wavelength = network and (network.wavelength_nm or layout.system.wavelength_nm)
    if not layout.system.elements and (network is None or network_wavelength is not None):
        # An empty ordered layout contributes no input to an independent port network.
        return analyze_layout(layout, include_sections=include_sections)
    issues = []
    system = solver_input(layout.system, LayoutSystem, "primary", issues) if layout.system.elements else None
    paths = []
    for path in layout.incident_paths:
        ready = solver_input(path, IncidentPath, path.uid, issues)
        if ready is not None:
            paths.append(ready)
    if system is not None:
        if samples_per_segment is not None:
            system = system.model_copy(update={"samples_per_segment":min(samples_per_segment,system.samples_per_segment)})
        model = OpticalLayout(system=system, incident_paths=paths, single_pass_power=layout.single_pass_power,
                              observation_planes=layout.observation_planes, network=layout.network)
        result = analyze_layout(model, include_sections=include_sections, section_layout=layout if issues else None)
        if issues and result.get("status") == "completed":
            result["status"] = "partial"
    else:
        result = {"mode":layout.system.mode if layout.system.elements else "port_network", "wavelength_nm":layout.system.wavelength_nm,
                  "status":"needs_input", "scope":[], "planes":{}}
        if (layout.system.wavelength_nm is None and layout.system.elements
                and all(el.prescription is None for el in layout.system.elements)):
            geometry_issues = []
            geometry = solver_input(layout.system, GeometricSystem, "primary", geometry_issues)
            if geometry is not None:
                for axis in ("tangential","sagittal"):
                    _, plane = axis_geometry(geometry, axis)
                    result["planes"][axis] = {**plane, "samples":[], "elements":[]}
                result["status"] = "partial"
                result["scope"].append("按给定的折射率和光焦度计算近轴几何量；光束尺寸和色散需要工作波长。")
                if geometry.mode != "single_pass":
                    result["stable_both_planes"] = all(item["stable"] for item in result["planes"].values())
            else:
                issues.extend(item for item in geometry_issues if item not in issues)
        if (layout.system.mode == "single_pass" and layout.system.elements and layout.system.wavelength_nm is None
                and (result["planes"] or layout.system.source is not None or layout.system.input_radius_mm is not None)):
            source = layout.system.primary_source()
            for axis in ("tangential", "sagittal"):
                supplied = source.axis(axis)
                plane = result["planes"].setdefault(axis, {})
                plane.update(axis_metadata(supplied, None), **entrance_width_only(supplied, None))
                if supplied.radius_mm is not None:
                    result["status"] = "partial"
            result["effective_wavelength_nm"] = None
        if layout.system.elements:
            if layout.system.mode == "single_pass":
                source = layout.system.primary_source()
            else:
                source = None
            if result["planes"] or (source and source.launch_ray is not None):
                center_axes = {}
                for axis in ("tangential", "sagittal"):
                    plane = result["planes"].get(axis)
                    trace = center_profile(((el, el.n) for el in layout.system.elements), source, axis,
                                           layout.system.wavelength_nm, samples_per_segment or layout.system.samples_per_segment,
                                           confined=layout.system.mode == "single_pass" or bool(plane and plane.get("stable")))
                    if plane is not None:
                        plane.update(trace)
                    else:
                        center_axes[axis] = trace
                if center_axes:
                    result["primary_center_axes"] = center_axes
                    if any(axis["center_profile"] for axis in center_axes.values()):
                        result["status"] = "partial"
        if layout.observation_planes or layout.incident_paths or layout.single_pass_power:
            result["input_issues"] = issues
            sections = analyze_sections(layout, result)
            result["sections"] = sections
            if sections["status"] in {"partial","completed"}:
                result["status"] = "partial"
        if network_wavelength is not None:
            solved = evaluate_network(network, network_wavelength, result.get("sections", {}).get("power_budgets", ()))
            result["network"] = solved
            result["scope"].append(solved["scope"])
            if solved["status"] in {"completed","partial"}:
                result["status"] = "partial"
        elif network is not None:
            issues.append({"owner":"network", "path":["network","wavelength_nm"], "element_uid":None,
                           "message":"请填写网络波长，或沿用已知的主光路波长。"})
    if issues:
        names = {path.uid:path.name for path in layout.incident_paths}
        result["input_issues"] = issues
        for item in issues:
            owner_name = "端口网络" if item["path"][:1] == ["network"] else names.get(item["owner"], "主光路")
            item["summary"] = f"{owner_name} · {item['message']}"
        result["model_warnings"] = [*result.get("model_warnings", []), *(item["summary"] for item in issues)]
    return result

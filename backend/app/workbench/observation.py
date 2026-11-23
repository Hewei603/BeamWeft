"""Registered transverse sections in a shared, unfolded paraxial geometry (mm, vacuum nm)."""
import math
from typing import Literal
from uuid import uuid4

from pydantic import Field, ValidationError, model_validator

from ..physics import beam as B
from .assemblies import Part, validate_parts
from .optics import APERTURE_REFERENCE, OpticalElement, OpticalSystem, StrictInput, _matrix, analyze_system, center_profile, evaluated_devices, model_wavelength_notes, resolve_medium_wavelength, trace_element
from .path_power import PathPower, analyze_path_power
from .network import PortNetwork, evaluate_network
from .shared_media import host_route, resolved_host_route
from .optics import axial_material_path
from .beam_source import (AxisTransferOrigin, BeamSource, SourceAxis, compose_ray_matrices, missing_axis_inputs, missing_source_inputs, ray_matrix_product,
                          ray_component as _sum_ray_terms, source_ray as _source_ray, transfer_ray as _transfer_ray)

AXES = ("tangential", "sagittal")


class HostSegment(StrictInput):
    target_uid: str = Field(min_length=1, max_length=100)
    n: float | None = Field(default=None, ge=1, le=5, description="Index at the incident wavelength.")


class IncidentPath(BeamSource):
    resolve_indices = model_validator(mode="before")(resolve_medium_wavelength)
    uid: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=120)
    target_uid: str = Field(min_length=1, max_length=100, description="Join the host at the entrance of this element on the unfolded axis. The private train moves with this entrance.")
    through_uid: str | None = Field(default=None,min_length=1,max_length=100,exclude_if=lambda v:v is None,
        description="Follow every shared physical element from target_uid through the exit of this host element, inclusive, in forward order. Omit to preserve a legacy explicit path. A shared mirror follows its declared reflection.")
    wavelength_nm: float | None = Field(default=None, gt=0, le=1e6)
    target_n: float | None = Field(default=None, ge=1, le=5, description="Segment index at this incident wavelength.")
    shared_index_wavelength_nm: float | None = Field(default=None, gt=0, le=1e6, exclude_if=lambda v: v is None,
        description="Vacuum wavelength of explicit target_n and downstream n values. Preserve on wavelength edits. Set it to the new wavelength only when those values have been independently specified there.")
    inherit_index_at_same_wavelength: bool = Field(default=True, exclude_if=lambda v: v,
        description="Use the host index for unassigned shared media at equal wavelengths. Set false for independently characterized paths, e.g. unknown polarization in a birefringent crystal.")
    independent_index_uids: list[str] = Field(default_factory=list, max_length=160, exclude_if=lambda v: not v,
        description="Shared media whose branch index must be supplied independently, even at the host wavelength. Other media keep the path's usual inheritance setting.")
    tangential: SourceAxis = Field(default_factory=SourceAxis)
    elements: list[OpticalElement] = Field(default_factory=list, max_length=80, description="Source to entrance in propagation order. Include transmitted faces and media explicitly. An empty train places the source at the entrance.")
    parts: list[Part] = Field(default_factory=list, exclude_if=lambda parts: not parts,
                              description="Physical component bindings and locks on this incident path, indexed into its own elements.")
    source_note: str = Field(default="", max_length=2000)
    downstream: list[HostSegment] = Field(default_factory=list, max_length=79, description="With through_uid, optional refractive indices at the incident wavelength, keyed by shared element after entry. The endpoints determine geometry. Without through_uid this is the legacy consecutive list of homogeneous segments.")
    power_budget: PathPower | None = None

    @model_validator(mode="after")
    def unique_segments(self):
        validate_parts(self.elements, self.parts)
        targets = [self.target_uid, *(s.target_uid for s in self.downstream)]
        element_ids = [el.uid for el in self.elements if el.uid]
        if len(set(targets)) != len(targets) or len(set(element_ids)) != len(element_ids):
            raise ValueError("支路内同一段或元件不能出现两个相同标识。")
        values = self.shared_index_values()
        if values and self.shared_index_wavelength_nm is None and self.wavelength_nm is not None:
            # Preserve omission in fields_set so an older editing client can be reconciled with its source revision.
            self.shared_index_wavelength_nm = self.wavelength_nm
            self.model_fields_set.discard("shared_index_wavelength_nm")
        elif self.shared_index_wavelength_nm is not None and self.shared_index_wavelength_nm != self.wavelength_nm:
            self.expire_shared_indices(values)
        return self

    def shared_index_values(self):
        values = {self.target_uid: self.target_n, **{s.target_uid: s.n for s in self.downstream}}
        return {uid: n for uid, n in values.items() if n is not None}

    def expire_shared_indices(self, uids):
        if self.target_uid in uids:
            self.target_n = None
        self.downstream = [s.model_copy(update={"n": None}) if s.target_uid in uids else s for s in self.downstream]
        self.independent_index_uids = list(dict.fromkeys([*self.independent_index_uids, *uids]))
        self.shared_index_wavelength_nm = self.wavelength_nm if self.shared_index_values() else None


class ObservationPlane(StrictInput):
    position_reference: Literal["element_depth"] = "element_depth"
    uid: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=120)
    target_uid: str = Field(min_length=1, max_length=100)
    depth_mm: float = Field(ge=0, le=1e6, description="Physical depth from entrance of the registered homogeneous element.")


class FixedObservationPlane(StrictInput):
    """Station on the unfolded main path, measured from its entrance."""
    uid: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=120)
    position_reference: Literal["fixed_z"]
    z_mm: float = Field(ge=0, description="Fixed longitudinal coordinate relative to the same main-path origin across edits.")
    boundary_side: Literal["upstream", "downstream"] | None = Field(default=None, description="Side to sample when exactly at a surface.")


class LayoutSystem(OpticalSystem):
    """A project can carry geometry and power data before a beam width is known."""
    require_input_radius = False
    # A single-path analysis may unfold the project's 80 private and 80 shared elements.
    elements: list[OpticalElement] = Field(max_length=160)


class EmptyOrderedPath(LayoutSystem):
    """An unused ordered path provides neither an optical frequency nor a beam."""
    require_closed_path = False
    wavelength_nm: float | None = Field(default=None, gt=0, le=1e6)
    elements: list[OpticalElement] = Field(max_length=0)

    def for_propagation(self):
        raise ValueError("顺序光路尚无元件，无法计算光束传播。")


class OpticalLayout(StrictInput):
    system: LayoutSystem | EmptyOrderedPath
    network: PortNetwork | None = Field(default=None, description="Connected optical ports with optional transverse source fields. A network-only project may have an empty ordered main path.")
    single_pass_power: PathPower | None = None
    incident_paths: list[IncidentPath] = Field(default_factory=list, max_length=12)
    observation_planes: list[ObservationPlane | FixedObservationPlane] = Field(default_factory=list, max_length=48)

    @model_validator(mode="after")
    def identities(self):
        if not self.system.elements and (self.single_pass_power or self.incident_paths or self.observation_planes):
            raise ValueError("主路截面、支路和顺序功率预算需要主路元件。请先加入对应的元件。")
        uids = [e.uid for e in self.system.elements if e.uid]
        if len(set(uids)) != len(uids):
            raise ValueError("主光路元件标识重复，无法唯一定位观察面。")
        for items in (self.incident_paths, self.observation_planes):
            if len(set(item.uid for item in items)) != len(items) or any(item.uid == "primary" for item in items):
                raise ValueError("路径与观察面的标识需在各自列表内唯一，primary 为主光路保留标识。")
        # An absolute station can be used with an anonymous train. Materialize the
        # missing element identities once, just as a newly saved project would.
        if any(isinstance(p, FixedObservationPlane) for p in self.observation_planes) and any(not e.uid for e in self.system.elements):
            elements = [e if e.uid else e.model_copy(update={"uid": str(uuid4())}) for e in self.system.elements]
            self.system = self.system.model_copy(update={"elements": elements})
        return self


def analyze_layout(layout, *, display_system=None, include_sections=False, section_layout=None):
    system = display_system or layout.system
    if system.elements:
        result = analyze_system(system, stamp=False)
        survey = section_layout or layout
        if survey.incident_paths or survey.observation_planes or survey.single_pass_power or include_sections:
            result["sections"] = analyze_sections(survey, result)
    elif layout.network is not None:
        result = {"mode": "port_network", "engine": "BeamWeft complex scattering", "planes": {},
                  "scope": [], "wavelength_nm": layout.network.wavelength_nm or system.wavelength_nm}
    else:
        result = {"mode":system.mode, "status":"needs_input", "planes":{}, "scope":[],
                  "wavelength_nm":system.wavelength_nm, "model_warnings":["工程尚无元件。可放入第一个元件，或插入已保存的组件模型。"]}
    if layout.network is not None:
        network = evaluate_network(layout.network, system.wavelength_nm,
                                   result.get("sections", {}).get("power_budgets", ()))
        result["network"] = network
        result["scope"].append(network["scope"])
        if not system.elements:
            result["status"] = network["status"]
        elif network["status"] != "completed":
            result["status"] = "partial"
    return result


class PowerAnalysis(StrictInput):
    revision_id: int | None = Field(default=None, gt=0, description="Use this saved revision, or omit to use the current revision.")
    layout: OpticalLayout | None = Field(default=None, description="An unsaved layout to analyze instead of a saved revision.")

    @model_validator(mode="after")
    def single_source(self):
        if self.revision_id is not None and self.layout is not None:
            raise ValueError("请选择一个已存版本或一份待计算草稿。")
        return self


def _reduced_q(point):
    return complex(point["q_real_mm"], point["q_imag_mm"]) / point["n"]


def incident_system(path, source, system_type=OpticalSystem):
    return system_type(mode="single_pass", wavelength_nm=path.wavelength_nm, source=BeamSource(tangential=source),
                       elements=path.elements, samples_per_segment=16)


def _source_result(path, source, axes, start):
    missing = missing_source_inputs(path, source)
    if missing:
        result = {"message": "需补充" + "、".join(missing)}
        if source.radius_mm is not None:
            result["profile"] = [[start, source.radius_mm]]
        return {axis: result.copy() for axis in axes}
    curvature = source.curvature_mm if source.wavefront == "curved" else None
    effective = path.wavelength_nm * source.m2
    length = sum(element.length_mm for element in path.elements)
    if length:
        system = incident_system(path, source)
        planes = analyze_system(system, stamp=False)["planes"]
        results = {}
        for axis in axes:
            plane = planes[axis]
            data = {"profile": [[start + p["z_mm"], p["radius_mm"]] for p in plane["samples"] if "radius_mm" in p]}
            if "q_real_mm" in plane.get("output", {}):
                data.update(q=_reduced_q(plane["output"]), effective=effective, exit_radii_mm=[p["radius_mm"] for p in plane["elements"]])
            else:
                data["message"] = plane["message"]
            if "profile_breaks" in plane:
                data["profile_breaks"] = plane["profile_breaks"]
            if "profile_omissions" in plane:
                data["profile_omissions"] = [{**item, "start_mm":start+item["start_mm"], "end_mm":start+item["end_mm"],
                                              "first_min_z_mm":start+item["first_min_z_mm"]}
                                             for item in plane["profile_omissions"]]
            results[axis] = data
        return results
    results = {}
    for axis in axes:
        q = B.q_from_beam(source.radius_mm, curvature, effective)
        exits = []
        for element in path.elements:
            q = B.propagate_q(_matrix(element, axis, path.wavelength_nm), q)
            exits.append(B.w_from_q(q, effective))
        results[axis] = {"q": q, "effective": effective, "exit_radii_mm": exits,
                         "profile": [[start, r] for r in [source.radius_mm, *exits]]}
    return results


def _radius(q, depth, n, wavelength):
    value = float(B.w_from_q(q * n + depth, wavelength, n))
    if not math.isfinite(value):
        raise ValueError("当前参数超出近轴模型可计算范围。")
    return value


def _reduced_vergence(q):
    if not q:
        return None
    value = (1 / q).real
    return value if math.isfinite(value) else None


def host_positions(system):
    positions, z = {}, 0.
    for index, element in enumerate(system.elements):
        if element.uid:
            positions[element.uid] = (index, element, z)
        z += element.length_mm
    return positions


def detector_segment(anchors, source_uid, reference, position, side=None):
    if reference == "element_depth":
        if source_uid not in anchors:
            raise ValueError("原定位元件在这个模型中已移除。")
        _, el, start = anchors[source_uid]
        if el.kind != "space":
            raise ValueError("定位元件不是均匀传播段，请重新选择。")
        if position < 0 or position > el.length_mm + 1e-9:
            raise ValueError("指定深度位于目标均匀段之外。")
        return el, start, min(position, el.length_mm)
    candidates = [
        (el, start)
        for _, el, start in anchors.values()
        if el.kind == "space" and el.length_mm > 0 and start - 1e-9 <= position <= start + el.length_mm + 1e-9
    ]
    if not candidates:
        raise ValueError("探测器坐标超出已建模的均匀传播段，或位于厚元件内部。")
    if side is not None:
        # At a face the upstream sample is just before it, downstream just after it.
        if side == "upstream":
            candidates = [(el, z) for el, z in candidates if z < position - 1e-9]
        else:
            candidates = [(el, z) for el, z in candidates if z + el.length_mm > position + 1e-9]
        if not candidates:
            raise ValueError("所选界面一侧没有可取样的均匀传播段。")
    if len(candidates) > 1:
        matches = [(el, start) for el, start in candidates if el.uid == source_uid]
        if len(matches) != 1:
            raise ValueError("探测器位于两个元件的边界，无法确认对应侧。请补充表面位置或元件定位依据。")
        candidates = matches
    el, start = candidates[0]
    return el, start, max(0.0, min(el.length_mm, position - start))


def locate_planes(layout, anchors):
    """Return a resolved segment/depth or a local explanation for each registered plane."""
    located = {}
    for plane in layout.observation_planes:
        try:
            if isinstance(plane, FixedObservationPlane):
                el, _, depth = detector_segment(anchors, None, "fixed_z", plane.z_mm, plane.boundary_side)
            else:
                el, _, depth = detector_segment(anchors, plane.target_uid, "element_depth", plane.depth_mm)
        except ValueError as exc:
            located[plane.uid] = (None, str(exc))
            continue
        local = ObservationPlane(uid=plane.uid, name=plane.name, target_uid=el.uid, depth_mm=depth)
        located[plane.uid] = (local, None)
    return located


def _incident_power(path,route,issue,planes):
    return analyze_path_power(path.power_budget,path_uid=path.uid,name=path.name,wavelength_nm=path.wavelength_nm,
                              arm=path.elements,route=route,route_issue=issue,planes=planes)


def _primary_power(layout, planes):
    if layout.single_pass_power is None:
        return []
    system = layout.system
    if system.mode != "single_pass":
        return [{"path_uid":"primary","name":"主光路","rows":[],"planes":[],"status":"needs_input",
                 "message":"主光路目前为谐振腔。单程预算不用于计算腔内循环功率。"}]
    z,route = 0.,[]
    for el in system.elements:
        route.append((el,z,el.n))
        z += el.length_mm
    return [analyze_path_power(layout.single_pass_power,path_uid="primary",name="主光路",wavelength_nm=system.wavelength_nm,
                               arm=[],route=route,route_issue=None,planes=planes)]


def power_for_layout(layout, located=None):
    anchors = host_positions(layout.system)
    if located is None:
        located = locate_planes(layout, anchors)
    planes = [p for p, _ in located.values() if p is not None]
    budgets = _primary_power(layout, planes)
    for path in layout.incident_paths:
        if path.power_budget is not None:
            route, issue = host_route(path, anchors, layout.system.wavelength_nm)
            budgets.append(_incident_power(path,route,issue,planes))
    return budgets


def _source_axes(path, start):
    groups = [(path.tangential, AXES)] if path.sagittal is None else [
        (path.tangential, ("tangential",)), (path.sagittal, ("sagittal",))]
    axes = {}
    for source, directions in groups:
        try:
            values = _source_result(path, source, directions, start)
            for axis, data in values.items():
                q = data.get("q")
                valid_q = q is None or (q.imag > 0 and math.isfinite(q.real) and math.isfinite(q.imag))
                valid_trace = all(math.isfinite(z) and math.isfinite(r) and r > 0 for z, r in data.get("profile", []))
                valid_exits = all(math.isfinite(r) and r > 0 for r in data.get("exit_radii_mm", []))
                axes[axis] = data if valid_q and valid_trace and valid_exits else {"message": "该方向的计算超出数值范围，请核对光源与元件参数。"}
        except (ArithmeticError, ValueError):
            axes.update({axis: {"message": "光源或元件参数超出近轴计算范围，请核对输入半径、曲率及其单位。"} for axis in directions})
    return axes


def _partial_axis(axis, wavelength_nm, ray):
    width_constraint = (axis.radius_mm is not None or axis.wavefront != "unknown") and missing_axis_inputs(wavelength_nm, axis)
    return bool(width_constraint or (ray[0] is None) != (ray[1] is None))


def _at_station(transfer, depth, n):
    if transfer is None or (depth and n is None):
        return None
    return compose_ray_matrices(B.slab(depth, n), transfer) if depth else transfer


def _ray_at_depth(x, p, depth, n):
    if not depth or p == 0:
        return x
    return _sum_ray_terms(1,x,depth/n,p) if n else None


def _trace_path(path, route, issue):
    trace = {"uid": path.uid, "status":"needs_input"}
    if path.through_uid:
        trace["transfer_scope"] = "沿共享元件的展开方向传播。固定理想参数沿用工程值，带波长模型的器件按入射波长计算。"
    if issue:
        trace["message"] = issue
    if not route:
        return trace, {}
    entry = route[0][1]
    start = entry - sum(element.length_mm for element in path.elements)
    geometry = [(el, el.n) for el in path.elements] + [(el, n) for el, _, n in route]
    host_elements = {el.uid:el for el, _, _ in route}
    centers, entrances = {}, {}
    for axis in AXES:
        keep_transfer = path.launch_ray is not None or _partial_axis(path.axis(axis), path.wavelength_nm, (0., 0.))
        def at_entrance(el, ray, transfer, linked):
            if host_elements.get(el.uid) is el:
                x, p = _transfer_ray(ray, transfer)
                entrances.setdefault(el.uid, {})[axis] = {"center_mm":x, "reduced_angle":p,
                    "transfer":transfer if keep_transfer and linked else None}
        centers[axis] = center_profile(geometry, path, axis, path.wavelength_nm, 17, start=start, at_entrance=at_entrance)
    trace["axes"] = centers
    known_center = path.launch_ray is not None and any(c["center_profile"] for c in centers.values())
    if known_center:
        trace["status"] = "partial"
    def partial_locations(message):
        return {uid:{axis:{**value,"message":message} for axis,value in location.items()}
                for uid,location in entrances.items() if all(axis in location for axis in AXES)}
    if any(el.allow_incomplete for el in path.elements):
        try:
            path = IncidentPath.model_validate(path.model_dump())
        except ValidationError as exc:
            trace.update(source_z_mm=start, entrance_z_mm=entry,
                         message="、".join(e["msg"].removeprefix("Value error, ") for e in exc.errors()))
            return trace, partial_locations(trace["message"])
    if path.wavelength_nm is not None:
        try:
            records = evaluated_devices(path.elements, path.wavelength_nm)
        except ValueError as exc:
            trace.update(message=str(exc), source_z_mm=start, entrance_z_mm=entry)
            return trace, partial_locations(trace["message"])
        if records:
            trace["evaluated_devices"] = records
    axes = _source_axes(path, start)
    locations = {}
    for element,z,n in route:
        length = element.length_mm
        locations[element.uid] = {}
        medium_unknown = n is None and length > 0
        optic = element.model_copy(update={"n": n}) if n is not None and n != element.n else element
        try:
            if optic.allow_incomplete and not medium_unknown:
                optic = OpticalElement.model_validate(optic.model_dump())
            if optic.prescription:
                device, = evaluated_devices([optic], path.wavelength_nm)
                trace.setdefault("evaluated_devices", []).append({**device,"scope":"host","element_uid":optic.uid})
        except (ValueError, ArithmeticError) as exc:
            if isinstance(exc, ValidationError):
                exc = exc.errors()[0]["msg"].removeprefix("Value error, ")
            trace["message"] = f"「{optic.name}」的共享传输模型不可用。{exc}"
            del locations[element.uid]
            break
        for axis, data in axes.items():
            location = {k:v for k,v in data.items() if k in {"q","effective","message"}}
            location.update(entrances.get(element.uid, {}).get(axis, {"center_mm":None, "reduced_angle":None, "transfer":None}))
            locations[element.uid][axis] = location
            if medium_unknown:
                data.setdefault("message",f"需补充上游段“{element.name}”在入射波长下的折射率。")
                data.pop("q",None)
                continue
            if "message" in data:
                continue
            try:
                q, exit_point, points, _, breaks = trace_element(
                    optic,axis,data["q"],z,path.wavelength_nm,data["effective"],17,
                    profile_omissions=data.setdefault("profile_omissions", []))
                if breaks:
                    data.setdefault("profile_breaks", []).extend(len(data["profile"])+i for i in breaks)
                data["profile"].extend([point["z_mm"], point["radius_mm"]] for point in points or [exit_point])
                data["q"] = q
            except (ArithmeticError,ValueError):
                location["message"] = data["message"] = "本段传播超出数值范围，请核对参数。"
                data.pop("q",None)
    reached_end = not trace.get("message") and all("q" in data and not data.get("message") for data in axes.values())
    has_field = any(axis.get("q") is not None for location in locations.values() for axis in location.values())
    trace.update(status="completed" if reached_end else "partial" if has_field or known_center else "needs_input",
                 source_z_mm=start, entrance_z_mm=entry, axes={
        axis: {**centers[axis], **{k: data[k] for k in ("profile", "profile_breaks", "message") if k in data},
               **({"profile_omissions":data["profile_omissions"]} if data.get("profile_omissions") else {}),
               "exit_radii_mm": data.get("exit_radii_mm")}
        for axis, data in axes.items()
    })
    return trace, locations


def retains_source_width(elements, axis, wavelength_nm=None):
    """At the source plane, phase-only surfaces preserve the supplied width."""
    if any(el.length_mm for el in elements):
        return False
    if not any(el.kind in {"matrix","registered"} for el in elements):
        return True
    if wavelength_nm is None and any(el.prescription for el in elements):
        return False
    try:
        matrix = ray_matrix_product(_matrix(el, axis, wavelength_nm) for el in elements)
    except (ValueError, ArithmeticError):
        # The draft may contain an uncharacterized transfer at the same z.
        return False
    return matrix[0,1] == 0 and math.isclose(abs(matrix[0,0]), 1., rel_tol=1e-14)


def _apply_station_transfer(beam, axis, source, launch_ray, transfer):
    """Preserve constraints that can reappear at a conjugate plane even when q is unknown."""
    if transfer is None:
        return
    ray = _source_ray(launch_ray, axis)
    state = beam["field_axes"][axis]
    center, direction = _transfer_ray(ray, transfer)
    state.update(center_mm=center, reduced_angle=direction)
    wl = beam["wavelength_nm"]
    if wl is None or not _partial_axis(source, wl, ray):
        return
    try:
        origin = AxisTransferOrigin(axis=source, offset_mm=ray[0], angle_mrad=None if ray[1] is None else 1000 * ray[1],
                                    matrix=transfer.tolist())
        local, _ = origin.at_plane(wl)
    except (ValueError, ArithmeticError):
        state["transfer_warning"] = "传输矩阵的数值精度不足，此轴无法保留跨段传播约束。"
        return
    if local.radius_mm is not None:
        if axis not in beam["radii_mm"]:
            beam["radius_origin"][axis] = "conjugate"
        beam["radii_mm"][axis] = local.radius_mm
        beam["notes"].pop(axis, None)
    if local.wavefront != "unknown":
        state["reduced_vergence_per_mm"] = 1 / local.curvature_mm if local.curvature_mm else 0.
    if beam["index"] is not None and beam["index"] > 0 and origin.retains_hidden_constraints(local, (center,direction), wl):
        state["transfer_origin"] = origin.model_dump()


def _incident_at_plane(path, location, plane, n, issue, include_q=False):
    beam = {"uid":path.uid,"wavelength_nm":path.wavelength_nm,"radii_mm":{},"notes":{},"width_basis":{},"radius_origin":{},
            "index":n,"field_axes":{},"launch_basis":"specified" if path.launch_ray else "ideal_on_axis"}
    for axis in AXES:
        source = (path.sagittal or path.tangential) if axis == "sagittal" else path.tangential
        beam["width_basis"][axis] = source.width_basis()
        if location is not None:
            entry = location[axis]
            p = entry["reduced_angle"]
            state = {"center_mm":_ray_at_depth(entry["center_mm"],p,plane.depth_mm,n),"reduced_angle":p}
            if entry.get("q") is not None and (n or plane.depth_mm == 0):
                local_q = entry["q"] + (plane.depth_mm/n if n else 0.)
                state["reduced_vergence_per_mm"] = _reduced_vergence(local_q)
                if include_q and n:
                    state["q_mm"] = [n * local_q.real, n * local_q.imag]
            beam["field_axes"][axis] = state
        if location is None:
            beam["notes"][axis] = issue or "此面没有连通的入射路径。"
        elif (plane.target_uid == path.target_uid and plane.depth_mm == 0 and source.radius_mm is not None
              and retains_source_width(path.elements, axis, path.wavelength_nm)):
            beam["radii_mm"][axis] = source.radius_mm
            beam["radius_origin"][axis] = "source_input"
        elif "message" in location[axis]:
            beam["notes"][axis] = location[axis]["message"]
        elif n is None and plane.depth_mm:
            beam["notes"][axis] = "需补充介质在入射波长下的折射率。"
        else:
            beam["radii_mm"][axis] = _radius(location[axis]["q"],plane.depth_mm,n or 1,location[axis]["effective"])
        if location is not None:
            _apply_station_transfer(beam, axis, source, path.launch_ray, _at_station(entry.get("transfer"), plane.depth_mm, n))
    return beam


def _primary_at_stations(system, primary, planes, anchors):
    """An incomplete downstream device cannot affect an upstream single-pass field."""
    if (system.mode != "single_pass" or not planes or not system.wavelength_nm
            or not (system.input_radius_mm or system.source) or primary["planes"]):
        return primary, len(system.elements)
    last = max(anchors[plane.target_uid][0] for plane in planes)
    ready = []
    for element in system.elements[:last+1]:
        try:
            ready.append(OpticalElement.model_validate(element.model_dump()))
        except ValidationError:
            break
    if not ready or not any(anchors[p.target_uid][0] < len(ready) for p in planes):
        return primary, 0
    prefix = LayoutSystem.model_validate({**system.model_dump(exclude={"elements"}), "elements":ready})
    return analyze_system(prefix, profile=False, stamp=False), len(ready)


def _primary_station_rays(system, planes, source):
    """Evaluate the chief ray independently of q. An unmeasured angle need not hide a conjugate-plane center."""
    stations = {}
    for plane in planes:
        stations.setdefault(plane.target_uid, []).append(plane)
    rays, matrices = {}, {}
    last = max(i for i, el in enumerate(system.elements) if el.uid in stations)
    for axis in AXES:
        def at_entrance(el, ray, transfer, linked):
            for plane in stations.get(el.uid, ()):
                station = _at_station(transfer, plane.depth_mm, el.n)
                if station is None:
                    x, p = _transfer_ray(ray, transfer)
                    local = _ray_at_depth(x, p, plane.depth_mm, el.n), p
                else:
                    local = _transfer_ray(ray, station)
                    if linked:
                        matrices[(plane.uid, axis)] = station
                rays[(plane.uid, axis)] = local
        center_profile(((el, el.n) for el in system.elements[:last+1]), source, axis, system.wavelength_nm,
                       0, at_entrance=at_entrance)
    return rays, matrices


def analyze_sections(layout: OpticalLayout, primary, *, include_q=False):
    anchors = host_positions(layout.system)
    located = locate_planes(layout, anchors)
    planes = [p for p, _ in located.values() if p is not None]
    main_source = layout.system.primary_source() if layout.system.mode == "single_pass" else None
    has_ray = main_source is not None and main_source.launch_ray is not None
    declared = layout.system.source is not None or layout.system.input_radius_mm is not None or layout.system.input_curvature_mm is not None
    need_transfer = main_source and declared and (has_ray or any(_partial_axis(main_source.axis(a), layout.system.wavelength_nm, (0., 0.)) for a in AXES))
    station_rays, matrices = _primary_station_rays(layout.system, planes, main_source) if need_transfer and planes else ({}, {})
    station_model, primary_count = _primary_at_stations(layout.system, primary, planes, anchors)
    incident, traces, budgets = {}, [], _primary_power(layout, planes)
    model_warnings = []
    main_pupils = [i for i, element in enumerate(layout.system.elements) if element.clear_aperture]
    clipped_segments = {}
    for path in layout.incident_paths:
        resolved, issue = resolved_host_route(path, anchors, layout.system.wavelength_nm)
        route = [(el, z, index["n"]) for el, z, index in resolved]
        trace, locations = _trace_path(path, route, issue)
        trace["shared_route"] = [{"element_uid": el.uid, "z_mm": z, "wavelength_nm": path.wavelength_nm,
                                  **index, **axial_material_path(el, index["n"])}
                                 for el, z, index in resolved]
        if issue:
            model_warnings.append(f"入射支路“{path.name}” · {issue}")
        clipped = any(el.clear_aperture for el in path.elements)
        affected = set()
        for el, _, _ in route:
            clipped = clipped or bool(el.clear_aperture)
            if clipped:
                affected.add(el.uid)
        clipped_segments[path.uid] = affected
        if affected:
            model_warnings.append(f"{path.name} · {APERTURE_REFERENCE}")
        model_warnings.extend(f"{path.name} · {note}" for note in model_wavelength_notes(
            [*path.elements, *(e for e,_,_ in route)], path.wavelength_nm))
        for record in trace.get("evaluated_devices", []):
            model_warnings.extend(f"{path.name} · {note}" for note in record.get("warnings", []))
        traces.append(trace)
        connected = {el.uid:n for el,_,n in route}
        declared = (connected if path.through_uid else
                    {path.target_uid:path.target_n, **{s.target_uid:s.n for s in path.downstream}})
        incident[path.uid] = (declared, connected, locations, trace.get("message"))
        if path.power_budget is not None:
            budgets.append(_incident_power(path,route,issue,planes))
    sections = []
    for requested in layout.observation_planes:
        plane, issue = located[requested.uid]
        fixed = isinstance(requested, FixedObservationPlane)
        if issue:
            location = ({"position_reference":"fixed_z", "requested_z_mm":requested.z_mm} if fixed
                        else {"target_uid":requested.target_uid, "depth_mm":requested.depth_mm})
            sections.append({"uid":requested.uid, **location, "message":issue, "beams":[]})
            continue
        section = {"uid":plane.uid,"target_uid":plane.target_uid,"depth_mm":plane.depth_mm,"beams":[]}
        if fixed:
            section.update(position_reference="fixed_z", requested_z_mm=requested.z_mm)
        sections.append(section)
        idx, medium, start = anchors[plane.target_uid]
        section["z_mm"] = start + plane.depth_mm
        if fixed:
            section["element_index"] = idx
        row = {"uid":"primary","wavelength_nm":layout.system.wavelength_nm,"radii_mm":{},"notes":{},
               "width_basis":{}, "radius_origin":{}, "index":medium.n, "field_axes":{},
               "launch_basis":"specified" if has_ray else "reference_axis"}
        for axis in AXES:
            axis_source = main_source.axis(axis) if main_source else None
            row["width_basis"][axis] = axis_source.width_basis() if axis_source else "gaussian"
            result = station_model["planes"].get(axis,{}) if idx < primary_count else {}
            x, p = station_rays.get((plane.uid, axis), (None, None)) if has_ray else (0., 0.)
            state = {"center_mm":x, "reduced_angle":p}
            row["field_axes"][axis] = state
            source_width = (axis_source is not None and axis_source.radius_mm is not None and section["z_mm"] == 0
                            and retains_source_width(layout.system.elements[:idx], axis, layout.system.wavelength_nm))
            if source_width:
                row["radius_origin"][axis] = "source_input"
                row["radii_mm"][axis] = axis_source.radius_mm
            if not result.get("input"):
                if source_width:
                    continue
                if not result.get("message"):
                    relevant = [issue["message"] for issue in primary.get("input_issues", ())
                                if issue["owner"] == "primary" and (layout.system.mode != "single_pass"
                                or issue["path"][:1] != ["elements"] or len(issue["path"])<2
                                or issue["path"][1] <= idx)]
                    row["notes"][axis] = " ".join(dict.fromkeys(relevant)) or "主光路尚无该方向的光斑预测。"
                else:
                    row["notes"][axis] = result["message"] if layout.system.mode == "single_pass" else "该平面没有受限本征模。"
                continue
            point = result["elements"][idx-1] if idx else result["input"]
            q = _reduced_q(point)
            effective_wavelength = result.get("effective_wavelength_nm", station_model.get("effective_wavelength_nm"))
            row["radii_mm"][axis] = _radius(q,plane.depth_mm,medium.n,effective_wavelength)
            state["reduced_vergence_per_mm"] = _reduced_vergence(q+plane.depth_mm/medium.n)
            if include_q:
                row["field_axes"][axis]["q_mm"] = [medium.n * q.real + plane.depth_mm, medium.n * q.imag]
        if main_source:
            for axis in AXES:
                _apply_station_transfer(row, axis, main_source.axis(axis), main_source.launch_ray, matrices.get((plane.uid, axis)))
        section["beams"].append(row)
        for path in layout.incident_paths:
            indices, connected, locations, issue = incident[path.uid]
            if plane.target_uid in indices:
                beam = _incident_at_plane(path,locations.get(plane.target_uid),plane,indices[plane.target_uid],issue,include_q)
                beam["registration_available"] = plane.target_uid in connected
                section["beams"].append(beam)
        for beam in section["beams"]:
            clipped = (bool(main_pupils) and (layout.system.mode != "single_pass" or idx >= main_pupils[0])
                       if beam["uid"] == "primary" else plane.target_uid in clipped_segments[beam["uid"]])
            if clipped:
                message = (
                    "此腔模经过有限孔径。该面的自洽场需要包含孔径与衍射的往返模型。"
                    if beam["uid"] == "primary" and layout.system.mode != "single_pass" else
                    "此光束经过有限孔径。已知相干场时可计算衍射分布；已知非相干光线的位置与方向时可计算几何截光分布。"
                )
                beam.update(radii_mm={}, field_axes={}, radius_origin={}, width_basis=dict.fromkeys(AXES,"clipped"),
                            notes=dict.fromkeys(AXES,message))
    rows = [beam for section in sections for beam in section["beams"]]
    any_result = any(beam["radii_mm"] or (beam.get("launch_basis") == "specified" and
                     any(state.get("center_mm") is not None for state in beam["field_axes"].values())) for beam in rows)
    any_result = any_result or any(b.get("rows") for b in budgets)
    if not sections:
        any_result = any_result or any(t["status"] != "needs_input" for t in traces)
    partial = (any(beam["notes"] for beam in rows) or any("message" in s for s in sections)
               or any(t["status"] != "completed" for t in traces) or any(b["status"]!="completed" for b in budgets))
    status = "needs_input" if not any_result else "partial" if partial else "completed"
    return {"status":status,"paths":traces,"planes":sections,"power_budgets":budgets,
            **({"model_warnings": model_warnings} if model_warnings else {})}

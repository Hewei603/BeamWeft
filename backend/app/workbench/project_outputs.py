"""Typed values from an immutable engineering result. Never run a solver to resolve history."""
from urllib.parse import quote

from pydantic import Field

from ..study_api import Condition, Port, PublishedPort, StrictInput
from .beam_source import MEDIUM_BEAM, PARAXIAL_BEAM, medium_beam_output
from .projects import read_project
from .station_outputs import station_beams
from .centroid_state import STATION_CENTROID, station_centers
from .layout_prefix import saved_prefixes


ABSORBED = Port(quantity="optics.absorbed-power", label="体吸收功率", unit="W", scale=1, shape="interval")
FORWARD = Port(quantity="optics.power", label="沿光路传播的功率", unit="W", scale=1, shape="interval")
OUTGOING_POWER = FORWARD.model_copy(update={"shape": "scalar"})
LENGTH = Port(quantity="geometry.length", label="物理长度", unit="mm", scale=.001)
WAVELENGTH = Port(quantity="optics.vacuum-wavelength", label="真空波长", unit="nm", scale=1e-9)
LAYOUT = Port(quantity="optics.ordered-layout.v1", label="工程光路模型", unit="unfolded-mm.v1", scale=1,
              shape="state", registration_label="本次计算使用的工程与参考系")
VACUUM_NM = "optics.vacuum-wavelength.nm"
MODEL_REVISION = "laserclaw.project-revision"
PUMP_SITE = "optics.pump-sampling-location"
REVISION_CONDITION = Condition(path="revision", label="工程版本", value_type="text")
SITE_CONDITION = Condition(path="site", label="泵浦支路与样品段", value_type="text")


def sampling_site(path, scope, element):
    return "/".join(quote(str(value), safe="") for value in (path, scope, element))


def at_wavelength(port, path="wavelength_nm"):
    return port.model_copy(update={"conditions": {VACUUM_NM: Condition(path=path, label="真空波长 / nm")}})


def project_scope_warnings(project):
    warnings = (project.get("component_context") or {}).get("scope_warnings", [])
    if isinstance(warnings, str):
        warnings = [warnings]
    return [note for note in warnings if isinstance(note, str) and note.strip()] if isinstance(warnings, list) else []


class ProjectReference(StrictInput):
    revision_id: int = Field(gt=0)
    port: str = Field(min_length=1, max_length=2600)
    registration: str = Field(default="", max_length=500)
    registration_from: str | None = Field(default=None, min_length=1, max_length=100)


class ProjectOutputQuery(StrictInput):
    revision_id: int | None = Field(default=None, gt=0)
    quantity: str | None = Field(default=None, max_length=160)
    path_uid: str | None = Field(default=None,max_length=100)
    element_uid: str | None = Field(default=None,max_length=100)
    plane_uid: str | None = Field(default=None,max_length=100)
    port: str | None = Field(default=None, min_length=1, max_length=2600,
        description="Return the frozen value of this exact published port from the selected revision.")
    offset: int = Field(default=0,ge=0)
    limit: int = Field(default=30,ge=1,le=100)


def project_outputs(db, case_id, revision_id, quantity=None, *, compact=False, only_port=None):
    return published_project_outputs(read_project(db, case_id, revision_id), quantity, compact=compact, only_port=only_port)


def published_project_outputs(row, quantity=None, *, compact=False, only_port=None):
    if not row:
        return {"revision_id":None,"outputs":{}}
    project = row["project"]
    values = {}

    def publish(keys, port, value, where, *, namespace="v1", key=None, **details):
        if quantity and quantity != port.quantity:
            return
        key = key or namespace + "/" + "/".join(quote(str(k),safe="") for k in keys)
        if only_port is not None and only_port != key and only_port != key + "/value":
            return
        offer = PublishedPort.describe(port, value, details)
        if offer is not None:
            brief = {k:details[k] for k in ("path_uid","scope","element_uid","terminal","plane_uid","z_mm","depth_mm",
                     "source_z_mm","wavelength_nm","medium_n","position_reference") if k in details} if compact else details
            values[key] = {"port":offer.model_dump(mode="json"),"location":where,**brief}
            if not compact or port.shape != "state":
                values[key]["value"] = value

    if only_port in (None, "v1/layout") and quantity in (None, LAYOUT.quantity) and not row.get("analysis", {}).get("input_issues"):
        # This is the frozen scientific layout only. Stock, assumptions and notes stay in the referenced revision.
        from .observation import OpticalLayout
        layout = {k: project[k] for k in OpticalLayout.model_fields if k in project}
        warnings = project_scope_warnings(project)
        publish(("layout",), LAYOUT.model_copy(update={"conditions":{MODEL_REVISION: REVISION_CONDITION}}),
                layout, project["title"], scope="layout", revision=f"#{row['revision_id']}",
                **({"scope_warnings":warnings} if warnings else {}))

    if quantity in (None, LAYOUT.quantity) and (only_port is None or only_port.startswith("layout-prefix-v1/")):
        port = LAYOUT.model_copy(update={"label":"从光源到观察面的单程光路","conditions":{
            MODEL_REVISION:REVISION_CONDITION.model_copy(update={"reference_only":True}),
            VACUUM_NM:Condition(path="wavelength_nm",label="真空波长 / nm"),
            "optics.path-source-z.mm":Condition(path="source_z_mm",label="光源在原工程的 z / mm"),
            "optics.path-end-z.mm":Condition(path="z_mm",label="观察面在原工程的 z / mm")}})
        for key, layout, details in saved_prefixes(project,row.get("analysis",{}),only_port):
            publish((),port,layout,details.pop("location"),key=key,**details,revision=f"#{row['revision_id']}")

    for path_id, path_name, path in [("primary","主光路",project["system"]), *(
            (p["uid"],p["name"],p) for p in project.get("incident_paths",[]))]:
        if path.get("wavelength_nm") is not None and (path_id != "primary" or path["elements"]):
            publish((path_id,"wavelength"),WAVELENGTH,path["wavelength_nm"],path_name,path_uid=path_id)
        for el in path["elements"]:
            if el.get("uid") and el.get("length_mm",0)>0:
                publish((path_id,"element",el["uid"],"length"),LENGTH,el["length_mm"],f"{path_name} · {el['name']}",
                        path_uid=path_id,element_uid=el["uid"])
    budgets = (row.get("analysis",{}).get("sections") or {}).get("power_budgets",[])
    network = row.get("analysis", {}).get("network") or {}
    if network.get("wavelength_nm") is not None:
        publish(("wavelength",), WAVELENGTH, network["wavelength_nm"], "端口光路", namespace="network-v1", scope="network")
    names = {c["uid"]: c["name"] for c in (project.get("network") or {}).get("components", [])}
    for terminal in network.get("ports", []):
        uid, name = terminal["component"], terminal["port"]
        publish(("component", uid, name, "outgoing"), at_wavelength(OUTGOING_POWER),
                terminal["outgoing_w"], f"{names.get(uid, uid)} · 端口 {name} · 单模匹配参考出射", namespace="network-v1", element_uid=uid, scope="network",
                terminal=name, wavelength_nm=network["wavelength_nm"], basis=(project.get("network") or {}).get("basis", ""),
                origins=network.get("origins", []))
    for budget in budgets:
        upstream = []
        for stage in budget.get("rows",[]):
            where = f"{budget['name']} · {stage['name']} · {budget['wavelength_nm']:g} nm"
            prefix = (budget["path_uid"],stage["scope"],stage["uid"])
            upstream.extend(f"{stage['name']} · {m}" for m in stage.get("missing",[]))
            for key, port in (("outgoing",FORWARD),("bulk_absorption",ABSORBED)):
                watts = (stage.get(key) or {}).get("watts")
                if watts is None:
                    continue
                details = {"wavelength_nm":budget["wavelength_nm"],"scope":stage["scope"],"element_uid":stage["uid"],
                           "path_uid":budget["path_uid"],"stage_law":stage.get("law"),"source_power":budget.get("input_spec"),
                           "incomplete":list(upstream)}
                conditioned = at_wavelength(port)
                conditioned.conditions.update({MODEL_REVISION: REVISION_CONDITION, PUMP_SITE: SITE_CONDITION})
                publish((*prefix,key),conditioned,watts,where,**details,revision=f"#{row['revision_id']}",site=sampling_site(*prefix))
    analysis = row.get("analysis", {})
    if quantity in (None, STATION_CENTROID.quantity) and (only_port is None or only_port.startswith("station-center-v1/")):
        for key, state, location, details in station_centers(project, analysis, only_port):
            publish((), STATION_CENTROID, state, location, key=key, **details)
    if quantity in (None, PARAXIAL_BEAM.quantity, MEDIUM_BEAM.quantity) and (only_port is None or only_port.startswith(("station-v2/", "station-v3/"))):
        for key, _, state, details in station_beams(project, analysis, only_port):
            port = medium_beam_output() if state["frame"] == MEDIUM_BEAM.unit else at_wavelength(PARAXIAL_BEAM)
            publish((), port, state, details.pop("location"), key=key, **details)
    if only_port is not None:
        selected = resolve_project_output(values, only_port)
        values = {only_port: selected} if selected is not None else {}
    return {"revision_id":row["revision_id"],"title":project["title"],"kind":row.get("model_kind","saved"),"outputs":values,
            **({"input_issues":analysis["input_issues"]} if analysis.get("input_issues") else {})}


def resolve_project_output(outputs, key):
    if key in outputs:
        return outputs[key]
    # Earlier revisions exposed a second, scalar-only key for a singleton interval.
    if key.endswith("/value"):
        item = outputs.get(key.removesuffix("/value"))
        if item and item["port"]["shape"] == "interval" and "scalar" in item["port"]["connects_as"]:
            return {**item, "value": item["value"][0],
                    "port": {**item["port"], "shape": "scalar", "connects_as": ["scalar", "interval"]}}
    return None


class FrozenProjectOutputs:
    """Resolve just the consumed ports, sharing one immutable row within a request."""

    def __init__(self, db, case_id, revision_id):
        self.row = read_project(db, case_id, revision_id)
        self._ports = {}

    def get(self, key):
        if key not in self._ports:
            outputs = published_project_outputs(self.row, only_port=key)["outputs"]
            self._ports[key] = outputs.get(key)
        return self._ports[key]


def discover_project_outputs(db, case_id, revision_id, query):
    result = project_outputs(db,case_id,revision_id,query.quantity,compact=query.port is None,only_port=query.port)
    items = [(k,v) for k,v in result["outputs"].items() if (not query.path_uid or v.get("path_uid")==query.path_uid)
             and (not query.element_uid or v.get("element_uid")==query.element_uid)
             and (not query.plane_uid or v.get("plane_uid")==query.plane_uid)]
    end = query.offset+query.limit
    return {**result,"outputs":dict(items[query.offset:end]),"total":len(items),"next_offset":end if end<len(items) else None}

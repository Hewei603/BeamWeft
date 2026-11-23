"""Source-to-station prescriptions in a saved layout, without evaluating a device or a field."""
from dataclasses import dataclass
import json
import math
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from .authoring import EditableLayout
from .beam_source import BeamSource
from .observation import FixedObservationPlane, host_positions, locate_planes
from .optics import OpticalElement, SCAN_PARAMETERS, ZERO_THICKNESS_MODELS
from .shared_media import resolved_host_route


def prefix_key(plane_uid, path_uid):
    return "/".join(("layout-prefix-v1", quote(plane_uid, safe=""), quote(path_uid, safe=""), "layout"))


def select_prefix_endpoint(selectors, inputs, plane_uid):
    if inputs.get(selectors["path"],"primary") != "primary" or inputs.get(selectors["plane"]) not in (None,plane_uid):
        raise ValueError("研究输入需对应片段的局部主光路和所选观察面。")
    return {**inputs,selectors["path"]:"primary",selectors["plane"]:plane_uid}


@dataclass
class PathElement:
    optic: OpticalElement
    scope: str
    index: int
    source_path: list
    n: float | None
    n_path: list | None
    shared_index: dict | None = None


class LayoutPrefixes:
    """Take a longitudinal slice of the original prescription. Coordinates on the slice start at its own source."""

    def __init__(self, layout):
        self.layout = layout
        self.host = layout.system
        self.anchors = host_positions(self.host)
        self.stations = locate_planes(layout, self.anchors)
        self.planes = {plane.uid:(i,plane) for i,plane in enumerate(layout.observation_planes)}
        self.paths = {path.uid:(i,path) for i,path in enumerate(layout.incident_paths)}
        self.routes = {}
        self.checked = {}

    @classmethod
    def from_document(cls, document):
        if not isinstance(document, dict):
            raise ValueError("片段需要当前工程的顺序光路。")
        # A source-to-station prescription has no optical dependency on a power ledger or port network.
        layout = EditableLayout.model_validate({key:document[key] for key in ("system","observation_planes","incident_paths") if key in document})
        return cls(layout)

    def _path(self, path_uid):
        if path_uid == "primary":
            if self.host.mode != "single_pass":
                raise ValueError("腔本征模依赖完整往返光路。请选择单程主路或独立入射支路。")
            entries = []
            for i, el in enumerate(self.host.elements):
                path = ["system","elements",i]
                entries.append(PathElement(el,"primary",i,path,el.n,[*path,"n"]))
            return self.host.model_dump(exclude={"elements"}), entries, 0., "主光束", None
        if path_uid not in self.paths:
            raise ValueError("找不到该独立入射支路。")
        i, arm = self.paths[path_uid]
        host, issue = resolved_host_route(arm, self.anchors, self.host.wavelength_nm)
        if not host:
            raise ValueError(issue or "支路没有已登记的主光路入口。")
        root = ["incident_paths",i]
        entries = []
        for j, el in enumerate(arm.elements):
            path = [*root,"elements",j]
            entries.append(PathElement(el,"arm",j,path,el.n,[*path,"n"]))
        overrides = {arm.target_uid:[*root,"target_n"],
                     **{row.target_uid:[*root,"downstream",j,"n"] for j,row in enumerate(arm.downstream)}}
        for el, _, index in host:
            j = self.anchors[el.uid][0]
            path = ["system","elements",j]
            source = index["index_source"]
            where = None
            if source == "branch_value":
                where = overrides[el.uid]
            elif source == "host_same_wavelength":
                where = [*path,"n"]
            entries.append(PathElement(el,"host",j,path,index["n"],where,index))
        source = {key:getattr(arm,key) for key in BeamSource.model_fields}
        system = {"mode":"single_pass","wavelength_nm":arm.wavelength_nm,"source":BeamSource(**source).model_dump()}
        start = self.anchors[arm.target_uid][2] - math.fsum(el.length_mm for el in arm.elements)
        return system, entries, start, arm.name, issue

    def _checked_element(self, entry):
        key = (tuple(entry.source_path), entry.n)
        if key not in self.checked:
            if entry.n is None and entry.optic.kind not in ZERO_THICKNESS_MODELS:
                self.checked[key] = f"「{entry.optic.name}」缺少该束光波长处的折射率。"
            else:
                try:
                    self.checked[key] = OpticalElement.model_validate({**entry.optic.model_dump(),"n":entry.n}).model_dump()
                except ValidationError as exc:
                    self.checked[key] = f"「{entry.optic.name}」" + exc.errors(include_url=False)[0]["msg"]
        result = self.checked[key]
        if isinstance(result,str):
            raise ValueError(result)
        return result

    def project(self, plane_uid, path_uid):
        plane, issue = self.stations.get(plane_uid,(None,"找不到登记的观察面。"))
        if issue:
            raise ValueError(issue)
        if path_uid not in self.routes:
            self.routes[path_uid] = self._path(path_uid)
        system, route, origin, label, discontinuity = self.routes[path_uid]
        if system["wavelength_nm"] is None:
            raise ValueError("此路径缺少真空波长，无法公开已定光路片段。")
        end = next((i for i,e in enumerate(route) if e.scope!="arm" and e.optic.uid==plane.target_uid),None)
        if end is None:
            raise ValueError(discontinuity or "这束光没有连续到达所选观察面。")
        segments = route[:end+1]
        reserved = {entry.optic.uid for entry in segments}
        used, optics, sources = set(), [], []
        plane_index, original_plane = self.planes[plane_uid]
        fixed = isinstance(original_plane,FixedObservationPlane)
        for idx, entry in enumerate(segments):
            element = self._checked_element(entry)
            uid = entry.optic.uid
            if not uid or uid in used:
                # Private and shared elements have separate namespaces. Only a collision needs a new local UID.
                counter = 0
                while True:
                    local = str(uuid5(NAMESPACE_URL,json.dumps(["beamweft:layout-prefix",path_uid,entry.scope,uid or entry.index,counter])))
                    if local not in reserved and local not in used:
                        break
                    counter += 1
                uid = local
            used.add(uid)
            terminal = idx == end
            projected = {**element,"uid":uid,"length_mm":plane.depth_mm if terminal else element["length_mm"]}
            if entry.shared_index:
                basis = entry.shared_index["index_source"]
                if basis == "material_dispersion":
                    projected["index_reference"] = {"model":entry.shared_index["index_model"], "wavelength_nm":system["wavelength_nm"]}
                elif basis in {"branch_value", "host_same_wavelength"} and element["kind"] == "space":
                    projected["index_reference"] = {"model":None, "wavelength_nm":system["wavelength_nm"]}
            optics.append(projected)
            length_path = (None if fixed else ["observation_planes",plane_index,"depth_mm"]) if terminal else [*entry.source_path,"length_mm"]
            sources.append({"scope":entry.scope,"source_index":entry.index,"element_uid":entry.optic.uid,"local_uid":uid,
                            "source_path":entry.source_path,"n_path":entry.n_path if "n" in SCAN_PARAMETERS.get(entry.optic.kind,()) else None,
                            "length_path":length_path, **({"shared_index":entry.shared_index} if entry.shared_index else {})})
        last_uid = optics[-1]["uid"]
        value = {"system":{**system,"elements":optics}, "incident_paths":[],
                 "observation_planes":[{**plane.model_dump(),"target_uid":last_uid}]}
        stop = self.anchors[plane.target_uid][2] + plane.depth_mm
        details = {"scope":"layout_prefix","plane_uid":plane_uid,"path_uid":path_uid,"element_uid":plane.target_uid,
                   "source_z_mm":origin,"z_mm":stop,"depth_mm":plane.depth_mm,"position_reference":original_plane.position_reference,
                   "wavelength_nm":system["wavelength_nm"],"medium_n":segments[-1].n,"element_sources":sources,
                   "coordinate_frame":"source_local_forward_mm","location":f"{label} · 源面至{plane.name}"}
        if fixed:
            details["boundary_side"] = original_plane.boundary_side
        return value, details


def saved_prefixes(project, analysis, only_key=None, *, missing_links_only=False):
    """Read the frozen model and confirm that the archived result placed the station on this route."""
    sections = (analysis.get("sections") or {}).get("planes", ())
    candidates = [(plane,beam,prefix_key(plane["uid"],beam["uid"])) for plane in sections if "message" not in plane
                  for beam in plane.get("beams",()) if beam.get("registration_available") is not False
                  and not (missing_links_only and beam.get("layout_prefix_port"))]
    if only_key is not None:
        candidates = [item for item in candidates if item[2]==only_key]
    if not candidates:
        return
    archived_indices = {(path["uid"], row["element_uid"]): row.get("n")
                        for path in (analysis.get("sections") or {}).get("paths", ())
                        for row in path.get("shared_route", ())}
    try:
        projections = LayoutPrefixes.from_document(project)
    except ValueError:
        return
    for plane, beam, key in candidates:
        try:
            value, where = projections.project(plane["uid"],beam["uid"])
        except ValueError:
            continue
        # A newly available material resolver must not backfill a previously untraced interval in an archived result.
        if any(archived_indices.get((beam["uid"], s["element_uid"])) != s["shared_index"]["n"]
               for s in where["element_sources"] if (s.get("shared_index") or {}).get("index_source") == "material_dispersion"):
            continue
        if (plane.get("target_uid") != where["element_uid"] or plane.get("depth_mm") != where["depth_mm"]
                or plane.get("z_mm") is None or not math.isclose(plane["z_mm"],where["z_mm"],abs_tol=1e-9,rel_tol=1e-12)
                or beam.get("wavelength_nm") != where["wavelength_nm"] or beam.get("index") != where["medium_n"]):
            continue
        issues = analysis.get("input_issues",())
        if any(_depends_on_issue(issue, where, projections.paths) for issue in issues):
            continue
        if issues:
            where["excluded_issue_indices"] = list(range(len(issues)))
        yield key, value, where


def _depends_on_issue(issue, where, paths):
    """Honor blockers recorded with the original method, even if a later schema supplies a new default."""
    owner = issue.get("owner")
    fields = issue.get("path") or []
    if len(fields) < 2 or fields[0] != "elements":
        return owner == where["path_uid"]
    if owner == "primary":
        path = ["system","elements",fields[1]]
    elif owner in paths:
        path = ["incident_paths",paths[owner][0],"elements",fields[1]]
    else:
        return False
    for source in where["element_sources"]:
        if source["source_path"] == path:
            # A branch may directly supply a material index at its own wavelength.
            return fields[2:] != ["n"] or source["n_path"] == [*path,"n"]
    return False

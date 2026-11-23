"""Publish archived observation-plane beam states without evaluating optics or installed devices."""
import math
from urllib.parse import quote

from .beam_source import MEDIUM_BEAM, PARAXIAL_BEAM


def station_beams(project, analysis, only_key=None, *, missing_links_only=False):
    sections = analysis.get("sections") or {}
    if not sections.get("planes"):
        return
    stations = {plane["uid"]: plane for plane in project.get("observation_planes", ())}
    branches = {path["uid"]: path for path in project.get("incident_paths", ())}
    system = project["system"]
    warnings = list(dict.fromkeys([*analysis.get("scope_warnings", ()), *sections.get("model_warnings", ())]))
    for section in sections.get("planes", ()):
        station = stations.get(section["uid"])
        if station is None or "message" in section:
            continue
        for beam in section["beams"]:
            if missing_links_only and beam.get("continuation_port"):
                continue
            uid = beam["uid"]
            index = beam.get("index")
            if index is None or not math.isfinite(index) or index <= 0:
                continue
            namespace = "station-v2" if index == 1 else "station-v3"
            key = "/".join((namespace, quote(section["uid"], safe=""), quote(uid, safe=""), "beam"))
            if only_key is not None and key != only_key:
                continue
            if uid == "primary":
                cavity = system["mode"] != "single_pass"
                if cavity and not analysis.get("stable_both_planes"):
                    continue
                if not cavity and not any(system.get(k) is not None for k in ("source", "input_radius_mm", "input_curvature_mm")):
                    continue
                name = "腔内正向本征模" if cavity else "主光束"
                source = system.get("source") or {"tangential": {"m2": 1 if cavity else system.get("beam_quality_m2", 1)}}
            elif uid in branches and beam.get("registration_available") is True:
                source = branches[uid]
                name = source["name"]
            else:
                continue
            state = _registered_beam(beam, source, index)
            if state is None:
                continue
            # Preserve the status of the frozen model independently of any later edits to a catalogue.
            local_warnings = [v["transfer_warning"] for v in beam["field_axes"].values() if v.get("transfer_warning")]
            if warnings or local_warnings:
                state["model_warnings"] = list(dict.fromkeys([*warnings, *local_warnings]))
            yield key, beam, state, {"location": f"{station['name']} · {name}", "scope": "observation",
                                     "plane_uid": section["uid"], "path_uid": uid, "element_uid": section["target_uid"],
                                     "z_mm": section.get("z_mm"), "wavelength_nm": beam["wavelength_nm"],
                                     **({"medium_n":index} if index != 1 else {})}


def with_station_links(project, analysis):
    """Attach action links to the presented rows, including old snapshots with sufficient evidence."""
    from .layout_prefix import saved_prefixes

    beams_by_plane = {(where["plane_uid"], where["path_uid"]): key
             for key, _, _, where in station_beams(project, analysis, missing_links_only=True)}
    layouts_by_plane = {(where["plane_uid"], where["path_uid"]): key
             for key, _, where in saved_prefixes(project, analysis, missing_links_only=True)}
    if not beams_by_plane and not layouts_by_plane:
        return analysis
    sections = analysis["sections"]
    planes = []
    for plane in sections["planes"]:
        beams = []
        for beam in plane["beams"]:
            station = (plane["uid"],beam["uid"])
            beam_key, layout_key = beams_by_plane.get(station), layouts_by_plane.get(station)
            beams.append({**beam, **({"continuation_port":beam_key} if beam_key else {}),
                          **({"layout_prefix_port":layout_key} if layout_key else {})})
        planes.append({**plane, "beams": beams})
    return {**analysis, "sections": {**sections, "planes": planes}}


def _registered_beam(beam, source, index):
    if not beam.get("wavelength_nm") or not beam.get("field_axes"):
        return None
    launch_basis = beam.get("launch_basis")
    if launch_basis not in ("reference_axis", "ideal_on_axis", "specified"):
        return None
    if source.get("launch_ray") is not None and launch_basis != "specified":
        return None
    axes, origins, ray = {}, {}, {}
    useful = False
    for axis, prefix in (("tangential", "t"), ("sagittal", "s")):
        reading = beam["field_axes"].get(axis, {})
        basis = beam.get("width_basis", {}).get(axis)
        if basis not in ("gaussian", "second_moment", "equivalent"):
            return None
        original = source.get(axis)
        if original is None:
            original = source.get("tangential", {})
        radius = beam.get("radii_mm", {}).get(axis)
        vergence = reading.get("reduced_vergence_per_mm")
        curvature = index / vergence if vergence else None
        curved = curvature is not None and math.isfinite(curvature)
        local = {"radius_mm": radius, "wavefront": "curved" if curved else "waist" if vergence == 0 else "unknown",
                 "curvature_mm": curvature if curved else None, "m2": original.get("m2"), "second_moment_radius": basis == "second_moment"}
        if basis == "equivalent":
            local["equivalent_radius"] = True
        if original.get("fit_origin"):
            local["fit_origin"] = original["fit_origin"]
        if origin := reading.get("transfer_origin"):
            origins[axis] = {**origin, "medium_n": 1.} if index != 1 else origin
        axes[axis] = local
        angle = reading.get("reduced_angle")
        ray[prefix + "_angle_mrad"] = 1000 * angle / index if angle is not None else None
        ray[prefix + "_offset_mm"] = reading.get("center_mm")
        useful |= radius is not None or vergence is not None or bool(origin)
    if not useful and not (launch_basis == "specified" and any(v is not None for v in ray.values())):
        return None
    frame = {"frame": PARAXIAL_BEAM.unit} if index == 1 else {"frame": MEDIUM_BEAM.unit, "medium_n": index}
    return {**frame, "wavelength_nm": beam["wavelength_nm"], **axes,
            "launch_ray": ray if launch_basis == "specified" else None, **({"origins": origins} if origins else {})}

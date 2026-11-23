"""Resolve shared physical segments in the coordinate and wavelength frame of one beam."""
import math

from ..physics.materials import MaterialError, index_info, load_materials, resolve_material
from .optics import SCAN_PARAMETERS


def _shared_index(path, optic, override, *, independent, same_wavelength):
    if "n" not in SCAN_PARAMETERS.get(optic.kind, ()):
        return {"n": optic.n, "index_source": "not_applicable"}
    if override is not None:
        return {"n": override, "index_source": "branch_value"}
    if not path.through_uid:
        return {"n": None, "index_source": "legacy_explicit"}
    if independent or not path.inherit_index_at_same_wavelength:
        return {"n": None, "index_source": "independent"}
    reference = optic.index_reference
    if reference and reference.model:
        try:
            model = resolve_material(reference.model)
        except MaterialError:
            return {"n": None, "index_source": "missing", "index_model": reference.model}
        material = load_materials()[model]
        evidence = {"index_model": model, "index_form": material["model"], "catalog_source": material["source"],
                    "index_basis": material.get("index_basis"), "confidence": material.get("confidence")}
        if bounds := material.get("valid_range_um"):
            evidence["valid_range_nm"] = [bound * 1000 for bound in bounds]
        if material.get("index_basis") != "scalar":
            return {"n": None, "index_source": "directional_model", **evidence}
        if path.wavelength_nm is None:
            return {"n": None, "index_source": "wavelength_missing", **evidence}
        info = index_info(model, path.wavelength_nm)
        if info.in_range and math.isfinite(info.n):
            return {"n": info.n, "index_source": "material_dispersion", **evidence}
        return {"n": None, "index_source": "out_of_range", **evidence}
    if same_wavelength and optic.n is not None:
        return {"n": optic.n, "index_source": "host_same_wavelength"}
    return {"n": None, "index_source": "missing" if path.wavelength_nm is not None else "wavelength_missing"}


def resolved_host_route(path, anchors, host_wavelength_nm=None):
    """Physical elements and local index evidence, stopping at the first topology break."""
    anchor = anchors.get(path.target_uid)
    if not anchor or (not path.through_uid and anchor[1].kind != "space"):
        return [], "原来接入的元件已移除或已改变，请选定新入口并核对该波长的介质折射率。"
    start_index = anchor[0]
    issue = None
    if path.through_uid:
        end = anchors.get(path.through_uid)
        if not end or end[0] < start_index:
            return [], "共享光路终点已移除或移至入口之前，请重新选择终点。"
        physical = [row for row in anchors.values() if start_index <= row[0] <= end[0]]
        if len(physical) != end[0] - start_index + 1:
            return [], "共享光路内有未登记标识的元件，请先保存这些元件。"
    else:
        physical = [anchor]
        for offset, segment in enumerate(path.downstream, 1):
            entry = anchors.get(segment.target_uid)
            if not entry or entry[0] != start_index + offset or entry[1].kind != "space":
                issue = "后续段与上游不再连续，请核对顺序及中间光学元件。仅计算已连通部分。"
                break
            physical.append(entry)
    indices = {path.target_uid: path.target_n, **{s.target_uid: s.n for s in path.downstream}}
    independent = set(path.independent_index_uids)
    same_wave = (host_wavelength_nm is not None and path.wavelength_nm is not None
                 and math.isclose(path.wavelength_nm, host_wavelength_nm, rel_tol=1e-12))
    result = []
    for _, optic, position in physical:
        index = _shared_index(path, optic, indices.get(optic.uid), independent=optic.uid in independent,
                              same_wavelength=same_wave)
        result.append((optic, position, index))
    return result, issue


def host_route(path, anchors, host_wavelength_nm=None):
    resolved, issue = resolved_host_route(path, anchors, host_wavelength_nm)
    return [(el, z, index["n"]) for el, z, index in resolved], issue

"""Recover the spectral basis of a mounted gain segment from its archived installation."""
from math import isclose, isfinite

from ..inventory.crystals import crystal_medium
from ..physics.materials import MaterialError, index_info, load_materials, resolve_material
from .optics import IndexReference


def restore_crystal_indices(optics, previous, bindings, context, current_bindings=None):
    # Older installs saved the catalogue fit in an ordered section receipt, separately from the element.
    # A scalar or a material name alone cannot establish which fit the project used.
    uids = context.get("crystal_element_uids") or []
    sections = context.get("crystal_sections") or []
    stock = context.get("crystal_inventory_id")
    old_wave = previous.get("wavelength_nm")
    if (not isinstance(uids, list) or not uids or not isinstance(sections, list) or len(uids) != len(sections)
            or not all(isinstance(uid, str) for uid in uids) or not all(isinstance(row, dict) for row in sections)
            or stock is None or old_wave is None or context.get("wavelength_nm") != old_wave):
        return optics
    by_uid = {el["uid"]: el for el in previous["elements"] if el.get("uid")}
    owned = {previous["elements"][p["element_index"]].get("uid") for p in bindings
             if p.get("inventory_id") == stock and p.get("role") == "crystal"}
    if current_bindings is not None:
        owned.intersection_update(optics.elements[p.element_index].uid for p in current_bindings
                                  if p.inventory_id == stock and p.role == "crystal")
    rows = dict(zip(uids, sections, strict=True))
    elements = list(optics.elements)
    changed = False
    for i, el in enumerate(elements):
        old = by_uid.get(el.uid)
        row = rows.get(el.uid)
        if (not row or not old or row.get("uncertain") or el.uid not in owned or el.index_reference or el.kind != "space"
                or el.length_mm != old["length_mm"] or old["length_mm"] != row.get("length_mm")
                or el.medium_role != old.get("medium_role") or el.n != old.get("n")
                or old.get("n") is None or row.get("model_index") != old["n"]):
            continue
        model = row.get("index_model")
        material = row.get("material")
        if not isinstance(model, str) or not isinstance(material, str) or not model or crystal_medium(material) != model:
            continue
        try:
            info = index_info(model, old_wave)
        except MaterialError:
            continue
        if not info.in_range or not isclose(info.n, old["n"], rel_tol=1e-12):
            continue
        new_n = old["n"] if optics.wavelength_nm == old_wave else None
        if load_materials()[info.material].get("index_basis") != "scalar":
            # Earlier versions assumed an extraordinary wave for a bare vanadate label.
            try:
                explicit = resolve_material(material) == info.material
            except MaterialError:
                explicit = False
            if not explicit:
                model = None
                new_n = None
        if model and optics.wavelength_nm is not None:
            info = index_info(model, optics.wavelength_nm)
            new_n = info.n if info.in_range and isfinite(info.n) else None
        ref = IndexReference(model=model, wavelength_nm=optics.wavelength_nm)
        elements[i] = el.model_copy(update={"index_reference":ref, "n":new_n})
        changed = True
    return optics.model_copy(update={"elements":elements}) if changed else optics

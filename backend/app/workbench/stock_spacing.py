"""Longitudinal mounting datums on a single unfolded optical path."""
from math import fsum, isclose

from .assemblies import membership
from .optics import material_interval


def mount_spacing(project, mount, optics, original, old_parts, installed, policy):
    """Apply the selected mechanical constraint to the candidate; coordinates start at its path origin."""
    start, end = mount.indices[0], mount.indices[-1] + 1
    old_elements = original["elements"]
    elements = optics["elements"]
    old_entry = fsum(e["length_mm"] for e in old_elements[:start])
    old_length = fsum(e["length_mm"] for e in old_elements[start:end])
    new_length = fsum(e["length_mm"] for e in elements[start:start + len(installed)])
    change = new_length - old_length
    gap = None
    notes = {
        "keep_gaps": "保留外部间隔，所选光路的总长度随实物厚度变化。",
        "fixed_entrance": "固定实物入射面，由紧邻的下游空气间隔补偿厚度变化。",
        "fixed_exit": "固定实物出射面，由紧邻的上游空气间隔补偿厚度变化。",
    }
    if policy != "keep_gaps" and not isclose(old_length, new_length, rel_tol=1e-14, abs_tol=1e-12):
        before = policy == "fixed_exit"
        index = start - 1 if before else end
        direction = "上游" if before else "下游"
        message = f"固定此端面需要紧邻{direction}的可调空气间隔。"
        if not 0 <= index < len(old_elements):
            raise ValueError(message + "所选实物已位于光路端点，请核对安装基准。")
        previous = old_elements[index]
        uid = previous["uid"]
        part = next((p for p in old_parts if p["element_index"] == index), {})
        if (previous["kind"] != "space" or material_interval(previous, part)
                or previous.get("clear_aperture") or previous.get("component_origin")
                or uid in membership(project.get("assemblies", []), mount.path_uid)
                or part.get("role") in {"medium", "crystal", "pump_input", "laser_output"}
                or part.get("inventory_id") is not None or part.get("ownership") == "owned"
                or "length_mm" in part.get("locked_parameters", [])):
            raise ValueError(message + f"「{previous['name']}」是光学实物或尺寸受约束。请核对间隔或改选安装基准。")

        old_gap = previous["length_mm"]
        remaining = old_gap - change
        if remaining < -1e-9:
            raise ValueError(f"{direction}空气间隔「{previous['name']}」只有 {old_gap:g} mm，"
                             f"补偿所需行程为 {change:g} mm。请调整布局或选择其他安装基准。")
        if remaining > 1e6:
            raise ValueError(f"{direction}空气间隔补偿后将超过模型支持的 1000000 mm。请核对厚度和单位。")
        remaining = max(0., remaining)
        updated = next(e for e in elements if e["uid"] == uid)
        updated["length_mm"] = remaining
        gap = {"uid": uid, "name": previous["name"], "side": "before" if before else "after",
               "old_length_mm": old_gap, "new_length_mm": remaining}
    new_entry = fsum(e["length_mm"] for e in elements[:start])
    return {"spacing_policy": policy, "compensating_gap": gap,
            "old_entry_z_mm": old_entry, "new_entry_z_mm": new_entry,
            "old_exit_z_mm": old_entry + old_length, "new_exit_z_mm": new_entry + new_length,
            "note": notes[policy]}

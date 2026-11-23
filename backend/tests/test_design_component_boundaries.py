"""Bound hardware must survive the specialized designer's scalar compatibility layer."""
from copy import deepcopy
import math

import pytest

from app.design.brief import extraction_prompt, validate_fact
from app.design.solver import solve_design, yvo4_index
from app.physics.materials import n_real


def facts(**updates):
    values = {
        "pump_wavelength_nm": 808, "laser_wavelength_nm": 1064,
        "pump_power_w": 6, "pump_source": "fiber", "cavity_type": "linear",
        "gain_material": "Nd:YAG", "crystal_length_mm": 18,
        "active_length_mm": 10, "front_cap_mm": 4, "rear_cap_mm": 4,
        "crystal_width_mm": 3, "crystal_height_mm": 3,
        "ar_wavelengths_nm": [808, 1064], "collimator_f_mm": 50,
        "focus_f_mm": 100, "fiber_core_um": 200, "fiber_na": .22,
        "input_roc_mm": 200, "input_mirror_flat": False, "output_mirror_flat": True,
        "input_mirror_owned": True, "output_mirror_owned": True,
        "crystal_front_mm": 3, "output_gap_mm": 12, "max_cavity_mm": 80,
    }
    values.update(updates)
    return {key: {"value": value} for key, value in values.items()}


def crystal(*, absorber=False, mixed_host=False):
    segments = [
        {"material": "YVO4" if mixed_host else "YAG", "role": "undoped", "length_mm": 4},
        {"material": "Nd:YAG", "role": "gain", "length_mm": 10, "doping_pct": None, "uncertain": True},
        {"material": "YAG", "role": "undoped", "length_mm": 4},
    ]
    if absorber:
        segments.append({"material": "Cr:YAG", "role": "saturable_absorber", "length_mm": 2,
                         "initial_transmission_pct": None, "note": "T90的定义和波长尚未确认"})
    return {"id": 12, "name": "Composite crystal", "category": "gain_crystal",
            "condition": "ok", "available_quantity": 1, "material": "YAG+Nd:YAG+YAG" + ("+Cr:YAG" if absorber else ""),
            "thickness_mm": 20 if absorber else 18, "doping_pct": None,
            "raw_spec": "3x3x(4+10+4+2), T=90%±2%" if absorber else "3x3x(4+10+4)",
            "crystal_spec": {"width_mm": 3, "height_mm": 3, "segments": segments}}


def test_plain_nd_yag_uses_its_own_dispersion_at_both_wavelengths():
    result = solve_design(facts(), [])
    assert result["candidates"], result
    assert "crystal_index" not in {question["key"] for question in result["questions"]}
    candidate = result["candidates"][0]
    assert candidate["crystal"]["n"] == pytest.approx(n_real("yag", 1064))
    indices = {a["name"]: a for a in result["assumptions"] if a["name"] in {"晶体折射率", "泵浦折射率"}}
    assert indices["晶体折射率"]["value"] == pytest.approx(n_real("yag", 1064), abs=1e-6)
    assert indices["泵浦折射率"]["value"] == pytest.approx(n_real("yag", 808), abs=1e-6)
    assert indices["晶体折射率"]["value"] != indices["泵浦折射率"]["value"]
    assert not any("YVO4" in source["title"] for source in result["sources"])


def test_unidentified_gain_label_containing_a_host_name_does_not_select_its_dispersion():
    result = solve_design(facts(gain_material="tray-YAG? unverified"), [])
    assert not result["candidates"]
    assert any(q["key"] == "crystal_index" for q in result["questions"])
    assert not any(s.get("key") in {"crystal_index","pump_index"} for s in result["assumptions"])


def test_evidence_only_geometry_can_use_measured_index_and_thickness_with_unidentified_sections():
    item = crystal()
    item["crystal_spec"]["segments"][1].update(material=None,role=None,length_mm=None)
    item["material"] = "Nd:YAG printed on crate"
    data = facts(crystal_inventory_id=12, crystal_index=1.51, target_mode_radius_um=160)
    del data["gain_material"]
    result = solve_design(data, [item], assumption_policy="evidence_only")
    assert "gain_material" not in result["resolved_inputs"]
    cold = result["evidence_assessment"]["cold_cavity"]
    assert cold["status"] == "computed" and cold["geometry"]["crystal"]["n"] == 1.51
    assert not any("target_error_um" in row for row in cold["rows"])


@pytest.mark.parametrize("updates", [
    {"gain_material": "YAG+1%Nd:YAG+YAG+Cr⁴⁺:YAG"},
    {"has_saturable_absorber": True},
    {"absorber_material": "Co:spinel"},
    {"absorber_length_mm": 2},
])
def test_explicit_absorber_is_not_silently_run_as_continuous_wave(updates):
    original = facts(**updates)
    before = deepcopy(original)
    result = solve_design(original, [])
    assert result["status"] == "unsupported"
    assert result["candidates"] == []
    assert "threshold_scan" not in result
    assert "optimization" not in result
    assert result["power_prediction"]["output_w"] is None
    assert "analyze_optical_system" in " ".join(result["next_steps"])
    assert "动力学" in result["summary"]
    assert original == before


def test_bound_absorber_segment_blocks_even_if_scalar_material_omits_cr():
    item = crystal(absorber=True)
    original = facts(crystal_inventory_id=12, crystal_length_mm=20, rear_cap_mm=6)
    result = solve_design(original, [item])
    assert result["status"] == "unsupported"
    assert not result["candidates"]
    assert result["facts"]["rear_cap_mm"]["value"] == 6
    assert item["crystal_spec"]["segments"][-1]["role"] == "saturable_absorber"
    assert result["power_prediction"]["output_w"] is None


def test_structured_bound_crystal_supplies_missing_geometry_without_rewriting_facts():
    original = facts(crystal_inventory_id=12)
    for key in ("gain_material", "crystal_length_mm", "active_length_mm", "front_cap_mm", "rear_cap_mm", "crystal_width_mm", "crystal_height_mm"):
        original.pop(key)
    before = deepcopy(original)
    result = solve_design(original, [crystal()])
    assert result["candidates"], result
    assert result["resolved_inputs"]["gain_material"] == "Nd:YAG"
    assert result["resolved_inputs"]["crystal_length_mm"] == 18
    assert result["resolved_inputs"]["active_length_mm"] == 10
    assert result["candidates"][0]["crystal"]["n"] == pytest.approx(n_real("yag", 1064))
    assert original == before
    assert result["facts"] == before
    assert "doping_pct" not in result["resolved_inputs"]
    assert any(a["name"] == "分段参数待核验" for a in result["assumptions"])


def test_bound_structure_conflict_requires_user_resolution_instead_of_old_geometry():
    original = facts(crystal_inventory_id=12, crystal_length_mm=13, active_length_mm=8, front_cap_mm=0, rear_cap_mm=5)
    before = deepcopy(original)
    result = solve_design(original, [crystal()])
    assert result["status"] == "needs_input"
    assert not result["candidates"]
    messages = " ".join(i["text"] for i in result["issues"])
    assert "crystal_length_mm=18" in messages and "13" in messages
    assert "静默" in messages
    assert original == before


def test_mixed_host_bound_structure_requires_general_segment_model():
    result = solve_design(facts(crystal_inventory_id=12), [crystal(mixed_host=True)])
    assert result["status"] == "unsupported"
    assert not result["candidates"]
    assert "单一均匀晶体" in result["summary"]


def test_prior_nd_yvo4_cavity_geometry_is_preserved():
    result = solve_design(facts(gain_material="Nd:YVO4", crystal_length_mm=13, active_length_mm=8,
                                front_cap_mm=0, rear_cap_mm=5), [])
    candidate = result["candidates"][0]
    n = yvo4_index(1064)
    assert candidate["crystal"]["n"] == pytest.approx(n)
    assert candidate["length_mm"] == 28
    assert candidate["active_center_mm"] == 7
    assert candidate["m"] == pytest.approx(1-2*(15+13/n)/200, abs=1e-6)
    assert candidate["mode_radius_mm"] == pytest.approx(.149026569, abs=1e-6)
    assert math.isfinite(candidate["mode_radius_mm"])


def test_absorber_intake_has_distinct_typed_fields_and_keeps_t90_ambiguous():
    assert validate_fact("has_saturable_absorber", True) is True
    assert validate_fact("absorber_material", "Cr⁴⁺:YAG") == "Cr⁴⁺:YAG"
    assert validate_fact("absorber_length_mm", 2) == 2
    with pytest.raises(ValueError):
        validate_fact("absorber_initial_transmission_pct", float("nan"))
    prompt = extraction_prompt()
    assert "Do not hide this segment inside rear_cap_mm" in prompt
    assert "An ambiguous label T=90% does not establish the output mirror transmission" in prompt


@pytest.mark.parametrize("transmission", [100, 110])
def test_no_output_mirror_feedback_is_reported_instead_of_log_zero(transmission):
    # Imported/current selected hardware can reach this solver without going
    # through the old text fact validator's strictly-less-than-100 check.
    result = solve_design(facts(oc_transmission_pct=transmission), [])
    assert result["status"] == "unsupported"
    assert not result["candidates"]
    assert "threshold_scan" not in result and "search" not in result
    assert result["power_prediction"]["output_w"] is None
    assert "反射反馈" in result["summary"]
    assert any("功率透过率" in step for step in result["next_steps"])


def test_bound_fully_transmitting_output_mirror_never_reaches_threshold_logarithm():
    mirror = {"id": 77, "name": "标称T100平镜", "category": "mirror", "roc_is_flat": True,
              "roc_mm": None, "diameter_mm": 25, "condition": "ok", "available_quantity": 1,
              "coatings": [{"surface": "S1", "function": "PR", "wl_min_nm": 1064,
                            "wl_max_nm": 1064, "value_type": "T", "comparator": "=", "value_pct": 100}]}
    result = solve_design(facts(M2_inventory_id=77), [mirror])
    assert result["resolved_inputs"]["oc_transmission_pct"] == 100
    assert result["status"] == "unsupported" and not result["candidates"]
    assert "反射反馈" in result["summary"]

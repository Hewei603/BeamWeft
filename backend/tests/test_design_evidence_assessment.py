"""A specimen and a proposed design must not acquire each other's parameters."""
import math

import pytest

from app.design.brief import validate_fact
from app.design.solver import solve_design


def assess(values, inventory=()):
    facts = {key: {"value": value, "source": "constraint_panel"} for key, value in values.items()}
    return solve_design(facts, list(inventory), assumption_policy="evidence_only")


CAVITY = {"cavity_type": "linear", "laser_wavelength_nm": 1064, "input_mirror_flat": True,
          "output_roc_mm": 160, "crystal_index": 1.8, "crystal_front_mm": 7,
          "crystal_length_mm": 12, "output_gap_mm": 40}


def test_a_partial_optics_inventory_is_not_an_assignment_or_a_new_physical_fact():
    known = {"pump_wavelength_nm": 808, "laser_wavelength_nm": 1064, "collimator_f_mm": 95}
    available = [{"id": 4, "category": "lens", "name": "f65", "condition": "ok",
                  "focal_length_mm": 65, "available_quantity": 1, "coatings": []}]
    result = assess(known, available)
    assert result["status"] == "needs_input"
    assert result["resolved_inputs"] == known
    assert not result["assumptions"] and not result["component_bindings"] and not result["candidates"]
    assert result["threshold_scan"] == []
    assert not result["evidence_assessment"]["cold_cavity"]["rows"]
    locked = assess({**known, "focus_lens_inventory_id": 4}, available)
    pump = locked["evidence_assessment"]["pump_imaging"]
    assert locked["component_bindings"]["focus_lens"]["id"] == 4
    assert pump["status"] == "conditional" and pump["magnification"] == pytest.approx(65/95)
    assert "beam" not in pump


def test_partial_fixed_cold_cavity_matches_the_analytic_plano_concave_mode():
    result = assess(CAVITY)
    cold = result["evidence_assessment"]["cold_cavity"]
    row, = cold["rows"]
    assert cold["geometry"]["R1_mm"] is None and cold["mode"] == "fixed"
    assert "segments" not in cold["geometry"]["crystal"]
    assert row["stable"] and row["length_mm"] == 59
    effective = 7 + 12/1.8 + 40
    z_rayleigh = math.sqrt(effective * (160 - effective))
    reduced_z = 7 + 6/1.8
    expected_radius = math.sqrt(1064e-6 / math.pi * (z_rayleigh + reduced_z**2 / z_rayleigh))
    assert row["mode_radius_mm"] == pytest.approx(expected_radius, rel=1e-12)
    assert result["evidence_assessment"]["pump_imaging"]["status"] == "needs_input"
    assert "pump_index" not in result["resolved_inputs"] and not result["threshold_scan"]
    assert result["sources"] == []


def test_scan_keeps_user_range_and_returns_marginal_and_unstable_points():
    inputs = {**CAVITY, "min_output_gap_mm": .15, "max_output_gap_mm": 180.7}
    del inputs["output_gap_mm"]
    cold = assess(inputs)["evidence_assessment"]["cold_cavity"]
    assert cold["mode"] == "scan"
    assert cold["rows"][0]["output_gap_mm"] == .15
    assert cold["rows"][0]["stable"] and not cold["rows"][-1]["stable"]
    assert cold["rows"][-1]["output_gap_mm"] == 180.7
    assert cold["rows"][-1]["mode_radius_mm"] is None
    flat = {**CAVITY, "output_mirror_flat": True}
    del flat["output_roc_mm"]
    row, = assess(flat)["evidence_assessment"]["cold_cavity"]["rows"]
    assert row["marginal"] and not row["stable"] and row["mode_radius_mm"] is None
    bad = assess({**CAVITY, "max_output_gap_mm": 30})
    assert bad["evidence_assessment"]["cold_cavity"]["status"] == "conflict"


def test_index_and_crystal_segments_are_supported_only_to_the_extent_recorded():
    inputs = {**CAVITY, "gain_material": "Nd:YVO4", "active_length_mm": 6}
    del inputs["crystal_index"]
    missing = assess(inputs)["evidence_assessment"]["cold_cavity"]
    assert "crystal_index" in missing["missing"] and not missing["rows"]
    geometry = assess({**inputs, "crystal_index": 2.1, "front_cap_mm": 2})["evidence_assessment"]["cold_cavity"]["geometry"]
    assert [(s["medium_role"], s["length_mm"]) for s in geometry["crystal"]["segments"]] == [
        ("undoped", 2), ("gain", 6), ("undoped", 4)]


def test_only_an_explicit_goal_at_a_known_plane_can_rank_the_gap_samples():
    inputs = {**CAVITY, "min_output_gap_mm": 5, "max_output_gap_mm": 46,
              "active_length_mm": 5, "front_cap_mm": 2, "target_mode_radius_um": 130}
    del inputs["output_gap_mm"]
    cold = assess(inputs)["evidence_assessment"]["cold_cavity"]
    comparison = cold["target_comparison"]
    assert comparison["depth_from_crystal_front_mm"] == 4.5
    rows = cold["rows"]
    assert all(row["target_error_um"] == pytest.approx(abs(row["target_plane_radius_um"] - 130)) for row in rows)
    assert all(rows[number - 1]["target_error_um"] == pytest.approx(min(row["target_error_um"] for row in rows))
               for number in comparison["closest_rows"])
    assert comparison["closest_rows"] and "closest_row_indices" not in comparison
    # Put the target halfway between adjacent radii. Both points can be passed directly to import-project.
    inputs["target_mode_radius_um"] = (rows[8]["target_plane_radius_um"] + rows[9]["target_plane_radius_um"]) / 2
    tied = assess(inputs)["evidence_assessment"]["cold_cavity"]["target_comparison"]
    assert tied["closest_rows"] == [9, 10]
    del inputs["front_cap_mm"]
    unresolved = assess(inputs)["evidence_assessment"]["cold_cavity"]["target_comparison"]
    assert unresolved["depth_from_crystal_front_mm"] is None and "closest_rows" not in unresolved


def test_named_fiber_diameter_and_na_do_not_predict_a_measured_beam():
    inputs = {**CAVITY, "pump_wavelength_nm": 808, "pump_index": 1.65, "pump_source": "fiber",
              "collimator_f_mm": 15, "focus_f_mm": 30, "source_z_mm": -72,
              "collimator_z_mm": -57, "focus_lens_z_mm": -12, "fiber_core_um": 100, "fiber_na": .15}
    pump = assess(inputs)["evidence_assessment"]["pump_imaging"]
    assert pump["status"] == "conditional" and pump["geometric_core_image_radius_mm"] == pytest.approx(.1)
    assert "beam" not in pump
    measured = {**inputs, "pump_exit_waist_radius_um": 50, "pump_farfield_half_angle_mrad": 10}
    pump = assess(measured)["evidence_assessment"]["pump_imaging"]
    assert pump["status"] == "modeled"
    assert pump["beam"]["m2"] == pytest.approx(math.pi * .05 * .01 / .000808)
    assert pump["beam"]["points"][0] == {"z_mm": -72, "w_mm": .05}
    assert pump["beam"]["minimum_inside_crystal"]["w_mm"] > 0
    assert assess({**measured, "pump_farfield_half_angle_mrad": .1})["evidence_assessment"]["pump_imaging"]["status"] == "conflict"


def test_transmission_endpoints_survive_intake_and_policy_names_are_validated():
    assert validate_fact("oc_transmission_pct", 0) == 0
    assert assess({**CAVITY, "oc_transmission_pct": validate_fact("oc_transmission_pct", 100)})["status"] == "unsupported"
    with pytest.raises(ValueError, match="assumption_policy"):
        solve_design({}, [], assumption_policy="unknown")

from copy import deepcopy
import math

import numpy as np
import pytest

from app.design.analysis import analyze_design, probe_design
from app.design.solver import solve_design
from app.workbench.authoring import analyze_editable
from app.workbench.pump_geometry import project_from_candidate_pump


INPUTS = dict(cavity_type="linear", gain_material="Nd:YAG", input_mirror_flat=True, output_roc_mm=210,
              laser_wavelength_nm=1030, crystal_index=1.64, crystal_length_mm=12, crystal_front_mm=15,
              front_cap_mm=2, active_length_mm=7, rear_cap_mm=3, crystal_width_mm=3, crystal_height_mm=3,
              pump_wavelength_nm=940, pump_index=1.71, collimator_f_mm=45, focus_f_mm=32,
              source_z_mm=-150, collimator_z_mm=-101, focus_lens_z_mm=-34, output_gap_mm=60,
              pump_exit_waist_radius_um=140, pump_farfield_half_angle_mrad=5.5)


def solve(values):
    return solve_design({k: {"value": v} for k, v in values.items()}, [])


@pytest.mark.parametrize("focus", [-34, -17])
def test_candidate_rank_uses_calibrated_width_at_same_plane_and_replays(focus):
    values = {**INPUTS, "focus_lens_z_mm": focus}
    # Disagreeing catalog values must not overwrite measured second moments.
    design = solve({**values, "fiber_core_um": 10, "fiber_na": .15})
    candidate, = design["candidates"]
    pump = candidate["pump"]
    assert pump["launch_basis"] == "measured_second_moment"
    assert pump["launch_radius_mm"] == .14 and pump["launch_half_angle_rad"] == .0055
    assert pump["estimated_m2"] == pytest.approx(math.pi * .14 * .0055 / .00094)
    assert "pump_source" not in design["resolved_inputs"]
    center = candidate["active_center_mm"]
    # Independent second-moment transport in the (x, n*theta) ray convention.
    def space(distance):
        return np.array([[1., distance], [0., 1.]])

    def lens(focal):
        return np.array([[1., 0.], [-1. / focal, 1.]])

    matrix = (space((center - 15) / 1.71 + 15 - focus) @ lens(32) @ space(focus + 101)
              @ lens(45) @ space(49))
    expected = math.hypot(matrix[0, 0] * .14, matrix[0, 1] * .0055)
    assert pump["radius_at_active_center_mm"] == pytest.approx(expected, rel=1e-12)
    assert candidate["target_radius_mm"] == pytest.approx(expected)
    assert candidate["score_terms"]["mode_size"] == pytest.approx(
        abs(math.log(candidate["mode_radius_mm"] / expected)), abs=1e-5)
    assert 15 <= pump["minimum_inside_crystal"]["z_mm"] <= 27
    assert pump["minimum_inside_crystal"]["w_mm"] <= expected
    assert design["power_prediction"]["output_w"] is None and "image_radius_mm" not in pump
    assert probe_design(design, candidate, mode="pump", z_mm=center)["point"]["w_mm"] == pytest.approx(expected)
    assert analyze_design(design, candidate, mode="pump")["launch_basis"] == pump["launch_basis"]


def test_explicit_target_wins_and_missing_or_invalid_measurement_cannot_trigger_nominal_replay():
    design = solve({**INPUTS, "target_mode_radius_um": 87})
    candidate, = design["candidates"]
    assert candidate["target_radius_mm"] == .087
    assert candidate["pump"]["radius_at_active_center_mm"] != pytest.approx(.087)
    for angle in [None, .01]:
        values = {**INPUTS, "pump_source": "fiber", "fiber_core_um": 100, "fiber_na": .1}
        if angle is None:
            del values["pump_farfield_half_angle_mrad"]
        else:
            values["pump_farfield_half_angle_mrad"] = angle
        design = solve(values)
        candidate, = design["candidates"]
        assert not candidate["pump"]["points"]
        assert candidate["pump"]["launch_basis"] in {"invalid_second_moment", "incomplete_second_moment"}
        with pytest.raises(ValueError):
            probe_design(design, candidate, mode="pump", z_mm=20)


def test_untagged_archival_candidate_uses_original_nominal_model():
    values = {**INPUTS, "pump_source": "fiber", "fiber_core_um": 80, "fiber_na": .1}
    del values["pump_exit_waist_radius_um"], values["pump_farfield_half_angle_mrad"]
    for key in ("source_z_mm", "collimator_z_mm", "focus_lens_z_mm"):
        del values[key]
    design = solve(values)
    candidate = deepcopy(design["candidates"][0])
    for key in ("launch_basis", "launch_radius_mm", "launch_half_angle_rad"):
        del candidate["pump"][key]
    design["resolved_inputs"].update(INPUTS)
    result = analyze_design(design, candidate, mode="pump")
    assert result["estimated_M2"] == pytest.approx(math.pi * .04 * .1 / .00094)


@pytest.mark.parametrize("missing,radius,angle", [("pump_farfield_half_angle_mrad", .14, None),
                                                ("pump_exit_waist_radius_um", None, .0055)])
def test_partial_source_opens_in_a_geometry_draft_without_losing_the_known_measurement(missing, radius, angle):
    inputs = {**INPUTS, "fiber_core_um": 80, "fiber_na": .15}
    del inputs[missing]
    design = solve(inputs)
    candidate, = design["candidates"]
    draft = project_from_candidate_pump(design, candidate)
    source = draft.system.source.tangential
    assert source.radius_mm == radius and source.m2 is None
    coordinates = draft.component_context["design_coordinates"]
    assert coordinates["launch_radius_mm"] == radius and coordinates["launch_half_angle_rad"] == angle
    assert coordinates["source_basis"] == "measured_second_moment"
    samples = analyze_editable(draft)["planes"]["tangential"]["samples"]
    assert samples == ([{"z_mm": 0., "radius_mm": radius}] if radius is not None else [])

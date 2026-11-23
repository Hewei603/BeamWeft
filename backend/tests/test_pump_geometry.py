import math
from copy import deepcopy

import numpy as np
import pytest

from app.design.solver import solve_design
from app.workbench.authoring import analyze_editable
from app.workbench.pump_geometry import project_from_pump_assessment


PUMP = dict(pump_wavelength_nm=940, crystal_front_mm=9.2, crystal_length_mm=11.2, pump_index=1.71,
            pump_exit_waist_radius_um=115, pump_farfield_half_angle_mrad=5.7, source_z_mm=-114.3,
            collimator_z_mm=-71.8, focus_lens_z_mm=-23.1, collimator_f_mm=42.5, focus_f_mm=35)


def run(values, inventory=()):
    return solve_design({k: {"value": v} for k, v in values.items()}, inventory, assumption_policy="evidence_only")


def covariance_width(values, z, f1):
    # Propagate the independent position/angle second moments in (y, n*theta).
    position = values["source_z_mm"]
    transfer = np.eye(2)
    for plane, lens in ((values["collimator_z_mm"], f1), (values["focus_lens_z_mm"], values["focus_f_mm"])):
        transfer = np.array([[1, min(z, plane) - position], [0, 1]]) @ transfer
        if z <= plane:
            break
        transfer = np.array([[1, 0], [-1 / lens, 1]]) @ transfer
        position = plane
    else:
        front = values["crystal_front_mm"]
        distance = min(z, front) - position + max(0, z - front) / values["pump_index"]
        transfer = np.array([[1, distance], [0, 1]]) @ transfer
    a, b = transfer[0]
    return math.hypot(a * values["pump_exit_waist_radius_um"], b * values["pump_farfield_half_angle_mrad"]) / 1000


@pytest.mark.parametrize("from_surface,old_record", [(False, False), (True, False), (True, True)])
def test_pump_reopens_at_source_with_local_index_and_measured_width(from_surface, old_record):
    values = {**PUMP, "laser_wavelength_nm": 1030, "crystal_index": 1.65, "focus_lens_owned": True}
    if from_surface:
        del values["collimator_f_mm"]
        values.update(collimator_roc_mm=22.1, collimator_index=1.52)
    design = run(values)
    recorded = design["evidence_assessment"]["pump_imaging"]
    if old_record:
        del recorded["lenses"]
    model = project_from_pump_assessment(design)
    section, = model.observation_planes
    z_offset = model.component_context["design_coordinates"]["origin_z_mm"]
    assert model.system.mode == "single_pass" and model.system.wavelength_nm == values["pump_wavelength_nm"]
    assert z_offset == values["source_z_mm"]
    assert not any(e.kind == "mirror" or e.medium_role == "gain" for e in model.system.elements)
    assert model.system.elements[-1].uid == section.target_uid
    assert model.system.source.launch_ray.t_offset_mm is None
    assert model.system.source.sagittal is None
    if from_surface:
        assert "R/(n−1)" in model.parts[0].source
    assert not model.parts[0].locked_parameters and "focal_mm" in model.parts[1].locked_parameters
    output = analyze_editable(model)
    assert output["intensity_model"] == "width_only"
    for p in recorded["beam"]["points"]:
        assert p["w_mm"] == pytest.approx(covariance_width(values, p["z_mm"], 42.5), rel=2e-12)
    for axis in ("tangential", "sagittal"):
        plane = output["planes"][axis]
        assert plane["width_basis"] == "second_moment"
        for p in plane["samples"]:
            assert p["radius_mm"] == pytest.approx(covariance_width(values, p["z_mm"] + z_offset, 42.5), rel=2e-12)
    extracted = output["sections"]["planes"][0]
    archived = recorded["beam"]["minimum_inside_crystal"]
    assert extracted["z_mm"] + z_offset == pytest.approx(archived["z_mm"])
    assert extracted["beams"][0]["index"] == values["pump_index"]
    assert extracted["beams"][0]["radii_mm"]["tangential"] == pytest.approx(archived["w_mm"])
    assert extracted["beams"][0]["field_axes"]["tangential"]["center_mm"] is None
    assert sum(e.length_mm for e in model.system.elements) + z_offset == pytest.approx(20.4)


def test_magnification_alone_is_not_an_editable_measured_profile():
    values = {**PUMP, "fiber_na": .2, "fiber_core_um": 75, "pump_source": "fiber"}
    del values["pump_exit_waist_radius_um"]
    del values["pump_farfield_half_angle_mrad"]
    design = run(values)
    assert design["evidence_assessment"]["pump_imaging"]["status"] == "conditional"
    geometry = project_from_pump_assessment(design)
    assert geometry.system.source.tangential.radius_mm is None
    assert geometry.system.source.tangential.m2 is None
    assert not analyze_editable(geometry)["planes"]["tangential"].get("output")
    del values["source_z_mm"]
    with pytest.raises(ValueError, match="位置"):
        project_from_pump_assessment(run(values))
    complete = run(PUMP)
    damaged = deepcopy(complete)
    del damaged["resolved_inputs"]["pump_index"]
    with pytest.raises(ValueError, match="折射率"):
        project_from_pump_assessment(damaged)
    complete["evidence_assessment"]["pump_imaging"]["beam"]["m2"] += .01
    with pytest.raises(ValueError, match="M²"):
        project_from_pump_assessment(complete)


def test_ideal_beam_on_floating_point_boundary_and_bound_stock():
    values = {**PUMP, "pump_farfield_half_angle_mrad": PUMP["pump_wavelength_nm"] / (math.pi * PUMP["pump_exit_waist_radius_um"]),
              "collimator_inventory_id": 83}
    del values["collimator_f_mm"]
    model = project_from_pump_assessment(run(values, [{"id": 83, "category": "lens", "condition": "ok",
        "available_quantity": 1, "name": "准直光学件", "focal_length_mm": 42.5}]))
    assert model.system.source.tangential.m2 == pytest.approx(1.)
    assert model.system.source.tangential.width_basis() == "second_moment"
    assert model.parts[0].ownership == "owned" and model.parts[0].inventory_id == 83
    assert "focal_mm" in model.parts[0].locked_parameters

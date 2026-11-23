import math

import numpy as np
import pytest

from app.workbench.ray_tables import RayTable, assemble_ray_bundle


def source(**updates):
    return assemble_ray_bundle(RayTable.model_validate({
        "plane": "entrance", "basis": "Forward T/S axes in a homogeneous window",
        "medium_n": 1.5, "x_mm": [1, 2], "y_mm": [3, 4], "directions": "slopes",
        "direction_x": [.03, -.01], "direction_y": [-.04, .02], "weighting": "equal_flux",
        **updates,
    }))


def test_reference_plane_preserves_joint_rows_index_and_relative_flux():
    raw = dict(z_mm=[-2, 3], reference_z_mm=0, weighting="radiant_power", power_w=[.07, .03],
               ray_wavelength_nm=[915, 915])
    expected = [[1.06, 2.92, .045, -.06, .7], [2.03, 3.94, -.015, .03, .3]]
    slopes = source(**raw)
    cosines = np.array([[.03, -.04, 1], [-.01, .02, 1]])
    cosines /= np.linalg.norm(cosines, axis=1)[:, None]
    a, b, c = cosines.T.tolist()
    directions = source(**raw, directions="direction_cosines", direction_x=a, direction_y=b, direction_z=c)
    for result in (slopes, directions):
        assert result["wavelength_nm"] == 915
        assert result["launched_power_w"] == pytest.approx(.1)
        np.testing.assert_allclose(result["bundle"]["rays"], expected, atol=1e-12)
    rel = source(weighting="relative_flux", relative_flux=[2, 0])
    assert "launched_power_w" not in rel
    assert [r[-1] for r in rel["bundle"]["rays"]] == [1, 0]
    assert math.fsum(r[-1] for r in source()["bundle"]["rays"]) == 1


@pytest.mark.parametrize("fields", [
    {"direction_x": [.03]}, {"direction_z": [1, 1]}, {"z_mm": [1, 2]},
    {"directions": "direction_cosines", "direction_z": [-1, 1]},
    {"directions": "direction_cosines", "direction_z": [.6, .6]},
    {"weighting": "relative_flux", "relative_flux": [0, 0]},
    {"weighting": "radiant_power", "power_w": [3, -1]}, {"relative_flux": [1, 1]},
    {"wavelength_nm": 780, "ray_wavelength_nm": [780, 785]},
])
def test_incompatible_or_unphysical_rows_are_explained(fields):
    with pytest.raises(ValueError):
        source(**fields)

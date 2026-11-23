"""Independent finite-area, coordinate and provenance checks for map comparisons."""

import math

import numpy as np
import pytest
from pydantic import ValidationError

from app.physics.raster_overlap import detector_integrals
from app.workbench.intensity_map import IntensityMap
from app.workbench.map_comparison import MapComparison, compare_intensity_maps


def image(x, y, masses, **kwargs):
    return IntensityMap(
        x_edges_mm=x,
        y_edges_mm=y,
        values=np.array(masses).tolist(),
        signal_unit="ADU",
        x_label="column",
        y_label="row",
        basis="Synthetic integral in each detector bin.",
        **kwargs,
    )


def compare(first, second, **changes):
    spec = {
        "reference": first,
        "observed": second,
        "offset_x_mm": 0,
        "offset_y_mm": 0,
        "rotation_deg": 0,
        "flip_y": False,
        "registration_basis": "Independent bench fiducials at the same sample plane.",
        **changes,
    }
    return compare_intensity_maps(MapComparison(**spec))


def test_unequal_pixels_integrate_before_normalizing_and_report_calibrated_signal():
    # Independent reference densities 1, 2, 5 over a 3×1 chip, observed bins at half-pixel offsets.
    ref = image([0, 1, 2, 3], [0, 1], [[1, 2, 5]])
    obs = image([0, 0.5, 2, 2.5], [0, 1], [[5, 30, 25]], y_down=True)
    out = compare(
        ref,
        obs,
        offset_x_mm=0.5,
        observed_units_per_reference_unit=10,
        signal_calibration_basis="Independent ADU calibration.",
    )
    # Expected overlap sums are [.5, 4.5, 2.5], total 7.5; no centroid fit or peak scaling.
    assert out["common_signal"]["reference"] == pytest.approx(7.5)
    assert out["calibrated_signal_ratio"] == pytest.approx(0.8)
    assert out["coverage_reference"] == pytest.approx(2.5 / 3)
    assert out["grid"]["y_down"]
    ref_mass = np.array([0.5, 4.5, 2.5]) / 7.5
    obs_mass = np.array([5.0, 30, 25]) / 60
    widths = np.array([0.5, 1.5, 0.5])
    np.testing.assert_allclose(out["grid"]["residual"][0], (obs_mass - ref_mass) / widths)
    assert out["relative_l1"] == pytest.approx(np.abs(obs_mass - ref_mass).sum())
    assert out["rmse_density_mm2"] == pytest.approx(np.sqrt(np.sum((obs_mass - ref_mass) ** 2 / widths) / 2.5))


@pytest.mark.parametrize("angle", [35.0, -35.0, 145.0, 215.0, 0.0, 90.0, 180.0, 270.0])
@pytest.mark.parametrize("flip", [True, False])
def test_half_plane_step_integral_is_exact_for_rotated_and_reflected_detectors(angle, flip):
    # A uniformly illuminated rectangle with a vertical step from density 1 to 3.
    # Its x-coordinate after rotation is the sum of two independent uniforms.
    # The positive-part quadratic formula is the analytic convolution CDF.
    rx, ry = np.array([-4.0, 0, 4]), np.array([-4.0, 4])
    mass = np.array([[32.0, 96.0]])
    x, y = np.array([-0.73, -0.19, 0.32, 1.26]), np.array([-0.84, -0.32, 0.47, 0.81])
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle)) * (-1 if flip else 1)
    m, valid = detector_integrals(rx, ry, mass, x, y, 0.17, -0.31, angle, flip)
    assert valid.all()
    for row, (bottom, top) in enumerate(zip(y, y[1:])):
        for col, (left, right) in enumerate(zip(x, x[1:])):
            a, b = sorted((c * left, c * right))
            d, e = sorted((-s * bottom, -s * top))
            h, w, z = b - a, e - d, -0.17 - a - d
            if min(h, w) < 1e-12:
                fraction_left = np.clip(z / max(h, w), 0, 1)
            else:
                fraction_left = sum(
                    k * max(0, v) ** 2 for v, k in ((z, 1), (z - h, -1), (z - w, -1), (z - w - h, 1))
                ) / (2 * h * w)
            ideal = (3 - 2 * fraction_left) * (right - left) * (top - bottom)
            assert m[row, col] == pytest.approx(ideal, abs=1e-13)


def test_nonrectangular_support_masks_all_three_displays_and_retains_negative_noise():
    ref = image([-1, 0, 1], [-1, 0, 1], [[1, 1], [1, 1]])
    vals = np.ones((5, 5))
    vals[2, 2] = -0.5
    obs = image(np.linspace(-1, 1, 6), np.linspace(-1, 1, 6), vals, warnings=["camera calibration uncertain"])
    out = compare(ref, obs, rotation_deg=45)
    assert 1 < out["cells_compared"] < vals.size
    assert out["grid"]["observed"][2][2] < 0
    for name in ("observed", "reference", "residual"):
        grid = out["grid"][name]
        assert grid[0][0] is None and sum(v is not None for row in grid for v in row) == out["cells_compared"]
    assert "camera calibration uncertain" in " ".join(out["warnings"])
    assert out["status"] == "completed" and out["coverage_state"] == "partial_observation"
    assert "calibrated_signal_ratio" not in out


def test_declared_conditions_and_quality_limitations_are_independent_of_wording():
    ref = image([0, 1, 2], [0, 1], [[1, 2]], quality="usable")
    values = dict(quality="conditional", warnings=["fixture note in any language"])
    usable = compare(ref, image([0, 1, 2], [0, 1], [[1, 2]], **values))
    assert usable["status"] == "completed" and not usable["limitations"]
    assert usable["source_quality"] == {"reference": "usable", "observed": "conditional"}
    limited = compare(ref, image([0, 1, 2], [0, 1], [[1, 2]], **{**values, "quality": "limited"}))
    assert limited["status"] == "partial" and limited["limitations"]
    assert limited["relative_l1"] == usable["relative_l1"]
    one = compare(ref, image([0, 2], [0, 1], [[1]]))
    assert one["status"] == "partial" and one["cells_compared"] == 1
    old = ref.model_dump(exclude={"quality"})
    historical = compare(old, ref)
    assert historical["source_quality"] == {"reference": "unassessed", "observed": "usable"}


def test_known_translation_remains_observable_and_incompatible_windows_or_grids_are_explained():
    e = [-2, -1, 0, 1, 2]
    ref = image(e, [0, 1], [[1, 4, 2, 1]])
    shifted = image(e, [0, 1], [[4, 2, 1, 0]])
    assert compare(ref, shifted)["relative_l1"] > 0.5
    assert compare(ref, shifted, offset_x_mm=1)["relative_l1"] == 0
    with pytest.raises(ValueError, match="完整观测单元"):
        compare(ref, shifted, offset_y_mm=20)
    with pytest.raises(ValueError, match="积分信号"):
        compare(ref, image(e, [0, 1], [[-1, 1, 2, -2]]))
    with pytest.raises(ValidationError):
        image(e, [0, 1], [[1, 2]])
    with pytest.raises(ValidationError):
        image([0, 1, 1], [0, 1], [[3, 4]])
    with pytest.raises(ValidationError):
        compare(ref, ref, observed_units_per_reference_unit=1)


def test_signed_centroid_uses_the_same_observation_axes_after_exchange_and_mirror():
    ref = image([0, 1, 2], [0, 1, 2], [[1, 2], [3, 4]])
    # 90 degrees and a y reflection exchange x and y. The prediction in observation order is [[1,3],[2,4]].
    obs = image([0, 1, 2], [0, 1, 2], [[-1, 5], [2, 4]])
    out = compare(ref, obs, rotation_deg=90, flip_y=True)
    assert out["centroids"]["reference"] == pytest.approx({"x_mm": 1.2, "y_mm": 1.1})
    assert out["centroids"]["observed"] == pytest.approx({"x_mm": 1.4, "y_mm": 1.1})
    assert out["centroid_dx_mm"] == pytest.approx(0.2) and out["centroid_dy_mm"] == pytest.approx(0)
    unstable = compare(ref, image([0, 1, 2], [0, 1, 2], [[-100, 101], [0, 0]]))
    assert unstable["centroids"]["observed"]["x_mm"] > 2
    assert any("一阶矩" in msg for msg in unstable["warnings"])


def test_arbitrarily_rotated_2d_pattern_against_independent_vertical_slice_quadrature():
    from scipy.integrate import quad

    ref_x, ref_y = np.array([-3.0, -0.8, 0.25, 0.93, 3.0]), np.array([-3.0, -0.62, 0.73, 3.0])
    obs_x, obs_y = np.array([-1.2, -0.3, 0.57, 1.3]), np.array([-1.0, -0.1, 0.4, 1.45])
    density = np.array([[0.4, 2, 3, 0], [4, -0.5, 7, 0.4], [1, 0, 0.5, 3]])
    area = np.outer(np.diff(ref_y), np.diff(ref_x))
    frame = np.array([0.33, -0.12])
    angle = np.deg2rad(31)
    matrix = np.array([[np.cos(angle), np.sin(angle)], [np.sin(angle), -np.cos(angle)]])
    projected, valid = detector_integrals(ref_x, ref_y, density * area, obs_x, obs_y, *frame, 31, True)
    assert valid.all()
    for j, (lo, hi) in enumerate(zip(obs_y, obs_y[1:])):
        for i, (left, right) in enumerate(zip(obs_x, obs_x[1:])):
            polygon = np.array([[left, lo], [right, lo], [right, hi], [left, hi]]) @ matrix.T + frame
            segments = list(zip(polygon, np.roll(polygon, -1, axis=0)))
            xs = list(polygon[:, 0]) + list(ref_x)
            for a, b in segments:
                for h in ref_y:
                    t = (h - a[1]) / (b[1] - a[1])
                    if 0 < t < 1:
                        xs.append(a[0] + t * (b[0] - a[0]))

            def scan_line(x):
                ys = [
                    a[1] + (x - a[0]) / (b[0] - a[0]) * (b[1] - a[1])
                    for a, b in segments
                    if min(a[0], b[0]) < x < max(a[0], b[0])
                ]
                if not ys:
                    return 0.0
                heights = np.maximum(0, np.minimum(max(ys), ref_y[1:]) - np.maximum(min(ys), ref_y[:-1]))
                return heights @ density[:, np.searchsorted(ref_x, x) - 1]

            xmin, xmax = polygon[:, 0].min(), polygon[:, 0].max()
            integral = quad(scan_line, xmin, xmax, points=[v for v in xs if xmin < v < xmax], epsabs=1e-12)[0]
            assert projected[j, i] == pytest.approx(integral, abs=1e-12)


def test_network_integrated_watts_can_be_compared_to_independently_scaled_counts(client):
    root = f"/api/cases/{client.post('/api/cases', json={'title': 'power at detector'}).json()['id']}/studies"
    name = "spatial_port_field"
    method = client.get(root + "/catalog/" + name).json()
    request = {
        "provider": method["provider"],
        "version": method["version"],
        "inputs": {
            "detector": {"component": "bs", "port": "3"},
            "window_mm": 3,
            "samples": 64,
            "layout": {
                "system": {"mode": "single_pass", "wavelength_nm": 1000, "elements": []},
                "network": {
                    "components": [{"uid": "bs", "name": "BS", "model": {"kind": "coupler", "through_fraction": 0.5}}],
                    "drives": [
                        {
                            "at": {"component": "bs", "port": "1"},
                            "power_w": 2,
                            "profile": {
                                "kind": "gaussian",
                                "tangential": {"radius_mm": 0.5, "wavefront": "waist", "m2": 1},
                            },
                        }
                    ],
                },
            },
        },
    }
    saved = client.post(root + "/run/" + name, json=request)
    assert saved.status_code == 200, saved.text
    result = saved.json()
    power = IntensityMap.model_validate(result["intensity_map"])
    assert power.signal_unit == "W" and power.plane == "bs / 3"
    assert np.asarray(power.values).sum() == pytest.approx(result["window_w"], rel=1e-13)
    assert result["detected_w"] == pytest.approx(1.0, rel=1e-8)
    counts = image(power.x_edges_mm, power.y_edges_mm, 200 * np.array(power.values))
    calibrated = compare(
        power,
        counts,
        observed_units_per_reference_unit=200,
        signal_calibration_basis="Independent calibration 200 ADU/W at this exposure.",
    )
    assert calibrated["relative_l1"] == pytest.approx(0, abs=1e-14)
    assert calibrated["calibrated_signal_ratio"] == pytest.approx(1, abs=1e-14)


def test_frozen_field_sampler_publishes_full_resolution_and_original_direct_map_stays_cheap(client):
    root = f"/api/cases/{client.post('/api/cases', json={'title': 'frozen image registration'}).json()['id']}/studies"
    wave = "propagate_coherent_field"
    definition = client.get(root + "/catalog/" + wave).json()
    req = {
        "provider": definition["provider"],
        "version": definition["version"],
        "inputs": {
            "window_mm": 2,
            "samples": 128,
            "layout": {
                "system": {
                    "wavelength_nm": 780,
                    "mode": "single_pass",
                    "input_radius_mm": 0.28,
                    "elements": [{"name": "camera", "uid": "p", "kind": "space", "length_mm": 20}],
                }
            },
        },
    }
    preview = client.post(root + "/preview/" + wave, json=req)
    assert preview.status_code == 200 and "intensity_map" not in preview.json()
    res = client.post(root + "/run/" + wave, json=req)
    assert res.status_code == 200, res.text
    direct = res.json()
    source = direct["study_record"]["id"]
    assert direct["study_record"]["available_outputs"]["intensity_map"]["shape"] == "state"
    m = IntensityMap.model_validate(direct["intensity_map"])
    assert np.asarray(m.values).sum() == pytest.approx(direct["window_power"], rel=1e-14)
    sample = client.post(
        root + "/run/sample_coherent_intensity_map",
        json={
            "provider": definition["provider"],
            "version": "1",
            "inputs": {"window_mm": 2, "samples": 256},
            "connections": {
                "field": {"run_id": source, "port": "exit_field", "registration": "在保存的出射面用相同 T/S 方向取样。"}
            },
        },
    )
    assert sample.status_code == 200, sample.text
    finer = sample.json()
    assert len(finer["intensity_map"]["values"]) == 256 and len(finer["grid"]["density"]) == 128
    assert np.asarray(finer["intensity_map"]["values"]).sum() == pytest.approx(finer["window_power"], rel=1e-14)
    out = client.post(
        root + "/run/compare_intensity_maps",
        json={
            "provider": definition["provider"],
            "version": "1",
            "inputs": {
                "offset_x_mm": 0,
                "offset_y_mm": 0,
                "rotation_deg": 0,
                "flip_y": False,
                "registration_basis": "同一参考面与 T/S 坐标",
            },
            "connections": {
                k: {"run_id": rid, "port": "intensity_map", "registration": "同一参考面与 T/S 坐标"}
                for k, rid in (("reference", finer["study_record"]["id"]), ("observed", source))
            },
        },
    )
    assert out.status_code == 200, out.text
    assert out.json()["cells_compared"] == 128**2 and out.json()["relative_l1"] < 0.001

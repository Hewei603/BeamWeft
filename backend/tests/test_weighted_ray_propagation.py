"""Independent optics examples for finite-source ray transfer, including information-limited cases."""

import math

import numpy as np
import pytest

from app.workbench.ray_study import RAY_STUDY, TraceRays, propagate_ray_bundle


def element(kind, uid, **settings):
    return {"kind": kind, "uid": uid, "name": uid, **settings}


def layout(*elements, **other):
    return {"system": {"mode": "single_pass", "wavelength_nm": 780, "elements": list(elements)}, **other}


def bundle(rows, **extra):
    return {
        "wavelength_nm": 780,
        "plane": "input",
        "basis": "registered detector",
        "medium_n": 1.0,
        "quality": "documented",
        "rays": rows,
        **extra,
    }


def support(**extra):
    return {
        "wavelength_nm": 780,
        "plane": "input",
        "basis": "mechanical limits",
        "medium_n": 1.0,
        "x_mm": [-1, 1],
        "y_mm": [-0.5, 0.5],
        "p_t": [-0.007, 0.008],
        "p_s": [-0.002, 0.001],
        **extra,
    }


def traced(project, **options):
    return propagate_ray_bundle(TraceRays(layout=project, **options))


def test_full_two_f_relay_retains_coordinate_direction_correlation_and_never_clips_at_the_plot():
    f = 25.0
    relay = layout(
        element("space", "object distance", length_mm=2 * f),
        element("lens", "lens", focal_mm=f),
        element("space", "image distance", length_mm=2 * f),
    )
    before = np.array([[0.3, -0.2, 0.012, 0.005, 0.4], [-0.2, 0.6, -0.004, 0.002, 0.6]])
    output = traced(relay, input_bundle=bundle(before.tolist()), window_mm=0.001, samples=1)
    actual = np.array(output["exit_bundle"]["rays"])
    np.testing.assert_allclose(actual[:, :2], -before[:, :2], atol=1e-12)
    np.testing.assert_allclose(actual[:, 2:4], -before[:, :2] / f - before[:, 2:4], atol=1e-12)
    np.testing.assert_allclose(actual[:, 4], before[:, 4])
    assert output["window_power"] == 0 and output["throughput"] == 1
    assert np.sum(output["intensity_map"]["values"]) == 0
    bound = traced(relay, input_support=support())
    assert bound["throughput_min"] == bound["throughput_max"] == 1
    assert bound["exit_support"]["x_mm"] == [-1, 1]
    assert bound["exit_support"]["y_mm"] == [-0.5, 0.5]
    assert not {"grid", "intensity_map", "exit_bundle", "throughput"} & bound.keys()


def test_known_collimated_population_reaches_geometric_minimum_before_detector():
    project = layout(element("lens", "fifty", focal_mm=50), element("space", "rail", length_mm=80))
    rows = [[-0.5, 0.5, 0, 0, 0.5], [0.5, -0.5, 0, 0, 0.5]]
    full = traced(project, input_bundle=bundle(rows))
    assert full["segment_minima"][0]["z_mm"] == pytest.approx(50)
    assert full["segment_minima"][0]["rms_radius_mm"] == pytest.approx(0, abs=1e-13)
    assert full["diameter_4sigma_t_mm"] == pytest.approx(1.2)
    assert full["rows"][0]["min_rms_z_mm"] == pytest.approx(50)


def test_two_batches_walk_through_a_rotated_stop_and_continue_the_original_flux():
    stop = element(
        "space",
        "stop",
        length_mm=10,
        n=2,
        clear_aperture={"shape": "rectangle", "width_mm": 2, "height_mm": 0.2, "angle_deg": 90},
    )
    plane = {"name": "embedded", "uid": "camera", "target_uid": "stop", "depth_mm": 4}
    project = layout(stop, observation_planes=[plane])
    rays = [[0, 0.8, 0.02, 0, 0.25], [0.5, 0, 0, 0, 0.25], [0, -0.9, 0, 0.01, 0.5]]
    first = traced(project, input_bundle=bundle(rays, replicate_split=2), plane_uid="camera", window_mm=4)
    assert first["throughput"] == 0.75 and first["window_power"] == 0.75
    assert first["exit_bundle"]["medium_n"] == 2 and first["z_mm"] == 4
    assert first["exit_bundle"]["replicate_split"] == 1
    assert first["replicate_throughput_difference"] == 0.5
    np.testing.assert_allclose(
        first["exit_bundle"]["rays"], [[0.04, 0.8, 0.02, 0, 0.25], [0, -0.88, 0, 0.01, 0.5]]
    )
    assert np.sum(first["intensity_map"]["values"]) == 0.75
    second = traced(layout(element("space", "other", length_mm=6, n=2)), input_bundle=first["exit_bundle"])
    assert second["throughput"] == 0.75
    np.testing.assert_allclose(
        second["exit_bundle"]["rays"], [[0.1, 0.8, 0.02, 0, 0.25], [0, -0.85, 0, 0.01, 0.5]]
    )


@pytest.mark.parametrize("width,offset,expected", [(6, 0, [1, 1]), (0.1, 0, [0, 1]), (0.2, 3, [0, 0])])
def test_only_geometrically_certified_bounds_are_reported(width, offset, expected):
    aperture = {"shape": "ellipse", "width_mm": width, "center_t_mm": offset, "angle_deg": 33}
    project = layout(element("space", "stop", length_mm=1, clear_aperture=aperture))
    got = traced(project, input_support=support(throughput_bounds=(0.2, 0.7)))
    lo, hi = expected
    assert [got["throughput_min"], got["throughput_max"]] == [0.2 * lo, 0.7 * hi]
    assert ("exit_support" in got) == bool(hi)
    assert "window_power" not in got


def test_joint_receiver_gate_distinguishes_equal_marginals_and_does_not_modify_the_exit():
    # Both sources have identical spatial and angular histograms. Only their joint correlation differs.
    settings = {"receiver_opening": {"shape": "ellipse", "width_mm": 0.2}, "receiver_na": 0.02}
    project = layout(element("space", "reference", length_mm=0))
    a = bundle([[0, 0, 0.01, 0, 0.5], [1, 0, 0.08, 0, 0.5]])
    b = bundle([[0, 0, 0.08, 0, 0.5], [1, 0, 0.01, 0, 0.5]])
    passed, missed = [traced(project, input_bundle=state, **settings) for state in (a, b)]
    assert passed["accepted_fraction"] == 0.5 and missed["accepted_fraction"] == 0
    assert passed["throughput"] == missed["throughput"] == 1
    assert passed["exit_bundle"]["rays"] == a["rays"]
    assert traced(project, input_support=support(), **settings)["accepted_min"] == 0
    assert "accepted_w" not in passed
    radiometric = traced(project, input_bundle=a, launched_power_w=3.4, **settings)
    assert radiometric["transmitted_w"] == 3.4 and radiometric["accepted_w"] == 1.7
    bounded = traced(
        project, input_support=support(throughput_bounds=(0.2, 0.7)), launched_power_w=10, **settings
    )
    assert bounded["transmitted_w_bounds"] == [2, 7] and bounded["accepted_w_bounds"] == [0, 7]
    assert "transmitted_w" not in bounded and "accepted_w" not in bounded
    with pytest.raises(ValueError, match="折射率"):
        traced(project, input_bundle=a, receiver_na=1.5)


def test_receiver_angle_uses_sine_in_the_registered_medium_and_handles_an_empty_population():
    project = layout(
        element("space", "glass", length_mm=2, n=1.6),
        observation_planes=[{"uid": "embedded", "target_uid": "glass", "name": "in glass", "depth_mm": 1}],
    )
    src = bundle([[0, 0, 1.6 * 0.12, 1.6 * 0.16, 1.0]])
    # n*tan(theta)=.32, n*sin(theta)=.31379…; a 0.315-NA receiver transmits this ray.
    out = traced(project, input_bundle=src, plane_uid="embedded", receiver_na=0.315)
    assert out["accepted_fraction"] == 1 and out["exit_bundle"]["medium_n"] == 1.6
    assert out["quality"] == "limited"
    assert (
        traced(project, input_bundle=src, plane_uid="embedded", receiver_na=0.312)["accepted_fraction"] == 0
    )
    blank = traced(project, input_bundle=bundle([], replicate_split=0), receiver_na=0)
    assert blank["throughput"] == blank["accepted_fraction"] == blank["window_power"] == 0
    assert blank["exit_bundle"]["rays"] == []
    assert "center_t_mm" not in blank and len(blank["grid"]["density"]) == 32


def test_incident_arm_uses_its_wavelength_and_medium_before_reaching_a_station():
    host = element("space", "sample", length_mm=30, n=1.5)
    project = layout(
        element("space", "lead", length_mm=20),
        host,
        incident_paths=[
            {
                "uid": "arm",
                "name": "green",
                "target_uid": "sample",
                "through_uid": "sample",
                "target_n": 2,
                "wavelength_nm": 532,
                "elements": [element("space", "private", length_mm=7)],
            }
        ],
        observation_planes=[{"uid": "station", "name": "internal", "target_uid": "sample", "depth_mm": 4}],
    )
    options = {"plane_uid": "station", "beam_uid": "arm"}
    got = traced(project, input_bundle=bundle([[0.3, 0, 0.08, 0, 1]], wavelength_nm=532), **options)
    assert (got["source_z_mm"], got["z_mm"], got["wavelength_nm"]) == (13, 24, 532)
    assert got["center_t_mm"] == pytest.approx(0.3 + 0.08 * 7 + 0.08 / 2 * 4)
    with pytest.raises(ValueError, match="波长"):
        traced(project, input_bundle=bundle([[0, 0, 0, 0, 1]]), **options)


def test_registered_astigmatic_and_grin_are_transferred_at_reference_faces():
    from app.workbench.device_laws import device_catalog

    prescription = device_catalog()[0]["aligned_lens_power"]["prescription"]
    prescription = {**prescription, "parameters": {"tangential_dpt": 20, "sagittal_dpt": 10}}
    g = 0.025
    project = layout(
        element("registered", "lens", prescription=prescription),
        element("grin", "rod", n=1.5, gradient_per_mm=g, length_mm=math.pi / 2 / g),
    )
    ray = bundle([[0.8, 0.4, 0, 0, 1.0]])
    actual = np.array(traced(project, input_bundle=ray)["exit_bundle"]["rays"][0])
    np.testing.assert_allclose(
        actual[:4],
        [-0.8 / 50 / (1.5 * g), -0.4 / 100 / (1.5 * g), -1.5 * g * 0.8, -1.5 * g * 0.4],
        atol=1e-12,
    )


def test_production_api_preview_and_save_bind_same_storage_and_clip_just_the_selected_prefix(client):
    case = client.post("/api/cases", json={"title": "Lens label unreadable beyond sensor"}).json()
    root = f"/api/cases/{case['id']}"
    ray = bundle([[0, 0, 0.01, 0, 1.0]])
    project = layout(
        element("space", "known", length_mm=80, clear_aperture={"shape": "ellipse", "width_mm": 4}),
        element("thick_lens", "unread", length_mm=4, front_radius_mm=35),
        observation_planes=[{"uid": "cam", "name": "camera", "target_uid": "known", "depth_mm": 5}],
    )
    response = client.post(
        root + "/optical-project",
        json={"project": {"title": "Registered sensor", **project}, "change_note": "Registered path"},
    )
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["analysis"]["input_issues"]
    port = "layout-prefix-v1/cam/primary/layout"
    base = {"version": "1", "provider": "LaserClaw", "inputs": {"input_bundle": ray}}
    pr = client.post(
        root + "/studies/preview/propagate_ray_bundle?prefix_plane_uid=cam&prefix_path_uid=primary",
        json={**base, "inputs": {**base["inputs"], "layout": project}},
    )
    assert pr.status_code == 200, pr.text
    rr = client.post(
        root + "/studies/run/propagate_ray_bundle",
        json={
            **base,
            "connections": {
                "layout": {
                    "revision_id": saved["revision_id"],
                    "port": port,
                    "registration": "Registered sensor plane.",
                }
            },
        },
    )
    assert rr.status_code == 200, rr.text
    preview, receipt = pr.json(), rr.json()
    assert preview["throughput"] == receipt["throughput"] and receipt["center_t_mm"] == pytest.approx(0.05)
    assert preview["grid"] == receipt["grid"] and "exit_bundle" not in preview
    assert receipt["exit_bundle"]["rays"] == [[0.05, 0, 0.01, 0, 1]]
    assert receipt["study_record"]["connections"]["layout"]["port"] == port
    assert len([r for r in receipt["rows"] if "before" in r]) == 1
    assert RAY_STUDY.layout_prefix_inputs == {"path": "beam_uid", "plane": "plane_uid"}


def test_archived_flux_table_connects_both_joint_samples_and_original_absolute_power(client):
    case = client.post("/api/cases", json={"title": "Radiometric relay"}).json()
    root = f"/api/cases/{case['id']}/studies"
    table = {
        "plane": "exit A",
        "basis": "same calibrated ray set",
        "medium_n": 1,
        "wavelength_nm": 780,
        "quality": "documented",
        "x_mm": [0, 0.8],
        "y_mm": [0, 0],
        "directions": "slopes",
        "direction_x": [0, 0],
        "direction_y": [0, 0],
        "weighting": "radiant_power",
        "power_w": [0.15, 0.55],
    }
    base = {"version": "1", "provider": "LaserClaw"}
    issued = client.post(root + "/run/assemble_ray_bundle", json={**base, "inputs": table})
    assert issued.status_code == 200, issued.text
    run = issued.json()["study_record"]["id"]
    connections = {
        key: {
            "run_id": run,
            "port": port,
            "registration": "Same launch plane, same calibrated parent flux, +T/S.",
        }
        for key, port in (("input_bundle", "bundle"), ("launched_power_w", "launched_power_w"))
    }
    inp = {
        "layout": layout(element("space", "screen", length_mm=1)),
        "receiver_opening": {"shape": "ellipse", "width_mm": 0.4},
    }
    response = client.post(
        root + "/run/propagate_ray_bundle", json={**base, "inputs": inp, "connections": connections}
    )
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["transmitted_w"] == pytest.approx(0.7) and out["accepted_w"] == pytest.approx(0.15)
    assert out["accepted_fraction"] == pytest.approx(0.15 / 0.7)
    assert set(out["study_record"]["connections"]) == {"input_bundle", "launched_power_w"}
    ports = out["study_record"]["available_outputs"]
    assert ports["accepted_w"]["label"] != ports["transmitted_w"]["label"]
    assert ports["accepted_w"]["quantity"] == ports["transmitted_w"]["quantity"] == "optics.power"
    response = client.get(root + f"/runs/{out['study_record']['id']}/searches")
    assert response.status_code == 200, response.text
    options = response.json()["measures"]
    assert options["accepted_w"] == {"label": "满足接收器条件的模型接收功率", "unit": "W"}
    assert options["transmitted_w"] == {"label": "到达观察面的模型透过功率", "unit": "W"}
    for axis in ("t", "s"):
        assert options[f"diameter_4sigma_{axis}_mm"] == {"label": f"{axis.upper()} 轴 4σ 全宽", "unit": "mm"}

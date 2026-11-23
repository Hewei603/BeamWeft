from io import BytesIO

import numpy as np
import pytest

from app.workbench.centroid_comparison import CenterComparison, compare_centers
from app.workbench.centroid_state import station_centers


def test_exact_window_centroid_through_frozen_station_and_fitted_registration(client):
    cid = client.post("/api/cases", json={"title": "相机坐标与工程基准"}).json()["id"]
    root = f"/api/cases/{cid}"

    def run(name, inputs, connections=None):
        definition = client.get(root + "/studies/catalog/" + name).json()
        result = client.post(
            root + "/studies/run/" + name,
            json={
                "inputs": inputs,
                "version": definition["version"],
                "provider": definition["provider"],
                "connections": connections or {},
            },
        )
        assert result.is_success, result.text
        return result.json()

    frame = np.zeros((12, 600), dtype=np.uint16)
    frame[[4, 6], 305] = 100
    raw = BytesIO()
    np.save(raw, frame)
    image = client.post(root + "/attachments", files={"file": ("camera.npy", raw.getvalue())}).json()
    capture = run(
        "measure_image_moments",
        {
            "image": {"attachment_id": image["id"]},
            "window": [5, 1, 513, 10],
            "pitch_x_um": 10,
            "pitch_y_um": 20,
            "response": "linear",
            "basis": "独立核实物方节距和完整光斑，行号向下。",
            "background_level": 0,
            "saturation_level": 4095,
            "wavelength_nm": 780,
            "window_coverage": "contains_beam",
        },
    )
    assert capture["centroid"]["pixel_center"] == [305, 5]
    assert [capture["centroid"]["x_mm"], capture["centroid"]["y_mm"]] == pytest.approx([3.05, 0.1])
    assert np.array(capture["centroid"]["covariance_mm2"]) == pytest.approx(np.diag([0, 0.0004]))
    # The occupied display bin is centered one original pixel away from the exact centroid.
    data = capture["intensity_map"]
    col = np.asarray(data["values"]).sum(axis=0).argmax()
    assert np.mean(data["x_edges_mm"][col : col + 2]) == pytest.approx(3.06)
    assert capture["centroid"]["quality"] == "usable"

    registration = run(
        "fit_transverse_registration",
        {
            "observed_x_mm": [0, 2, 0, 1],
            "observed_y_mm": [0, 0, 1, 1],
            "reference_x_mm": [0.2, 0.2, 1.2, 1.2],
            "reference_y_mm": [-3, -1, -3, -2],
            "check_rows": [4],
            "max_error_mm": 0.001,
            "basis": "独立参考标记",
        },
    )
    plane = {"uid": "target/百分%", "name": "接收面", "target_uid": "medium", "depth_mm": 20}
    source = {
        "tangential": {"radius_mm": 0.3, "wavefront": "waist"},
        "launch_ray": {"t_offset_mm": 0.2, "s_offset_mm": 0.05, "t_angle_mrad": 0, "s_angle_mrad": 0},
    }
    project = {
        "title": "平面基准",
        "system": {
            "mode": "single_pass",
            "wavelength_nm": 780,
            "source": source,
            "elements": [{"uid": "medium", "kind": "space", "name": "空气", "length_mm": 20}],
        },
        "observation_planes": [plane],
    }

    def save(base=None):
        response = client.post(
            root + "/optical-project",
            json={
                "project": project,
                "base_revision_id": base,
                "change_note": "录入入射位置与指向",
            },
        )
        assert response.is_success, response.text
        return response.json()["revision_id"]

    first = save()
    offers = client.get(root + "/studies/project-outputs", params={"revision_id": first}).json()["outputs"]
    ((port, item),) = [
        (key, value)
        for key, value in offers.items()
        if value["port"]["quantity"] == "optics.station-centroid.v1" and value["plane_uid"] == plane["uid"]
    ]
    assert "%2F" in port and item["path_uid"] == "primary" and "value" not in item
    note = "物方标记与探测器同面，使用该冻结版本的平面和主光束。"
    links = {
        "observed": {"run_id": capture["study_record"]["id"], "port": "centroid", "registration": note},
        "reference": {"revision_id": first, "port": port, "registration": note},
        "registration": {"run_id": registration["study_record"]["id"], "port": "registration", "registration": note},
    }
    complete = run("compare_transverse_centers", {}, links)
    assert complete["status"] == "completed" and complete["comparison_quality"] == "usable"
    assert complete["observed_mm"] == pytest.approx({"tangential": 0.3, "sagittal": 0.05})
    assert complete["residual_mm"] == pytest.approx({"tangential": 0.1, "sagittal": 0}, abs=1e-10)
    assert complete["distance_mm"] == pytest.approx(0.1)
    run_path = root + f"/studies/runs/{complete['study_record']['id']}"
    frozen = client.get(run_path).json()
    assert frozen["project_evidence"]["reference"]["plane_uid"] == plane["uid"]
    assert frozen["connections"]["observed"]["run_id"] == capture["study_record"]["id"]
    assert frozen["connections"]["registration"]["run_id"] == registration["study_record"]["id"]

    source["launch_ray"]["s_angle_mrad"] = None
    links["reference"]["revision_id"] = save(first)
    partial = run("compare_transverse_centers", {}, links)
    assert partial["status"] == "partial" and partial["distance_mm"] is None
    assert partial["reference_mm"]["sagittal"] is None and partial["axis_notes"]["sagittal"]
    assert partial["residual_mm"] == pytest.approx({"tangential": 0.1})
    assert client.get(run_path).json() == frozen


@pytest.mark.parametrize(
    ("obs_change", "ref_change", "expected_status", "expected_quality", "reason"),
    [
        ({"window_coverage": "unverified"}, {}, "completed", "conditional", "像素窗"),
        ({"wavelength_nm": None}, {}, "completed", "conditional", "波长"),
        ({"quality": "limited"}, {}, "completed", "limited", "质量"),
        ({"quality": "unassessed"}, {}, "completed", "conditional", "质量"),
        ({}, {"launch_basis": "ideal_on_axis"}, "completed", "conditional", "理想轴"),
        ({"window_coverage": "truncated"}, {}, "needs_input", "unavailable", "截断"),
        ({}, {"wavelength_nm": 515}, "needs_input", "unavailable", "波长"),
        ({}, {"launch_basis": "unrecorded"}, "needs_input", "unavailable", "坐标依据"),
    ],
)
def test_window_coverage_and_model_limits_preserve_measured_point(
    obs_change, ref_change, expected_status, expected_quality, reason
):
    inputs = CenterComparison.model_validate(
        {
            "observed": {
                "x_mm": 0.2,
                "y_mm": -0.3,
                "quality": "usable",
                "window_coverage": "contains_beam",
                "basis": "物方标定读数",
                "wavelength_nm": 532,
                **obs_change,
            },
            "reference": {
                "t_mm": 0.2,
                "s_mm": -0.3,
                "plane_uid": "A",
                "beam_uid": "sample",
                "launch_basis": "specified",
                "wavelength_nm": 532,
                **ref_change,
            },
            "registration": {
                "offset_x_mm": 0,
                "offset_y_mm": 0,
                "rotation_deg": 0,
                "flip_y": False,
                "basis": "共用标记",
            },
        }
    )
    result = compare_centers(inputs)
    assert result["status"] == expected_status and result["comparison_quality"] == expected_quality
    assert any(reason in item for item in result["warnings"])
    assert result["observed_mm"] == pytest.approx({"tangential": 0.2, "sagittal": -0.3})
    if expected_status == "needs_input":
        assert result["residual_mm"] == {} and result["distance_mm"] is None
        assert set(result["axis_notes"]) == {"tangential", "sagittal"}
    else:
        assert result["distance_mm"] == 0


def test_archived_station_center_uses_only_frozen_axis_evidence():
    project = {
        "system": {"mode": "single_pass"},
        "observation_planes": [{"uid": "p", "name": "平面"}],
        "incident_paths": [{"uid": "b", "name": "支路", "launch_ray": {"t_offset_mm": 0.1}}],
    }
    beam = {
        "uid": "b",
        "registration_available": True,
        "index": None,
        "launch_basis": "specified",
        "field_axes": {"tangential": {"center_mm": 0.1}, "sagittal": {"center_mm": 0.3}},
        "width_basis": {"sagittal": "clipped"},
    }
    section = {"uid": "p", "beams": [beam], "z_mm": 25}

    def published():
        (row,) = station_centers(project, {"sections": {"planes": [section]}})
        return row[1]

    state = published()
    assert state["t_mm"] == 0.1 and state["s_mm"] is None and state["wavelength_nm"] is None
    assert "孔径" in state["notes"]["sagittal"]
    del beam["registration_available"]
    assert published()["t_mm"] is None
    beam["registration_available"] = True
    del beam["launch_basis"]
    state = published()
    assert state["t_mm"] is None and state["launch_basis"] == "unrecorded"


@pytest.mark.parametrize(
    ("checks", "new_positions", "reason"), [(0, 0, "没有预留"), (1, 0, "原位复测"), (2, None, "是否独立")]
)
def test_fit_and_repeat_measurements_keep_alignment_accuracy_conditional(checks, new_positions, reason):
    result = compare_centers(
        CenterComparison.model_validate(
            {
                "observed": {
                    "x_mm": 1,
                    "y_mm": 2,
                    "quality": "usable",
                    "window_coverage": "contains_beam",
                    "wavelength_nm": 532,
                    "basis": "贯通物方坐标",
                },
                "reference": {
                    "plane_uid": "site",
                    "beam_uid": "probe",
                    "t_mm": 1,
                    "s_mm": 2,
                    "launch_basis": "specified",
                    "wavelength_nm": 532,
                },
                "registration": {
                    "offset_x_mm": 0,
                    "offset_y_mm": 0,
                    "rotation_deg": 0,
                    "flip_y": False,
                    "basis": "重复读数",
                    "fit_evidence": {
                        "fit_count": 3,
                        "fit_rms_mm": 0,
                        "fit_max_mm": 0,
                        "check_count": checks,
                        "check_new_location_count": new_positions,
                    },
                },
            }
        )
    )
    assert result["status"] == "completed" and result["comparison_quality"] == "conditional"
    assert result["distance_mm"] == 0 and any(reason in note for note in result["warnings"])

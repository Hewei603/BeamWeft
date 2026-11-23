"""Optical models must meet a physical detector in its own coordinate and width convention."""

import asyncio
import hashlib
import math
from copy import deepcopy

import pytest

from app.models import AgentTask, Attachment, ExperimentCase, GeneratedContent
from app.workbench.beam_comparisons import CompareBeam, ReadingQuery, compare_reading
from app.workbench.projects import SaveProject, read_project, save_project
from app.workbench.study_inputs import WorkingDraft, capture_snapshot, basis
from app.workbench.tools import execute


def bench():
    return {
        "title": "移动透镜",
        "system": {
            "mode": "single_pass",
            "wavelength_nm": 808,
            "input_radius_mm": 0.45,
            "elements": [
                {"uid": "in", "name": "输入空气", "kind": "space", "length_mm": 30},
                {"uid": "lens", "name": "透镜", "kind": "lens", "focal_mm": 100},
                {"uid": "out", "name": "输出段", "kind": "space", "length_mm": 120},
            ],
        },
        "observation_planes": [{"uid": "camera", "name": "相机", "target_uid": "out", "depth_mm": 80}],
    }


def setup(client, project=None, beam_uid="primary", definition="fwhm_diameter", **changes):
    case = client.post("/api/cases", json={"title": "光斑定位"}).json()["id"]
    url = f"/api/cases/{case}/optical-project"
    saved = client.post(url, json={"project": project or bench(), "change_note": "登记时的模型"}).json()
    file = client.post(
        f"/api/cases/{case}/attachments", files={"file": ("camera.csv", b"T_um,S_um\n125,129\n", "text/csv")}
    ).json()
    note = {
        "revision_id": saved["revision_id"],
        "attachment_id": file["id"],
        "beam_uid": beam_uid,
        "plane_uid": "camera",
        "definition": definition,
        "unit": "um",
        "tangential": 125,
        "sagittal": 129,
        "axes_aligned": True,
        "wavelength_nm": 808,
        "locator": "camera.csv 第2行",
        "conditions": "台架 A",
        **changes,
    }
    result = client.post(url + "/beam-readings", json=note)
    assert result.status_code == 200, result.text
    return case, url, saved, result.json()


def test_detector_placement_changes_which_models_can_explain_a_reading(client, db):
    case, url, saved, reading = setup(client, tangential=147.5, sagittal=None)
    changed = deepcopy(saved["project"])
    changed["system"]["elements"][1]["focal_mm"] = 115
    trial = capture_snapshot(db, case, WorkingDraft(project=changed, source_revision_id=saved["revision_id"],
                                                    base_revision_id=saved["revision_id"]))
    db.commit()
    api = f"{url}/{trial['revision_id']}/beam-comparisons"
    body = {"reading_id": reading["id"], "position_reference":"fixed_z", "registration_note":"mount",
            "width_bounds":{"tangential":5,"source":"camera"}}
    nominal = client.post(api, json=body).json()
    assert nominal["measurement_intervals"]["tangential"]["target_inside"] is False
    bounded = {**body, "position_bound":{"plus_minus_mm":5, "source":"stage calibration"}}
    both = client.post(api, json=bounded).json()
    band = both["measurement_intervals"]["tangential"]
    assert band == {"lower":142.5, "upper":152.5, "reference_compatible":True, "target_compatible":True}
    assert nominal["id"] != both["id"]
    moved = client.post(api, json={**bounded,"data_role":"different_configuration"}).json()
    assert "target_compatible" not in moved["measurement_intervals"]["tangential"]
    boundary = client.post(api, json={**bounded,"position_mm":31}).json()
    assert boundary["status"] == "partial"
    assert "target_compatible" not in boundary["measurement_intervals"]["tangential"]
    assert client.post(api, json={**bounded,"position_bound":{"plus_minus_mm":-2,"source":"mount"}}).status_code==422


@pytest.mark.parametrize("t,s,joint", [(142, 103, False), (142, 142.12, True)])
def test_two_axes_have_to_share_one_camera_position(client, t, s, joint):
    _, url, saved, reading = setup(client, tangential=t, sagittal=s)
    api = f"{url}/{saved['revision_id']}/beam-comparisons"
    request = {"reading_id": reading["id"], "position_reference": "fixed_z", "registration_note": "one camera",
               "position_bound": {"plus_minus_mm":5,"source":"stage"}, "width_bounds": {
                   "tangential":.1, "sagittal":.1, "source":"width calibration"}}
    response = client.post(api, json=request)
    assert response.status_code == 200, response.text
    data = response.json()
    assert all(b["target_compatible"] for b in data["measurement_intervals"].values())
    sample = data["target_prediction"]
    assert bool(sample["common_offsets_mm"]) is joint
    if joint:
        lo, hi = sample["common_offsets_mm"][0]
        for delta in [lo, (lo+hi)/2, hi]:
            after = 80 + delta
            actual = 1000*math.sqrt(2*math.log(2))*.45*math.hypot(1-after/100,
                         (30*(1-after/100)+after)*.000808/(math.pi*.45**2))
            assert abs(actual-t) <= .10000001 and abs(actual-s) <= .10000001


def test_reading_above_waist_can_match_two_separate_stations(client):
    _, url, saved, reading = setup(client, tangential=72, sagittal=None)
    api = f"{url}/{saved['revision_id']}/beam-comparisons"
    response = client.post(api, json={"reading_id":reading["id"],"position_reference":"fixed_z","position_mm":130,
                           "registration_note":"mount log","position_bound":{"plus_minus_mm":8,"source":"travel"},
                           "width_bounds":{"tangential":.1,"source":"camera"}})
    offsets = response.json()["target_prediction"]["widths"]["tangential"]["compatible_offsets_mm"]
    assert len(offsets) == 2 and offsets[0][1] < offsets[1][0]
    for pair in offsets:
        for offset in pair:
            length = 100 + offset
            width = 1000*math.sqrt(2*math.log(2))*.45*math.hypot(1-length/100,
                       (30*(1-length/100)+length)*.000808/(math.pi*.45**2))
            assert abs(width-72) == pytest.approx(.1, abs=1e-8)


def test_cross_revision_predicts_native_fwhm_at_stationary_detector_and_moving_mount(client, db):
    case, url, first, reading = setup(client)
    raw = deepcopy(first["project"])
    raw["system"]["elements"][0]["length_mm"] = 50
    raw["observation_planes"] = []  # A candidate can be tested without a corresponding UI marker.
    candidate = capture_snapshot(
        db,
        case,
        WorkingDraft(project=raw, source_revision_id=first["revision_id"], base_revision_id=first["revision_id"]),
    )
    db.commit()
    current = candidate["revision_id"]
    api = f"{url}/{current}/beam-comparisons"
    body = {
        "reading_id": reading["id"],
        "position_reference": "fixed_z",
        "registration_note": "同台架，入口共同零点；T/S 工况相同。",
    }

    def expected(d, z):
        after = z - d
        a = 1 - after / 100
        b = d * a + after
        return 1000 * math.sqrt(2 * math.log(2)) * 0.45 * math.hypot(a, b * 0.000808 / (math.pi * 0.45**2))

    assert not reading["radii_mm"]
    assert reading["native_widths"]["tangential"]["predicted"] == pytest.approx(expected(30, 110))
    r = client.post(api, json=body)
    assert r.status_code == 200, r.text
    fixed = r.json()
    assert fixed["target_prediction"]["model_kind"] == "draft"
    assert fixed["target_prediction"]["depth_mm"] == 60
    assert fixed["reference_prediction"]["widths"]["sagittal"]["predicted"] == pytest.approx(expected(30, 110))
    assert fixed["target_prediction"]["widths"]["tangential"]["residual"] == pytest.approx(125 - expected(50, 110))
    assert client.post(api, json=body).json()["id"] == fixed["id"]
    moved = client.post(
        api, json={**body, "position_reference": "element_depth", "data_role": "different_configuration"}
    ).json()
    assert moved["target_prediction"]["z_mm"] == 130
    assert moved["target_prediction"]["widths"]["tangential"]["predicted"] == pytest.approx(expected(50, 130))
    assert "residual" not in moved["target_prediction"]["widths"]["tangential"]
    known = client.post(api, json={**body, "position_mm": 100, "data_role": "model_input"}).json()
    assert known["target_prediction"]["widths"]["tangential"]["residual"] == pytest.approx(125 - expected(50, 100))
    assert known["reference_prediction"]["z_mm"] == known["target_prediction"]["z_mm"] == 100
    assert read_project(db, case)["revision_id"] == first["revision_id"]
    bounded = client.post(api, json={**body, "width_bounds": {"tangential": 5, "source": "合成仪器校准记录"}}).json()
    assert bounded["measurement_intervals"]["tangential"] == {
        "lower": 120,
        "upper": 130,
        "reference_inside": True,
        "target_inside": False,
    }
    alternate = client.post(
        api,
        json={
            **body,
            "data_role": "different_configuration",
            "width_bounds": {"sagittal": 200, "source": "另一工况算例"},
        },
    ).json()
    assert "target_inside" not in alternate["measurement_intervals"]["sagittal"]
    assert not alternate["measurement_intervals"]["sagittal"]["lower"]
    assert len(client.get(f"{url}/{current}/beam-comparisons").json()["items"]) == 5
    assert len(client.get(f"{url}/beam-observations").json()["readings"]) == 1
    # Older observations did not store native-width predictions.
    row = db.get(GeneratedContent, reading["id"])
    row.content = {k: v for k, v in row.content.items() if k != "native_widths"}
    db.commit()
    task = AgentTask(mode="workbench", case_id=case, status="running", goal="比较草稿")
    db.add(task)
    db.commit()
    index = asyncio.run(
        execute(
            "read_beam_readings",
            ReadingQuery(all_revisions=True, revision_id=current),
            db=db,
            case=db.get(ExperimentCase, case),
            task=task,
            provider=None,
            session_id=None,
            attachment_ids=[],
            working=basis(candidate),
        )
    )
    assert index["scope"] == "all_case_revisions" and index["readings"][0]["id"] == reading["id"]
    replay = asyncio.run(
        execute(
            "compare_beam_reading",
            CompareBeam(**{**body, "registration_note": "旧观测与同一坐标"}),
            db=db,
            case=db.get(ExperimentCase, case),
            task=task,
            provider=None,
            session_id=None,
            attachment_ids=[],
            working=basis(candidate),
        )
    )
    assert replay["target_prediction"]["widths"] == fixed["target_prediction"]["widths"]
    assert replay["source"]["sha256"] == hashlib.sha256(b"T_um,S_um\n125,129\n").hexdigest()


def test_second_moment_branch_cannot_supply_a_gaussian_fwhm_or_independent_source_test(client, db):
    cavity = bench()
    cavity["system"].update(
        mode="standing_wave",
        wavelength_nm=1064,
        elements=[
            {"uid": "m1", "name": "M1", "kind": "mirror"},
            {"uid": "out", "name": "入射段", "kind": "space", "length_mm": 120},
            {"uid": "m2", "name": "M2", "kind": "mirror", "radius_mm": 220},
        ],
    )
    cavity["incident_paths"] = [
        {
            "uid": "pump",
            "name": "泵浦",
            "target_uid": "out",
            "target_n": 1,
            "wavelength_nm": 808,
            "tangential": {"radius_mm": 0.18, "m2": 1, "wavefront": "waist"},
            "sagittal": {"radius_mm": 0.2, "m2": 1.6, "wavefront": "waist", "second_moment_radius": True},
        }
    ]
    case, url, model, reading = setup(client, cavity, beam_uid="pump")

    def compare(**args):
        return compare_reading(
            db,
            case,
            CompareBeam(
                reading_id=reading["id"], position_reference="fixed_z", registration_note="台架、坐标和工况一致", **args
            ),
            model["revision_id"],
        )

    residuals = compare()
    assert residuals["status"] == "partial"
    t = 0.18 * math.hypot(1, 80 * 0.000808 / (math.pi * 0.18**2))
    assert residuals["target_prediction"]["widths"]["tangential"]["predicted"] == pytest.approx(
        1000 * math.sqrt(2 * math.log(2)) * t
    )
    assert "residual" not in residuals["target_prediction"]["widths"]["sagittal"]
    assert "二阶矩" in residuals["target_prediction"]["axis_notes"]["sagittal"]
    at_source = compare(position_mm=0)
    assert at_source["status"] == "needs_input"
    assert "光源输入" in at_source["target_prediction"]["axis_notes"]["tangential"]


@pytest.mark.parametrize(
    "change,expected",
    [
        ("wavelength", "波长不同"),
        ("outside", "超出"),
        ("boundary", "边界"),
        ("removed", "已移除"),
        ("mode", "类型不同"),
    ],
)
def test_mismatch_is_preserved_with_reason_and_no_spurious_residual(client, db, change, expected):
    case, url, first, reading = setup(client)
    raw = deepcopy(first["project"])
    args = {"reading_id": reading["id"], "registration_note": "共同坐标", "position_reference": "fixed_z"}
    if change == "wavelength":
        raw["system"]["wavelength_nm"] = 532
    if change == "outside":
        args["position_mm"] = 2000
    if change == "boundary":
        args["position_mm"] = 30
        raw["system"]["elements"][2]["uid"] = "new-output"
    if change == "removed":
        args["position_reference"] = "element_depth"
        raw["system"]["elements"][2]["uid"] = "another"
    if change == "mode":
        raw["system"] = {
            "mode": "standing_wave",
            "wavelength_nm": 808,
            "elements": [
                {"name": "M1", "kind": "mirror"},
                {"uid": "out", "name": "air", "kind": "space", "length_mm": 120},
                {"name": "M2", "kind": "mirror", "radius_mm": 220},
            ],
        }
    revision = save_project(
        db, case, SaveProject(project=raw, base_revision_id=first["revision_id"], change_note="候选")
    )
    response = client.post(f"{url}/{revision['revision_id']}/beam-comparisons", json=args)
    assert response.status_code == 200, response.text
    candidate = response.json()["target_prediction"]
    assert candidate["status"] == "needs_input" and expected in candidate["message"]
    assert not any("residual" in v for v in candidate["widths"].values())


def test_cross_case_and_changed_original_cannot_be_used_for_comparison(client, db):
    case, base, saved, reading = setup(client)
    other, other_url, version, _ = setup(client)
    body = {"reading_id": reading["id"], "position_reference": "fixed_z", "registration_note": "共同坐标"}
    assert client.post(f"{other_url}/{version['revision_id']}/beam-comparisons", json=body).status_code == 422
    assert client.post(f"{base}/{version['revision_id']}/beam-comparisons", json=body).status_code == 422
    attachment = db.get(Attachment, reading["attachment_id"])
    attachment.content_hash = "0" * 64
    db.commit()
    blocked = client.post(f"{base}/{saved['revision_id']}/beam-comparisons", json=body)
    assert blocked.status_code == 422 and "校验" in blocked.text


def test_source_input_primary_is_not_presented_as_a_measured_prediction(client):
    proj = bench()
    proj["observation_planes"][0].update(target_uid="in", depth_mm=0)
    _, _, _, reading = setup(client, proj, definition="radius_1e2")
    assert not reading["measured_minus_model_mm"]
    assert reading["radii_mm"]["tangential"] == 0.125
    assert "光源输入" in reading["axis_notes"]["tangential"]


def test_zero_distance_magnification_is_resolved_separately_for_each_axis(client):
    proj = bench()
    proj["system"]["elements"].insert(
        0,
        {
            "uid": "relay",
            "name": "等效传递",
            "kind": "matrix",
            "matrix_t": [2, 0, 0, 0.5],
            "matrix_s": [1, 0, -0.01, 1],
        },
    )
    proj["observation_planes"][0].update(target_uid="in", depth_mm=0)
    _, _, _, reading = setup(client, proj, definition="diameter_1e2")
    assert reading["native_widths"]["tangential"]["predicted"] == pytest.approx(1800)
    assert "residual" not in reading["native_widths"]["sagittal"]

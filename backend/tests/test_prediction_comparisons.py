"""Prospective comparisons may use archived model inputs but never invent camera data."""

from copy import deepcopy
import math

import pytest

from app.workbench.study_inputs import WorkingDraft, capture_snapshot
from app.models import GeneratedContent


def project(focal=100):
    return {
        "title": "参考光路",
        "system": {
            "mode": "single_pass",
            "wavelength_nm": 808,
            "input_radius_mm": 0.45,
            "elements": [
                {"uid": "in", "name": "输入空气", "kind": "space", "length_mm": 30},
                {"uid": "lens", "name": "透镜", "kind": "lens", "focal_mm": focal},
                {"uid": "out", "name": "输出空气", "kind": "space", "length_mm": 120},
            ],
        },
    }


def models(client, db, first, other):
    case = client.post("/api/cases", json={"title": "候选测点"}).json()["id"]
    url = f"/api/cases/{case}/optical-project"
    response = client.post(url, json={"project": first, "change_note": "原工程"})
    assert response.status_code == 200, response.text
    rid = response.json()["revision_id"]
    trial = capture_snapshot(db, case, WorkingDraft(project=other, source_revision_id=rid, base_revision_id=rid))
    db.commit()
    return url, rid, trial["revision_id"]


def expected(f, z, w=0.45, wavelength_mm=0.000808, m2=1):
    after = max(0.0, z - 30)
    a = 1 - after / f
    b = min(z, 30) * a + after
    return w * math.hypot(a, b * wavelength_mm * m2 / (math.pi * w * w))


def request(rid, **kwargs):
    return {
        "reference_revision_id": rid,
        "axis": "tangential",
        "definition": "fwhm_diameter",
        "unit": "um",
        "registration_note": "同一台架零点和仪器轴；所列位置有取样口。",
        "positions_mm": [15, 80, 110, 140],
        **kwargs,
    }


def test_prospective_fwhm_without_a_reading_preserves_versions_and_reports_bounds(client, db):
    url, first, trial = models(client, db, project(), project(115))
    endpoint = f"{url}/{trial}/prediction-comparisons"
    body = request(first, positions_mm=[15, 80, 110, 140, 250], absolute_width_bound=5, bound_source="相机标定限值")
    response = client.post(endpoint, json=body)
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["status"] == "partial" and record["models"][1]["kind"] == "draft"
    measured_factor = math.sqrt(2 * math.log(2)) * 1000
    for row in record["points"][:-1]:
        z = row["position_mm"]
        a, b = [expected(f, z) * measured_factor for f in (100, 115)]
        assert row["reference"]["width"] == pytest.approx(a)
        assert row["target"]["width"] == pytest.approx(b)
        assert row["target"]["reading_interval"] == pytest.approx([max(0, b - 5), b + 5])
        assert row["disjoint"] == (abs(a - b) > 10)
    assert "issue" in record["points"][-1] and "difference" not in record["points"][-1]
    assert record["largest_at_mm"] == [max(record["points"][:-1], key=lambda p: p["difference"])["position_mm"]]
    assert client.post(endpoint, json=body).json()["id"] == record["id"]
    assert client.get(endpoint).json()["items"][0] == record
    assert client.get(url).json()["current"]["revision_id"] == first
    assert client.get(f"{url}/{first}/beam-readings").json() == []
    options = client.get(url + "/versions").json()["items"]
    assert [(o["id"], o["kind"]) for o in options] == [(trial, "optical_snapshot"), (first, "optical_project")]
    assert client.get(url + "/versions", params={"before_id": trial}).json()["items"] == options[1:]
    assert client.get(f"{url}/{first}/prediction-comparisons").json()["items"] == [record]
    # Intervals just touching do not discriminate. Unknown bounds still allow a plain numerical comparison.
    row = record["points"][1]
    touching = client.post(
        endpoint, json={**body, "positions_mm": [80], "absolute_width_bound": row["difference"] / 2}
    ).json()
    assert not touching["points"][0]["disjoint"]
    equal = client.post(endpoint, json=request(first, positions_mm=[10, 15])).json()
    assert equal["largest_at_mm"] == [] and equal["largest_difference"] == 0
    assert "disjoint" not in equal["points"][0]


def test_unknown_downstream_medium_keeps_local_predictions_and_usable_reference(client, db):
    candidate = project()
    candidate["system"]["elements"][-1]["n"] = None
    url, first, trial = models(client, db, project(), candidate)
    body = request(first, positions_mm=[15,80], definition="radius_1e2", unit="mm",
                   absolute_width_bound=.001, bound_source="相机读数")
    response = client.post(f"{url}/{trial}/prediction-comparisons", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "partial"
    upstream, unknown = result["points"]
    assert upstream["difference"] == pytest.approx(0)
    assert upstream["target"]["width"] == pytest.approx(expected(100,15))
    assert unknown["reference"]["width"] == pytest.approx(expected(100,80))
    assert unknown["reference"]["reading_interval"] == pytest.approx([expected(100,80)-.001,expected(100,80)+.001])
    assert "clearance" not in unknown and "reading_interval" not in unknown["target"]
    assert "输出空气" in unknown["target"]["issue"] and "折射率" in unknown["target"]["issue"]
    assert "width" not in unknown["target"] and "difference" not in unknown
    assert result["largest_at_mm"] == []


def test_unclosed_resonator_never_uses_a_single_pass_prefix(client, db):
    base = project()
    base["system"].update(mode="standing_wave",
        elements=[{"kind":"mirror","uid":"m1","name":"平镜"},base["system"]["elements"][0],
                  {"kind":"mirror","uid":"m2","name":"曲镜","radius_mm":120}])
    draft = deepcopy(base)
    draft["system"]["elements"].pop()
    url, first, trial = models(client,db,base,draft)
    result = client.post(f"{url}/{trial}/prediction-comparisons", json=request(first,positions_mm=[15])).json()
    assert result["status"] == "needs_input"
    row = result["points"][0]
    assert row["reference"]["width"]>0 and "width" not in row["target"] and "difference" not in row


def test_private_train_uses_its_own_known_prefix_and_wavelength(client, db):
    first = project()
    first["incident_paths"] = [{"uid":"probe","name":"独立探针","target_uid":"in","wavelength_nm":635,
        "tangential":{"radius_mm":.3,"m2":1,"wavefront":"waist"},"elements":deepcopy(first["system"]["elements"])}]
    changed = deepcopy(first)
    changed["system"]["elements"][-1]["n"] = None
    changed["incident_paths"][0]["elements"][-1]["n"] = None
    url, base, trial = models(client,db,first,changed)
    body = request(base, beam_uid="probe",region="incident_train",positions_mm=[-135,-70],definition="radius_1e2",unit="mm")
    result = client.post(f"{url}/{trial}/prediction-comparisons",json=body).json()
    upstream, blocked = result["points"]
    assert upstream["target"]["width"] == pytest.approx(expected(100,15,.3,.000635))
    assert upstream["difference"] == 0 and "difference" not in blocked
    assert "折射率" in blocked["target"]["issue"] and blocked["reference"]["width"]>0


def test_no_pseudo_results_at_interface_or_incompatible_wavelength(client, db):
    initial = project()
    url, first, trial = models(client, db, initial, project(115))
    endpoint = f"{url}/{trial}/prediction-comparisons"
    raw = request(first, positions_mm=[0, 30, 60])
    result = client.post(endpoint, json=raw).json()
    assert "光源输入" in result["points"][0]["issue"]
    assert "边界" in result["points"][1]["issue"]
    side = client.post(endpoint, json={**raw, "boundary_side": "downstream"}).json()
    assert side["points"][1]["difference"] == pytest.approx(0)
    for updates in [
        {"reference_revision_id": trial},
        {"positions_mm": [5, 5]},
        {"absolute_width_bound": 0},
        {"position_reference": "element_depth"},
        {"region": "incident_train"},
    ]:
        assert client.post(endpoint, json={**raw, **updates}).status_code == 422
    other = project(115)
    other["system"]["wavelength_nm"] = 1064
    url2, rid2, snapshot2 = models(client, db, project(), other)
    route2 = f"{url2}/{snapshot2}/prediction-comparisons"
    assert client.post(route2, json=raw).status_code == 422
    different = client.post(route2, json=request(rid2)).json()
    assert different["status"] == "needs_input"
    assert all("波长不同" in row["issue"] and "difference" not in row for row in different["points"])


def test_canvas_trial_reuses_frozen_inputs_without_replacing_official_model(client, db):
    url, first, other = models(client, db, project(), project(115))
    endpoint = url + "/prediction-comparisons"
    draft = project(106)
    for element in draft["system"]["elements"]:
        del element["uid"]  # Legacy files have no stable element IDs until capture.
    body = {
        "working_draft": {"project": draft, "base_revision_id": first, "source_revision_id": first},
        "comparison": request(other, positions_mm=[110]),
    }
    previous = client.get(url + "/versions").json()["items"]
    invalid = {**body, "comparison": request(other, revision_id=first)}
    assert client.post(endpoint, json=invalid).status_code == 422
    outsider, foreign, _ = models(client, db, project(), project(85))
    assert client.post(endpoint, json={**body, "comparison": request(foreign)}).status_code == 422
    assert client.get(url + "/versions").json()["items"] == previous
    result = client.post(endpoint, json=body)
    assert result.status_code == 200, result.text
    record = result.json()
    snapshot = record["revision_id"]
    assert record["models"][1]["kind"] == "draft"
    width = math.sqrt(2 * math.log(2)) * 1000 * expected(106, 110)
    assert record["points"][0]["target"]["width"] == pytest.approx(width)
    assert client.post(endpoint, json=body).json()["id"] == record["id"]
    alternate = client.post(endpoint, json={**body, "comparison": request(first, positions_mm=[75])}).json()
    assert alternate["revision_id"] == snapshot and alternate["id"] != record["id"]
    assert client.get(url + "/versions").json()["items"][1:] == previous
    assert client.get(url).json()["current"]["revision_id"] == first
    assert client.get(endpoint).json()["items"] == [alternate, record]
    assert client.get(endpoint, params={"before_id": alternate["id"]}).json()["items"] == [record]
    assert client.get(outsider + "/prediction-comparisons").json()["items"] == []
    for row_id in (snapshot, record["id"], alternate["id"]):
        assert len(db.get(GeneratedContent, row_id).operation_key) <= 80


def test_pump_train_coordinates_and_second_moment_are_axis_specific(client, db):
    host = project()
    host["system"] = {
        "mode": "standing_wave",
        "wavelength_nm": 1064,
        "elements": [
            {"uid": "m1", "name": "M1", "kind": "mirror"},
            {"uid": "entry", "name": "入射段", "kind": "space", "length_mm": 120},
            {"uid": "m2", "name": "M2", "kind": "mirror", "radius_mm": 220},
        ],
    }
    path = {
        "uid": "pump",
        "name": "入射支路",
        "target_uid": "entry",
        "target_n": 1,
        "wavelength_nm": 808,
        "tangential": {"wavefront": "unknown"},
        "sagittal": {"radius_mm": 0.45, "m2": 2, "wavefront": "waist", "second_moment_radius": True},
        "elements": project()["system"]["elements"],
    }
    host["incident_paths"] = [path]
    modified = deepcopy(host)
    modified["incident_paths"][0]["elements"][1]["focal_mm"] = 115
    url, first, second = models(client, db, host, modified)
    api = f"{url}/{second}/prediction-comparisons"
    body = request(
        first,
        region="incident_train",
        beam_uid="pump",
        axis="sagittal",
        definition="d4sigma_diameter",
        unit="mm",
        positions_mm=[-70, -40],
    )
    for row in client.post(api, json=body).json()["points"]:
        for name, f in (("reference", 100), ("target", 115)):
            sample = row[name]
            assert sample["z_mm"] == row["position_mm"]
            assert sample["width"] == pytest.approx(2 * expected(f, row["position_mm"] + 150, m2=2))
    inner = client.post(
        api, json={**body, "position_reference": "element_depth", "element_uid": "out", "positions_mm": [50]}
    ).json()
    assert inner["points"][0]["target"]["z_mm"] == -70
    for options, match in (({"axis": "tangential"}, "入射光源"), ({"definition": "fwhm_diameter"}, "D4σ")):
        result = client.post(api, json={**body, **options}).json()
        assert result["status"] == "needs_input" and match in result["points"][0]["issue"]
    propagated = client.post(api, json={**body, "region": "host", "positions_mm": [10]}).json()
    assert propagated["status"] == "completed"
    assert propagated["points"][0]["target"]["width"] == pytest.approx(2 * expected(115, 160, m2=2))

    # Bounds before entry use the absolute host datum; after entry the S axis
    # uses the pump wavelength and measured second-moment propagation parameter.
    for region, station, centre in (("incident_train", -40, 110), ("host", 10, 160)):
        bounded = client.post(api, json={**body, "region": region, "positions_mm": [station],
                              "position_bound": {"plus_minus_mm": 2, "source": "mount"}}).json()
        limits = sorted(2 * expected(115, z, m2=2) for z in (centre-2, centre+2))
        assert bounded["points"][0]["target"]["position_width_range"] == pytest.approx(limits)
        assert "disjoint" not in bounded["points"][0]


def test_axial_bounds_change_the_choice_and_include_interior_waist(client, db):
    url, first, second = models(client, db, project(), project(115))
    api = f"{url}/{second}/prediction-comparisons"
    body = request(first, positions_mm=[110, 115, 125, 147], absolute_width_bound=5, bound_source="camera",
                   position_bound={"plus_minus_mm": 5, "source": "stage datum"})
    result = client.post(api, json=body).json()
    assert result["status"] == "partial"
    assert result["largest_at_mm"] == [115] and result["separated_at_mm"] == [125]
    factor = 1000 * math.sqrt(2 * math.log(2))
    assert result["maximum_clearance"] == pytest.approx(factor * (expected(115, 130) - expected(100, 120)) - 10)
    assert result["points"][0]["disjoint"] is False
    assert "disjoint" not in result["points"][-1]
    assert "position_issue" in result["points"][-1]["target"]
    zray = math.pi * .45**2 / .000808
    f = 100
    waist = 30 + (1/f - 30*(1-30/f)/zray**2)/(1/f**2 + ((1-30/f)/zray)**2)
    assert 120 < waist < 130
    assert result["points"][2]["reference"]["position_width_range"] == pytest.approx(
        [factor*expected(f, waist), factor*max(expected(f, 120), expected(f, 130))])
    assert client.post(api, json=body).json()["id"] == result["id"]


def test_position_range_can_cross_a_model_partition_but_respects_registered_element_and_medium(client, db):
    a = project()
    a["system"]["elements"][-1]["length_mm"] = 70
    a["system"]["elements"].append({"uid": "tail", "name": "rear", "kind": "space", "length_mm": 50})
    b = deepcopy(a)
    b["system"]["elements"][-1]["n"] = 1.5
    url, first, second = models(client, db, a, b)
    endpoint = f"{url}/{second}/prediction-comparisons"
    body = request(first, positions_mm=[99], position_bound={"plus_minus_mm": 3, "source": "stage"})
    row = client.post(endpoint, json=body).json()["points"][0]
    assert "position_width_range" in row["reference"] and "position_issue" in row["target"]
    anchored = client.post(endpoint, json={**body, "position_reference": "element_depth",
                           "element_uid": "out", "positions_mm": [69]}).json()["points"][0]
    assert "position_issue" in anchored["reference"] and "position_issue" in anchored["target"]


def test_dielectric_axial_coordinate_and_cavity_mode(client, db):
    a = project()
    a["system"]["elements"] = [{"uid":"slab","kind":"space","name":"glass","length_mm":110,"n":1.5}]
    other = deepcopy(a)
    other["system"]["input_radius_mm"] = .5
    url, ref, dst = models(client, db, a, other)
    req = request(ref, positions_mm=[80], definition="radius_1e2", unit="mm",
                  position_bound={"plus_minus_mm":6,"source":"imaging calibration"})
    row = client.post(f"{url}/{dst}/prediction-comparisons",json=req).json()["points"][0]
    for w, key in ((.45, "reference"),(.5,"target")):
        limits = [w*math.hypot(1,z*.000808/(1.5*math.pi*w*w)) for z in (74,86)]
        assert row[key]["position_width_range"] == pytest.approx(limits)
    a["system"]={"mode":"standing_wave","wavelength_nm":1064,"elements":[
        {"uid":"m1","name":"M1","kind":"mirror"},{"uid":"air","name":"arm","kind":"space","length_mm":100},
        {"uid":"m2","name":"M2","kind":"mirror","radius_mm":250}]}
    other=deepcopy(a)
    other["system"]["elements"][-1]["radius_mm"]=350
    url,ref,dst=models(client,db,a,other)
    row = client.post(f"{url}/{dst}/prediction-comparisons",json={**req,"reference_revision_id":ref}).json()["points"][0]
    for radius,key in ((250,"reference"),(350,"target")):
        zr=math.sqrt(100*(radius-100))
        limits=[math.sqrt(.001064*zr/math.pi)*math.hypot(1,z/zr) for z in (74,86)]
        assert row[key]["position_width_range"] == pytest.approx(limits)

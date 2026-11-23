from math import hypot, pi

import pytest

from app.models import ExperimentCase
from app.workbench.projects import SaveProject, read_project, save_project


def setup_bench(client, db, *, thickness=8, gap_index=1, gap_part=None):
    case = ExperimentCase(title="双基准测量", goal="", cavity_type="custom", parameters={"L_mm": 180})
    db.add(case)
    db.commit()
    elements = [{"uid": uid, "name": uid, "kind": "space", "n": index, "length_mm": distance}
                for uid, distance, index in [("left", 20, 1), ("plate", 2, 1.5), ("right", 25, gap_index)]]
    planes = [{"uid": uid, "name": uid, "target_uid": "right", "depth_mm": depth}
              for uid, depth in [("probe", 12), ("edge", 23)]]
    planes.append({"uid": "camera", "name": "camera", "position_reference": "fixed_z", "z_mm": 34})
    saved = save_project(db, case.id, SaveProject(project={"title": "同轴测量", "system": {
        "mode": "single_pass", "wavelength_nm": 633, "input_radius_mm": .08, "elements": elements},
        "observation_planes": planes, "parts": [gap_part] if gap_part else []}, change_note="登记坐标及实物"))
    item = client.post("/api/inventory/items", json={"name": "窗口", "category": "window",
                       "material": "BK7", "thickness_mm": thickness})
    assert item.status_code == 201, item.text
    url = f"/api/cases/{case.id}/optical-project/replace-component"
    args = {"base_revision_id": saved["revision_id"], "target_element_uid": "plate",
            "inventory_id": item.json()["id"]}
    return case.id, saved, url, args


@pytest.mark.parametrize(("policy", "left", "right", "edge_z"), [
    ("keep_gaps", 20, 25, 51), ("fixed_entrance", 20, 19, None), ("fixed_exit", 14, 25, 45)])
def test_mount_preserves_registered_observation_reference(client, db, policy, left, right, edge_z):
    case_id, saved, url, args = setup_bench(client, db)
    args["spacing_policy"] = policy
    preview = client.post(url + "/preview", json=args)
    assert preview.status_code == 200, preview.text
    proposal = preview.json()
    receipt = proposal["replacement"]
    assert receipt["spacing_policy"] == policy
    assert (receipt["old_entry_z_mm"], receipt["old_exit_z_mm"]) == (20, 22)
    assert (receipt["new_entry_z_mm"], receipt["new_exit_z_mm"]) == (left, left+8)
    assert receipt["new_path_length_mm"] == left + 8 + right
    if policy == "fixed_entrance":
        assert receipt["compensating_gap"]["new_length_mm"] == 19
        assert [(p["old_z_mm"], p["new_z_mm"]) for p in receipt["affected_planes"]] == [(34, 40), (45, None)]
    if policy == "keep_gaps":
        assert receipt["compensating_gap"] is None
        assert {p["uid"] for p in receipt["affected_planes"]} == {"probe", "edge"}
    # The same 34 mm bench station and the station registered 12 mm into the gap may diverge.
    rows = {row["uid"]: row for row in proposal["analysis"]["sections"]["planes"]}
    assert rows["probe"]["z_mm"] == left + 8 + 12
    assert rows["camera"]["z_mm"] == 34
    assert rows["edge"].get("z_mm") == edge_z
    if edge_z is None:
        assert rows["edge"]["beams"] == [] and rows["edge"]["message"]
    index = proposal["project"]["system"]["elements"][1]["n"]
    for row in (rows["probe"], rows["camera"]):
        reduced_distance = row["z_mm"] - 8 + 8/index
        radius = hypot(.08, reduced_distance*.000633 / (pi*.08))
        assert row["beams"][0]["radii_mm"]["tangential"] == pytest.approx(radius, rel=1e-10)
    assert proposal["project"]["observation_planes"] == saved["project"]["observation_planes"]
    other = "fixed_exit" if policy != "fixed_exit" else "keep_gaps"
    token = {"preview_token": proposal["preview_token"], "change_note": "锁定评估条件"}
    assert client.post(url, json={**args, **token, "spacing_policy": other}).status_code == 409
    assert read_project(db, case_id)["revision_id"] == saved["revision_id"]
    result = client.post(url, json={**args, **token})
    assert result.status_code == 200, result.text
    assert result.json()["replacement"] == receipt
    assert db.get(ExperimentCase, case_id).parameters["L_mm"] == 180
    assert read_project(db, case_id, saved["revision_id"])["project"] == saved["project"]


@pytest.mark.parametrize(("thickness", "index", "part", "error"), [
    (30, 1, None, "28 mm"), (8, 1.45, None, "可调空气间隔"),
    (8, 1, {"element_index": 2, "source": "固定尺寸", "locked_parameters": ["length_mm"]}, "尺寸受约束"),
])
def test_fixed_datum_requires_available_unlocked_travel(client, db, thickness, index, part, error):
    case_id, saved, url, args = setup_bench(client, db, thickness=thickness, gap_index=index, gap_part=part)
    for endpoint in (url + "/preview", url):
        response = client.post(endpoint, json={**args, "spacing_policy": "fixed_entrance", "change_note": "按端面定位"})
        assert response.status_code == 422 and error in response.text
    assert read_project(db, case_id)["revision_id"] == saved["revision_id"]


@pytest.mark.parametrize(("thickness", "index", "gap"), [(1, 1, 26), (27, 1, 0), (2, 1.5, None)])
def test_shorter_equal_and_zero_clearance(client, db, thickness, index, gap):
    _, _, url, args = setup_bench(client, db, thickness=thickness, gap_index=index)
    response = client.post(url + "/preview", json={**args, "spacing_policy": "fixed_entrance"})
    assert response.status_code == 200, response.text
    receipt = response.json()["replacement"]
    assert receipt["new_path_length_mm"] == 47
    assert (receipt["compensating_gap"] or {}).get("new_length_mm") == gap


def test_fixed_station_crosses_face_and_hits_interface(client, db):
    case_id, saved, url, args = setup_bench(client, db)
    project = saved["project"]
    project["observation_planes"] = [
        {"uid": uid, "name": uid, "position_reference": "fixed_z", "z_mm": z}
        for uid, z in [("sample", 25), ("surface", 28)]]
    revised = save_project(db, case_id, SaveProject(base_revision_id=saved["revision_id"], project=project,
                          change_note="两个固定坐标，未预设界面哪一侧"))
    response = client.post(url + "/preview", json={**args, "base_revision_id": revised["revision_id"],
                           "spacing_policy": "fixed_entrance"})
    assert response.status_code == 200, response.text
    data = response.json()
    changes = {p["uid"]: p for p in data["replacement"]["affected_planes"]}
    sample = changes["sample"]
    assert (sample["old_z_mm"], sample["new_z_mm"]) == (25, 25)
    assert sample["old_element_uid"] == "right"
    assert sample["new_element_uid"] == data["replacement"]["target_element_uid"]
    assert changes["surface"]["new_z_mm"] is None and "边界" in changes["surface"]["note"]
    assert data["analysis"]["sections"]["planes"][1]["beams"] == []

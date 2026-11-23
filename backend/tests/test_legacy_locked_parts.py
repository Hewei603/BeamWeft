from copy import deepcopy

import pytest

from app.models import ExperimentCase, GeneratedContent


def legacy_train(client, db, *, duplicates=False, arm=False):
    cid = client.post("/api/cases", json={"title": "旧工程的实物锁定"}).json()["id"]
    base = f"/api/cases/{cid}/optical-project"
    elements = [{"name": "空气", "kind": "space", "length_mm": 20, "n": 1},
                {"name": "透镜", "kind": "lens", "focal_mm": 60},
                {"name": "空气", "kind": "space", "length_mm": 40, "n": 1}]
    parts = [{"element_index": 1, "source": "物镜座", "locked_parameters": ["focal_mm"]}]
    if duplicates:
        elements += [deepcopy(elements[1]), deepcopy(elements[2])]
        parts.append({**parts[0], "element_index": 3})
    model = {"title": "透镜台架", "system": {"mode": "single_pass", "wavelength_nm": 633,
             "input_radius_mm": .3, "elements": elements}, "parts": parts}
    if arm:
        model["system"]["elements"] = [{"name": "接入口", "uid": "root", "kind": "space", "length_mm": 30, "n": 1}]
        model["parts"] = []
        model["incident_paths"] = [{"uid": "branch", "name": "耦合支路", "target_uid": "root", "through_uid": "root",
                                    "wavelength_nm": 532, "elements": elements, "parts": parts}]
    receipt = client.post(base, json={"change_note": "台架原件", "project": model})
    assert receipt.status_code == 200, receipt.text
    revision = receipt.json()["revision_id"]
    row = db.get(GeneratedContent, revision)
    record = deepcopy(row.content)
    before = record["project"]
    path = before["incident_paths"][0] if arm else before["system"]
    bindings = path["parts"] if arm else before["parts"]
    for element in path["elements"]:
        element.pop("uid")
    for part in bindings:
        part.pop("inventory_id")
        part.pop("role")
    row.content = record
    db.commit()
    return base, revision, before


@pytest.mark.parametrize("arm", [False, True])
def test_public_save_and_snapshot_adoption_keep_anonymous_hardware_in_its_registered_slot(client, db, arm):
    base, revision, before = legacy_train(client, db, arm=arm)
    changed = deepcopy(before)
    optics = changed["incident_paths"][0]["elements"] if arm else changed["system"]["elements"]
    optics[0]["length_mm"], optics[2]["length_mm"] = 23.75, 36.25
    optics[1]["name"] = "镜座上的透镜"
    result = client.post(base + "/variants", json={"source_revision_id": revision, "base_revision_id": revision,
                                                    "project": changed, "variant_name": "镜座微调"})
    assert result.status_code == 200, result.text
    variant = result.json()
    if arm:
        saved = client.post(base + "/adopt-snapshot", json={"base_revision_id": revision, "snapshot_id": variant["revision_id"]})
    else:
        saved = client.post(base, json={"base_revision_id": revision, "change_note": "采用新位置", "project": variant["project"]})
    assert saved.status_code == 200, saved.text
    output = saved.json()["project"]
    path = output["incident_paths"][0] if arm else output["system"]
    bindings = path["parts"] if arm else output["parts"]
    assert path["elements"][1]["uid"] and path["elements"][1]["focal_mm"] == 60
    assert bindings[0]["locked_parameters"] == ["focal_mm"]
    assert path["elements"][0]["length_mm"] == 23.75
    assert client.get(base, params={"revision_id": revision}).json()["current"]["project"] == before


@pytest.mark.parametrize("change", ["delete", "reorder", "unlock", "focal", "role", "duplicate"])
def test_ambiguous_or_changed_anonymous_lock_gives_actionable_error_and_preserves_head(client, db, change):
    base, revision, before = legacy_train(client, db, duplicates=change == "duplicate")
    model = deepcopy(before)
    elements, part = model["system"]["elements"], model["parts"][0]
    if change == "delete":
        elements.pop(1)
        model["parts"].clear()
    elif change == "reorder":
        elements[0], elements[1] = elements[1], elements[0]
        part["element_index"] = 0
    elif change == "unlock":
        part["locked_parameters"] = []
    elif change == "focal":
        elements[1]["focal_mm"] = 72
    elif change == "role":
        part["role"] = "collimator"
    else:
        elements.insert(1, elements.pop(2))
        part["element_index"] = 2
        model["title"] = "两个镜座无法区分"
    saved = client.post(base, json={"base_revision_id": revision, "change_note": "尝试修改", "project": model})
    assert saved.status_code == 422, saved.text
    assert all(word in saved.json()["detail"] for word in ["锁定", "透镜", "工程编辑器"])
    assert client.get(base).json()["current"]["revision_id"] == revision
    assert client.get(base).json()["current"]["project"] == before


def test_identical_legacy_locks_allow_editing_notes_when_the_whole_path_is_unchanged(client, db):
    base, revision, before = legacy_train(client, db, duplicates=True)
    before["system"].pop("wavelength_nm")
    row = db.get(GeneratedContent, revision)
    row.content = {**row.content, "project": before}
    db.commit()
    changed = {**before, "notes": "原位置的两片镜仍在原镜座，仅补充记录"}
    saved = client.post(base, json={"base_revision_id": revision, "change_note": "补记录", "project": changed})
    assert saved.status_code == 200, saved.text
    optics = saved.json()["project"]
    assert optics["system"]["elements"][1]["uid"] != optics["system"]["elements"][3]["uid"]
    assert [p["element_index"] for p in optics["parts"]] == [1, 3]
    assert [p["locked_parameters"] for p in optics["parts"]] == [["focal_mm"], ["focal_mm"]]
    assert client.get(base, params={"revision_id": revision}).json()["current"]["project"] == before


def test_assigning_ids_to_an_unchanged_locked_crystal_keeps_measured_material(client, db):
    base, revision, original = legacy_train(client, db)
    case = db.get(ExperimentCase, int(base.split("/")[-2]))
    row = db.get(GeneratedContent, revision)
    before = deepcopy(original)
    before["system"]["elements"][1] = {"name": "增益段", "kind": "space", "length_mm": 3, "n": 1.8}
    before["parts"][0].update(role="crystal", locked_parameters=["kind", "length_mm"])
    row.content = {**row.content, "project": before}
    known = {"value": "Nd:YAG", "source": "实物检验单", "verification": "verified"}
    case.parameters = {**case.parameters, "design_facts": {"gain_material": known}}
    db.commit()
    changed = deepcopy(before)
    changed["system"]["elements"][0]["length_mm"] = 25
    saved = client.post(base, json={"base_revision_id": revision, "change_note": "移动晶体座", "project": changed})
    assert saved.status_code == 200, saved.text
    db.refresh(case)
    assert case.parameters["design_facts"].get("gain_material") == known

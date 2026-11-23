"""Element identity selects physical positions outside the original laser template."""

from app.models import ExperimentCase
from app.workbench.projects import SaveProject, save_project


def test_touching_copies_of_same_stock_are_separate_objects(client, db):
    case = ExperimentCase(title="相邻的同批实物", cavity_type="custom", goal="只更换被选中的样品", parameters={})
    db.add(case)
    db.commit()
    item = client.post("/api/inventory/items", json={"name":"two cut pieces","category":"gain_crystal","quantity":2,
        "crystal_spec":{"segments":[{"material":"Nd:YAG","role":"gain","length_mm":4}]}}).json()
    elements = [{"uid":uid,"kind":"space","name":"same name","length_mm":4,"n":1.8} for uid in ("A","B","C")]
    parts = [{"element_index":i,"role":"crystal","inventory_id":item["id"] if i < 2 else None,
              "source":"two samples from the same stock"} for i in range(3)]
    row = save_project(db, case.id, SaveProject(project={"title":"separate objects","assemblies":[], "parts":parts,
        "system":{"mode":"single_pass","input_radius_mm":.4,"wavelength_nm":1064,"elements":elements}},change_note="loaded"))
    url = f"/api/cases/{case.id}/optical-project/replace-component"
    request = {"base_revision_id":row["revision_id"],"inventory_id":item["id"],"change_note":"replace selected sample"}
    ambiguous = client.post(url,json={**request,"target_role":"crystal"})
    assert ambiguous.status_code == 422 and "多件独立实物" in ambiguous.text
    insufficient = client.post(url,json={**request,"target_element_uid":"C"})
    assert insufficient.status_code == 422 and "另占2件" in insufficient.text
    result = client.post(url,json={**request,"target_element_uid":"B"})
    assert result.status_code == 200, result.text
    saved = result.json()
    assert saved["replacement"]["old_component_length_mm"] == saved["replacement"]["new_component_length_mm"] == 4
    assert [saved["project"]["system"]["elements"][i] for i in (0,2)] == [row["project"]["system"]["elements"][i] for i in (0,2)]


def test_crystal_uid_selects_only_one_contiguous_physical_group_with_same_inventory(client, db):
    case = ExperimentCase(title="双晶体光路", cavity_type="custom", goal="替换第二块实物", parameters={})
    db.add(case)
    db.commit()
    payload = {
        "name": "两块相同复合晶体",
        "category": "gain_crystal",
        "quantity": 2,
        "crystal_spec": {
            "width_mm": 3,
            "height_mm": 3,
            "segments": [
                {"material": "YAG", "role": "undoped", "length_mm": 2},
                {"material": "Nd:YAG", "role": "gain", "length_mm": 5},
            ],
        },
    }
    item = client.post("/api/inventory/items", json=payload).json()
    n = 1.8161498458805496
    before = save_project(
        db,
        case.id,
        SaveProject(
            project={
                "title": "分离实物",
                "system": {
                    "mode": "standing_wave",
                    "wavelength_nm": 1064,
                    "elements": [
                        {"name": "M1", "kind": "mirror", "radius_mm": 200},
                        {"name": "前隙", "kind": "space", "length_mm": 3},
                        {"name": "第一块端帽", "kind": "space", "length_mm": 2, "n": n},
                        {"name": "第一块增益", "kind": "space", "length_mm": 5, "n": n},
                        {"name": "两块之间", "kind": "space", "length_mm": 5},
                        {"name": "第二块端帽", "kind": "space", "length_mm": 2, "n": n},
                        {"name": "第二块增益", "kind": "space", "length_mm": 5, "n": n},
                        {"name": "后隙", "kind": "space", "length_mm": 12},
                        {"name": "M2", "kind": "mirror"},
                    ],
                },
                "parts": [
                    {
                        "element_index": i,
                        "role": "crystal",
                        "inventory_id": item["id"],
                        "ownership": "owned",
                        "source": "两块独立实物",
                        "locked_parameters": ["kind", "length_mm"],
                    }
                    for i in (2, 3, 5, 6)
                ],
            },
            change_note="两块相同库存、独立位置",
        ),
    )
    chosen = before["project"]["system"]["elements"][6]["uid"]
    result = client.post(
        f"/api/cases/{case.id}/optical-project/replace-component",
        json={
            "base_revision_id": before["revision_id"],
            "inventory_id": item["id"],
            "target_element_uid": chosen,
            "change_note": "只选第二块，不吞并第一块",
        },
    )
    assert result.status_code == 200, result.text
    saved = result.json()
    for i in (0, 1, 2, 3, 4, 7, 8):
        assert saved["project"]["system"]["elements"][i] == before["project"]["system"]["elements"][i]
    context = saved["project"]["component_context"]
    assert len(context["crystal_element_uids"]) == 2
    assert before["project"]["system"]["elements"][2]["uid"] not in context["crystal_element_uids"]
    assert any("多块分离晶体" in w for w in saved["scope_warnings"])
    # A later replacement of the first physical group keeps the second snapshot.
    second = client.post(
        f"/api/cases/{case.id}/optical-project/replace-component",
        json={
            "base_revision_id": saved["revision_id"],
            "inventory_id": item["id"],
            "target_element_uid": before["project"]["system"]["elements"][2]["uid"],
            "change_note": "再单独选用第一块",
        },
    )
    assert second.status_code == 200, second.text
    assert len(second.json()["project"]["component_context"]["crystal_groups"]) == 2
    for i in (4, 5, 6):
        assert second.json()["project"]["system"]["elements"][i] == saved["project"]["system"]["elements"][i]


def test_replace_arbitrary_lens_by_identity_preserves_other_positions_and_quantity(client, db):
    case = ExperimentCase(title="全链路透镜", cavity_type="custom", goal="提高泵浦耦合", parameters={})
    db.add(case)
    db.commit()
    before = save_project(
        db,
        case.id,
        SaveProject(
            project={
                "title": "光纤至晶体耦合",
                "system": {
                    "mode": "single_pass",
                    "wavelength_nm": 808,
                    "input_radius_mm": 0.005,
                    "elements": [
                        {"name": "光纤至准直镜", "kind": "space", "length_mm": 50},
                        {"name": "准直镜", "kind": "lens", "focal_mm": 50},
                        {"name": "镜间距", "kind": "space", "length_mm": 120},
                        {"name": "聚焦镜", "kind": "lens", "focal_mm": 100},
                        {"name": "至晶体", "kind": "space", "length_mm": 100},
                    ],
                },
                "parts": [
                    {
                        "element_index": i,
                        "role": "pump_lens",
                        "source": "已有透镜",
                        "ownership": "owned",
                        "locked_parameters": ["kind", "focal_mm"],
                    }
                    for i in (1, 3)
                ],
            },
            change_note="两片现有透镜",
        ),
    )
    uid1, uid2 = [before["project"]["system"]["elements"][i]["uid"] for i in (1, 3)]
    incomplete = client.post(
        "/api/inventory/items", json={"name": "R50普通玻璃", "category": "lens", "roc_mm": 50}
    ).json()
    denied = client.post(
        f"/api/cases/{case.id}/optical-project/replace-component",
        json={
            "base_revision_id": before["revision_id"],
            "inventory_id": incomplete["id"],
            "target_element_uid": uid1,
            "change_note": "未知玻璃不得只从R猜焦距",
        },
    )
    assert denied.status_code == 422 and "焦距未知" in denied.text
    item = client.post(
        "/api/inventory/items", json={"name": "FL75透镜", "category": "lens", "focal_length_mm": 75, "quantity": 1}
    ).json()
    endpoint = f"/api/cases/{case.id}/optical-project/replace-component"
    response = client.post(
        endpoint,
        json={
            "base_revision_id": before["revision_id"],
            "inventory_id": item["id"],
            "target_element_uid": uid1,
            "change_note": "只换准直镜",
        },
    )
    assert response.status_code == 200, response.text
    saved = response.json()
    updated = saved["project"]["system"]["elements"]
    assert updated[1]["uid"] != uid1 and updated[1]["focal_mm"] == 75
    for index in (0, 2, 3, 4):
        assert updated[index] == before["project"]["system"]["elements"][index]
    assert saved["replacement"]["previous_target_element_uid"] == uid1
    assert saved["replacement"]["new_path_length_mm"] == 270
    assert "new_mirror_spacing_mm" not in saved["replacement"]
    assert (
        saved["analysis"]["planes"]["tangential"]["output"]["radius_mm"]
        != before["analysis"]["planes"]["tangential"]["output"]["radius_mm"]
    )
    denied = client.post(
        endpoint,
        json={
            "base_revision_id": saved["revision_id"],
            "inventory_id": item["id"],
            "target_element_uid": uid2,
            "change_note": "不能一片镜子占两处",
        },
    )
    assert denied.status_code == 422 and "另占1件" in denied.text
    assert client.patch(f"/api/inventory/items/{item['id']}", json={"quantity": 2}).status_code == 200
    accepted = client.post(
        endpoint,
        json={
            "base_revision_id": saved["revision_id"],
            "inventory_id": item["id"],
            "target_element_uid": uid2,
            "change_note": "库存两件可独立使用",
        },
    )
    assert accepted.status_code == 200, accepted.text
    db.refresh(case)
    assert len([key for key in case.parameters["component_selection"] if key.startswith("element:")]) == 2
    assert "input_roc_mm" not in case.parameters.get("design_facts", {})


def test_replace_fold_mirror_by_identity_preserves_other_mirror_and_rejects_stale_uid(client, db):
    case = ExperimentCase(title="折叠腔", cavity_type="custom", goal="环形腔模式", parameters={})
    db.add(case)
    db.commit()
    saved = save_project(
        db,
        case.id,
        SaveProject(
            project={
                "title": "折叠镜工程",
                "system": {
                    "mode": "ring",
                    "wavelength_nm": 1064,
                    "elements": [
                        {"name": "支路1", "kind": "space", "length_mm": 50},
                        {"name": "折叠镜A", "kind": "mirror", "radius_mm": 200, "aoi_deg": 20},
                        {"name": "支路2", "kind": "space", "length_mm": 50},
                        {"name": "折叠镜B", "kind": "mirror", "radius_mm": 200, "aoi_deg": 20},
                    ],
                },
                "parts": [
                    {"element_index": i, "role": "fold_mirror", "source": "已有", "ownership": "owned"} for i in (1, 3)
                ],
            },
            change_note="环形系统",
        ),
    )
    uid = saved["project"]["system"]["elements"][1]["uid"]
    item = client.post("/api/inventory/items", json={"name": "R300折叠镜", "category": "mirror", "roc_mm": 300}).json()
    result = client.post(
        f"/api/cases/{case.id}/optical-project/replace-component",
        json={
            "base_revision_id": saved["revision_id"],
            "inventory_id": item["id"],
            "target_element_uid": uid,
            "change_note": "换A不换B",
        },
    )
    assert result.status_code == 200, result.text
    current = result.json()
    assert current["project"]["system"]["elements"][1]["radius_mm"] == 300
    assert current["project"]["system"]["elements"][1]["aoi_deg"] == 20
    assert current["project"]["system"]["elements"][3] == saved["project"]["system"]["elements"][3]
    stale = client.post(
        f"/api/cases/{case.id}/optical-project/replace-component",
        json={
            "base_revision_id": current["revision_id"],
            "inventory_id": item["id"],
            "target_element_uid": uid,
            "change_note": "旧UID已不可用",
        },
    )
    assert stale.status_code == 422 and "旧版本" in stale.text

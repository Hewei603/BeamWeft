"""User-facing inventory and scoped replacement, without production data or LLMs."""

import asyncio
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.inventory.crystals import ManualItemCreate
from app.models import AgentTask, ExperimentCase, InventoryItem
from app.workbench.projects import OpticalProject, ReplaceComponentTool, SaveProject, read_project, save_project
from app.workbench.service import context_for_case, session_for_case
from app.workbench.tools import execute
from app.physics.case_context import compute_case_physics
from app.design.service import inventory_snapshot


def crystal_payload():
    return {
        "category": "gain_crystal",
        "name": "CRYSLASER JBB4260",
        "vendor": "CRYSLASER",
        "quantity": 1,
        "raw_spec": "3×3×(4+10+4+2) mm；T=90%±2%，含义/波长未确认；掺杂浓度遮挡。",
        "crystal_spec": {
            "width_mm": 3,
            "height_mm": 3,
            "coating_note": "未知",
            "notes": "标签顺序未必等于实物泵浦朝向",
            "segments": [
                {"material": "YAG", "role": "undoped", "length_mm": 4},
                {
                    "material": "Nd:YAG",
                    "role": "gain",
                    "length_mm": 10,
                    "doping_pct": None,
                    "uncertain": True,
                    "note": "浓度疑似1.0%，未确认",
                },
                {"material": "YAG", "role": "undoped", "length_mm": 4},
                {"material": "Cr:YAG", "role": "saturable_absorber", "length_mm": 2, "note": "T90%±2%意义与波长待确认"},
            ],
        },
    }


def old_project(db):
    case = ExperimentCase(
        title="元件更换验收",
        description="已有M1 R200和M2平镜",
        cavity_type="linear",
        goal="提高平均输出功率",
        parameters={
            "design_facts": {
                "gain_material": {"value": "Nd:YVO4"},
                "crystal_length_mm": {"value": 13},
                "active_length_mm": {"value": 8},
                "front_cap_mm": {"value": 0},
                "rear_cap_mm": {"value": 5},
                "doping_pct": {"value": 0.4},
                "ar_wavelengths_nm": {"value": [808, 1064]},
                "oc_transmission_pct": {"value": 5},
                "pump_power_w": {"value": 6},
            }
        },
    )
    db.add(case)
    db.commit()
    elements = [
        {"name": "M1 R200", "kind": "mirror", "radius_mm": 200},
        {"name": "前隙", "kind": "space", "length_mm": 3},
        {"name": "旧Nd:YVO4", "kind": "space", "length_mm": 8, "n": 2.1648721403743165},
        {"name": "旧端帽", "kind": "space", "length_mm": 5, "n": 2.1648721403743165},
        {"name": "后隙", "kind": "space", "length_mm": 12},
        {"name": "M2平镜", "kind": "mirror", "radius_mm": None},
    ]
    project = OpticalProject(
        title="旧13mm工程",
        system={"mode": "standing_wave", "wavelength_nm": 1064, "elements": elements},
        parts=[
            {
                "element_index": index,
                "role": role,
                "ownership": "owned",
                "source": "用户已有硬件",
                "locked_parameters": ["kind", "radius_mm", "aoi_deg"] if role != "crystal" else ["kind", "length_mm"],
            }
            for index, role in ((0, "pump_input"), (2, "crystal"), (3, "crystal"), (5, "laser_output"))
        ],
    )
    saved = save_project(db, case.id, SaveProject(project=project, change_note="旧参考布局"))
    return case, saved


def add_crystal(client):
    response = client.post("/api/inventory/items", json=crystal_payload())
    assert response.status_code == 201, response.text
    return response.json()


def test_inventory_partial_sections_preserve_independent_dimensions_and_label(client):
    url = "/api/inventory/items"
    payload = {"name":"unmarked slices","category":"gain_crystal","crystal_spec":{"overall_length_mm":8.5,
        "material_label":"tray R","width_mm":3,"segments":[{"length_mm":2},{"material":"p-17 YAG?","role":"gain"}]}}
    response = client.post(url, json=payload)
    assert response.status_code == 201, response.text
    item = response.json()
    assert item["material"] == "tray R" and item["thickness_mm"] == 8.5
    saved = item["crystal_spec"]
    assert [s["length_mm"] for s in saved["segments"]] == [2,None]
    assert [s["material"] for s in saved["segments"]] == [None,"p-17 YAG?"]
    assert [s["role"] for s in saved["segments"]] == [None,"gain"]
    assert client.patch(f"{url}/{item['id']}",json={"location":"station B"}).json()["crystal_spec"] == saved
    saved["segments"][1]["length_mm"] = 7
    revised = client.patch(f"{url}/{item['id']}",json={"crystal_spec":saved}).json()
    assert revised["thickness_mm"] == 8.5
    assert sum(s["length_mm"] for s in revised["crystal_spec"]["segments"]) == 9
    saved.update(overall_length_mm=None,material_label=None)
    withdrawn = client.patch(f"{url}/{item['id']}",json={"crystal_spec":saved}).json()
    assert withdrawn["thickness_mm"] == 9 and withdrawn["material"] is None
    assert not withdrawn["crystal_spec"].get("overall_length_mm")
    assert not withdrawn["crystal_spec"].get("material_label")
    rejected = client.patch(f"{url}/{item['id']}",json={"thickness_mm":10,"crystal_spec":{**saved,"overall_length_mm":11}})
    assert rejected.status_code == 422
    assert client.get(f"{url}/{item['id']}").json() == withdrawn
    # A record with the old schema has only generated top-level summaries.
    legacy = client.post(url,json={"name":"legacy","crystal_spec":{"segments":[{"material":"Nd:YAG","role":"gain","length_mm":5}]}}).json()
    legacy_url = f"{url}/{legacy['id']}"
    edited = client.patch(legacy_url,json={"location":"tray B"}).json()
    assert not ({"overall_length_mm","material_label"} & set(edited["crystal_spec"]))
    edited["crystal_spec"]["segments"][0].update(material=None,length_mm=None)
    withdrawn = client.patch(legacy_url,json={"crystal_spec":edited["crystal_spec"]}).json()
    assert withdrawn["thickness_mm"] is None and withdrawn["material"] is None
    assert withdrawn["crystal_spec"]["segments"][0]["role"] == "gain"


def test_replacing_verified_crystal_with_unidentified_sections_leaves_dependent_optics_pending(client, db):
    case, before = old_project(db)
    base = f"/api/cases/{case.id}/optical-project"
    item = client.post("/api/inventory/items",json={"name":"unidentified optic","category":"gain_crystal",
        "crystal_spec":{"material_label":"Nd:YAG on crate","segments":[
            {"role":"gain","length_mm":5,"material":"Not-YAG calibration C"},
            {"length_mm":3},{"length_mm":2,"material":"BK7"}]}}).json()
    args = {"inventory_id":item["id"],"target_role":"crystal","change_note":"use measured boundaries","base_revision_id":before["revision_id"]}
    result = client.post(base+"/replace-component",json=args)
    assert result.status_code == 200, result.text
    mounted = result.json()
    parts = [p for p in mounted["project"]["parts"] if p["role"] == "crystal"]
    optics = [mounted["project"]["system"]["elements"][p["element_index"]] for p in parts]
    assert [e["length_mm"] for e in optics] == [5,3,2]
    assert [e["n"] for e in optics[:2]] == [None,None]
    assert "没有可用的标量色散模型" in " ".join(parts[0]["unknowns"])
    assert "材料未确认" in " ".join(parts[1]["unknowns"])
    assert "折射率" not in " ".join(parts[2]["unknowns"])
    assert optics[2]["n"] == pytest.approx(1.506634801642448,abs=1e-7)
    assert all(e["inventory_id"] == item["id"] for e in parts)
    assert not mounted["analysis"]["planes"]
    db.refresh(case)
    facts = case.parameters["design_facts"]
    assert set(facts).isdisjoint({"gain_material","active_length_mm","has_saturable_absorber","front_cap_mm","rear_cap_mm"})
    assert facts["crystal_length_mm"]["value"] == 10 and "gain_medium" not in case.parameters
    assert read_project(db,case.id,before["revision_id"])["project"] == before["project"]
    revised = deepcopy(item["crystal_spec"])
    revised["segments"][1].update(material="Nd:YAG",role="gain",length_mm=4)
    assert client.patch(f"/api/inventory/items/{item['id']}",json={"crystal_spec":revised,"cut_axis":"c","raw_spec":"later certification"}).status_code == 200
    snapshot = next(row for row in inventory_snapshot(db,case) if row["id"] == item["id"])
    assert snapshot["thickness_mm"] == 10 and snapshot["cut_axis"] is None and snapshot["raw_spec"] is None
    assert snapshot["crystal_spec"]["segments"][1]["material"] is None


def test_missing_segment_length_or_conflicting_measured_total_cannot_be_installed(client, db):
    case, before = old_project(db)
    base = f"/api/cases/{case.id}/optical-project"
    for segments, expected in (([{"role":"gain"},{"length_mm":2}],"第 1 段"),([{"length_mm":6}],"不一致")):
        item = client.post("/api/inventory/items",json={"name":"incomplete","category":"gain_crystal",
            "crystal_spec":{"overall_length_mm":7,"segments":segments}}).json()
        args = {"inventory_id":item["id"],"target_role":"crystal","change_note":"mount","base_revision_id":before["revision_id"]}
        result = client.post(base+"/replace-component",json=args)
        assert result.status_code == 422 and expected in result.text
        assert read_project(db,case.id)["revision_id"] == before["revision_id"]


def test_whole_part_length_only_can_locate_outer_faces_without_creating_material_facts(client, db):
    case, before = old_project(db)
    base = f"/api/cases/{case.id}/optical-project"
    item = client.post("/api/inventory/items",json={"name":"closed assembly","category":"gain_crystal",
        "crystal_spec":{"overall_length_mm":13,"segments":[]}}).json()
    args = {"inventory_id":item["id"],"target_role":"crystal","change_note":"outer faces","base_revision_id":before["revision_id"]}
    response = client.post(base+"/replace-component",json=args)
    assert response.status_code == 200, response.text
    mounted = response.json()
    part, = [p for p in mounted["project"]["parts"] if p["role"] == "crystal"]
    element = mounted["project"]["system"]["elements"][part["element_index"]]
    assert element["length_mm"] == 13 and element["n"] is None
    assert mounted["project"]["component_context"]["crystal_spec"]["segments"] == []
    assert "has_saturable_absorber" not in case.parameters["design_facts"]


def test_inventory_crystal_can_be_installed_before_the_working_wavelength_is_known(client, db):
    case, original = old_project(db)
    base = f"/api/cases/{case.id}/optical-project"
    project = deepcopy(original["project"])
    project["system"]["wavelength_nm"] = None
    before = client.post(base,json={"project":project,"base_revision_id":original["revision_id"],"change_note":"待定波长"}).json()
    inventory_id = add_crystal(client)["id"]
    args = {"inventory_id":inventory_id,"target_role":"crystal","change_note":"领取实物"}
    replaced = client.post(base+"/replace-component",json={**args,"base_revision_id":before["revision_id"]})
    assert replaced.status_code == 200, replaced.text
    mounted = replaced.json()
    project = deepcopy(mounted["project"])
    segments = [project["system"]["elements"][p["element_index"]] for p in project["parts"] if p["role"]=="crystal"]
    assert [el["length_mm"] for el in segments] == [4,10,4,2]
    assert all(el["n"] is None for el in segments) and not mounted["analysis"]["planes"]
    project["system"]["wavelength_nm"] = 1064
    measured = client.post(base,json={"project":project,"base_revision_id":mounted["revision_id"],"change_note":"补实测波长"})
    assert measured.status_code == 200, measured.text
    derived = client.post(base+"/replace-component",json={**args,"base_revision_id":measured.json()["revision_id"]})
    assert derived.status_code == 200, derived.text
    assert all(s["model_index"] > 1 for s in derived.json()["project"]["component_context"]["crystal_sections"])
    assert read_project(db,case.id,mounted["revision_id"])["project"]["system"]["wavelength_nm"] is None


def test_crystal_replacement_retires_wavelength_specific_pump_index(client, db):
    case, original = old_project(db)
    base = f"/api/cases/{case.id}/optical-project"
    project = deepcopy(original["project"])
    old_uid = project["system"]["elements"][2]["uid"]
    project["observation_planes"] = [{"uid": "probe", "name": "in old crystal", "target_uid": old_uid, "depth_mm": 1}]
    project["incident_paths"] = [{"uid": "pump", "name": "808 pump", "target_uid": old_uid, "wavelength_nm": 808,
                                  "target_n": 2.2, "tangential": {"radius_mm": .25, "wavefront": "waist", "m2": 1}}]
    before = client.post(base, json={"base_revision_id": original["revision_id"], "project": project, "change_note": "measure pump"}).json()
    assert before["analysis"]["sections"]["status"] == "completed"
    response = client.post(base + "/replace-component", json={"base_revision_id": before["revision_id"],
        "inventory_id": add_crystal(client)["id"], "target_role": "crystal", "orientation": "as_listed", "change_note": "swap material"})
    assert response.status_code == 200, response.text
    after = response.json()
    branch = after["project"]["incident_paths"][0]
    assert branch["uid"] == "pump" and branch["target_n"] is None
    assert after["analysis"]["sections"]["planes"][0]["beams"] == []
    assert any("808 pump" in warning and "新入口" in warning for warning in after["scope_warnings"])
    assert read_project(db, case.id, before["revision_id"])["project"]["incident_paths"][0]["target_n"] == 2.2


def test_manual_inventory_retains_uncertainty_and_does_not_misclassify_T(client):
    item = add_crystal(client)
    assert item["thickness_mm"] == 20
    assert item["doping_pct"] is None
    assert item["coatings"] == []
    assert item["crystal_spec"]["segments"][1]["uncertain"]
    assert item["crystal_spec"]["segments"][-1]["initial_transmission_pct"] is None
    assert "T=90%" in item["raw_spec"]
    assert client.get(f"/api/inventory/items/{item['id']}").json()["crystal_spec"] == item["crystal_spec"]
    edited = client.patch(f"/api/inventory/items/{item['id']}", json={"location": "光学平台A"}).json()
    assert edited["location"] == "光学平台A"
    assert edited["crystal_spec"] == item["crystal_spec"]
    incomplete = client.post("/api/inventory/items", json={"name": "新晶体，尺寸待测", "category": "gain_crystal"})
    assert incomplete.status_code == 201
    assert incomplete.json()["parse_confidence"] == "partial"


def test_invalid_crystal_role_dimension_and_nonfinite_are_rejected(client):
    payload = crystal_payload()
    payload["crystal_spec"]["segments"][-1]["role"] = "undoped"
    assert client.post("/api/inventory/items", json=payload).status_code == 422
    payload = crystal_payload()
    payload["crystal_spec"]["segments"][0]["length_mm"] = -4
    assert client.post("/api/inventory/items", json=payload).status_code == 422
    with pytest.raises(ValidationError):
        ManualItemCreate(name="bad", category="mirror", roc_mm=float("nan"))
    with pytest.raises(ValidationError):
        ManualItemCreate(name="misidentified gain piece",category="gain_crystal",crystal_spec={
            "width_mm":3,"height_mm":3,"segments":[{"material":"Cr:YAG","role":"saturable_absorber","length_mm":2}]})


def test_passive_inventory_keeps_cut_and_normal_thickness_without_claiming_gain_mount(client, db):
    case, before = old_project(db)
    inventory = "/api/inventory/items"
    spec = {"segments":[{"role":"undoped","material":"fused_silica","length_mm":2},
                        {"role":"saturable_absorber","material":"Cr:YAG","length_mm":1}]}
    for category in ("window","nonlinear_crystal","crystal_other"):
        result = client.post(inventory,json={"name":"witness "+category,"category":category,"crystal_spec":spec,
            "dimensions":"22 mm body; clear 16 mm", "doping_pct":0.05,
            "cut_axis":"reference x", "cut_angle_theta_deg":-2.5, "cut_angle_phi_deg":317.75})
        assert result.status_code == 201, result.text
        item = result.json()
        modified = client.patch(f"{inventory}/{item['id']}",json={"raw_spec":"metrology certificate"}).json()
        for field in ("category","crystal_spec","material","cut_axis","cut_angle_theta_deg","cut_angle_phi_deg","dimensions","doping_pct"):
            assert modified[field] == item[field]
        assert modified["dimensions"] == "22 mm body; clear 16 mm" and modified["doping_pct"] == 0.05
        assert modified["crystal_spec"]["segments"][0]["doping_pct"] is None
        assert modified["thickness_mm"] == 3 and modified["cut_angle_theta_deg"] == -2.5
        response = client.post(f"/api/cases/{case.id}/optical-project/replace-component",json={
            "inventory_id":item["id"],"target_role":"crystal","base_revision_id":before["revision_id"],"change_note":"compare eligibility"})
        assert response.status_code == 422 and "类别" in response.text


def test_crystal_replacement_preserves_hardware_and_revises_case_facts(client, db):
    case, before = old_project(db)
    original = deepcopy(before)
    item = add_crystal(client)
    response = client.post(
        f"/api/cases/{case.id}/optical-project/replace-component",
        json={
            "base_revision_id": before["revision_id"],
            "inventory_id": item["id"],
            "target_role": "crystal",
            "orientation": "as_listed",
            "change_note": "换成新标签复合晶体，前后空气隙保持",
        },
    )
    assert response.status_code == 200, response.text
    saved = response.json()
    elements = saved["project"]["system"]["elements"]
    assert saved["parent_revision_id"] == before["revision_id"]
    assert saved["replacement"]["old_component_length_mm"] == 13
    assert saved["replacement"]["new_component_length_mm"] == 20
    assert saved["replacement"]["new_mirror_spacing_mm"] == 35
    assert saved["analysis"]["stable_both_planes"]
    assert saved["analysis"]["planes"]["tangential"]["m"] == pytest.approx(0.7397861184160475)
    for old, new in ((0, 0), (1, 1), (4, 6), (5, 7)):
        assert original["project"]["system"]["elements"][old] == elements[new]
    assert not {e["uid"] for e in original["project"]["system"]["elements"][2:4]} & {e["uid"] for e in elements[2:6]}
    assert read_project(db, case.id, before["revision_id"])["project"] == original["project"]
    assert [s["role"] for s in saved["project"]["component_context"]["crystal_spec"]["segments"]] == [
        "undoped",
        "gain",
        "undoped",
        "saturable_absorber",
    ]
    assert any("动力学" in s for s in saved["scope_warnings"])
    assert any("动力学" in s for s in saved["analysis"]["scope"])
    db.refresh(case)
    facts = case.parameters["design_facts"]
    assert facts["gain_material"]["value"] == "Nd:YAG"
    assert facts["crystal_length_mm"]["value"] == 20
    assert facts["active_length_mm"]["value"] == 10
    assert facts["rear_cap_mm"]["value"] == 4  # Cr is not silently folded into cap
    assert facts["has_saturable_absorber"]["value"] is True
    assert facts["absorber_length_mm"]["value"] == 2
    assert "doping_pct" not in facts and "ar_wavelengths_nm" not in facts
    assert "absorber_initial_transmission_pct" not in facts
    assert facts["oc_transmission_pct"]["value"] == 5  # unrelated mirror T is retained
    assert facts["pump_power_w"]["value"] == 6
    assert case.parameters["component_selection"]["crystal"] == item["id"]
    sid = case.parameters["workbench_session_id"]
    current = context_for_case(db, case, sid)["current_optical_project"]
    assert current["revision_id"] == saved["revision_id"]
    assert current["project"]["component_context"]["crystal_inventory_id"] == item["id"]
    # Editing the inventory later must not rewrite a historical model snapshot.
    revised = deepcopy(item["crystal_spec"])
    revised["segments"][0]["length_mm"] = 6
    assert client.patch(f"/api/inventory/items/{item['id']}", json={"crystal_spec": revised}).status_code == 200
    assert (
        read_project(db, case.id, saved["revision_id"])["project"]["component_context"]["crystal_spec"]["segments"][0][
            "length_mm"
        ]
        == 4
    )


def test_replacement_rejects_stale_revision_active_run_and_unknown_curve(client, db):
    case, before = old_project(db)
    item = add_crystal(client)
    endpoint = f"/api/cases/{case.id}/optical-project/replace-component"
    request = {
        "base_revision_id": before["revision_id"],
        "inventory_id": item["id"],
        "target_role": "crystal",
        "change_note": "替换",
    }
    assert client.post(endpoint, json={**request, "base_revision_id": before["revision_id"] + 100}).status_code == 409
    running = AgentTask(case_id=case.id, mode="workbench", status="running", goal="已有任务")
    db.add(running)
    db.commit()
    assert client.post(endpoint, json=request).status_code == 409
    running.status = "completed"
    db.commit()
    mirror = client.post("/api/inventory/items", json={"name": "曲率未知镜", "category": "mirror"}).json()
    denied = client.post(endpoint, json={**request, "inventory_id": mirror["id"], "target_role": "pump_input"})
    assert denied.status_code == 422 and "曲率未知" in denied.text
    assert read_project(db, case.id)["revision_id"] == before["revision_id"]


def test_mirror_replacement_only_changes_selected_surface_and_binding(client, db):
    case, before = old_project(db)
    mirror = client.post(
        "/api/inventory/items",
        json={
            "name": "新R250输入镜",
            "category": "mirror",
            "roc_mm": 250,
            "coatings": [{"surface": "S1", "wl_min_nm": 1064, "wl_max_nm": 1064, "function": "HR"}],
        },
    ).json()
    response = client.post(
        f"/api/cases/{case.id}/optical-project/replace-component",
        json={
            "base_revision_id": before["revision_id"],
            "inventory_id": mirror["id"],
            "target_role": "pump_input",
            "change_note": "换输入镜",
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["project"]["system"]["elements"][0]["radius_mm"] == 250
    assert result["project"]["system"]["elements"][1:] == before["project"]["system"]["elements"][1:]
    assert result["replacement"]["new_mirror_spacing_mm"] == 28
    assert any("基底" in w and "1064 nm" in w for w in result["scope_warnings"])
    assert not any("808 nm" in w for w in result["scope_warnings"])
    db.refresh(case)
    assert case.parameters["design_facts"]["input_roc_mm"]["value"] == 250
    assert case.parameters["design_facts"]["gain_material"]["value"] == "Nd:YVO4"
    # A new flat output mirror with a different known coating keeps geometry
    # identical, while the application explicitly refrains from predicting power.
    flat = client.post(
        "/api/inventory/items",
        json={
            "name": "新平面T10输出镜",
            "category": "mirror",
            "roc_is_flat": True,
            "coatings": [
                {
                    "surface": "S1",
                    "wl_min_nm": 1064,
                    "wl_max_nm": 1064,
                    "function": "PR",
                    "value_type": "T",
                    "comparator": "=",
                    "value_pct": 10,
                }
            ],
        },
    ).json()
    second = client.post(
        f"/api/cases/{case.id}/optical-project/replace-component",
        json={
            "base_revision_id": result["revision_id"],
            "inventory_id": flat["id"],
            "target_role": "laser_output",
            "change_note": "换输出镜",
        },
    ).json()
    assert second["analysis"]["planes"]["tangential"]["m"] == result["analysis"]["planes"]["tangential"]["m"]
    assert any("未计算其对输出功率" in w for w in second["scope_warnings"])
    db.refresh(case)
    assert case.parameters["design_facts"]["oc_transmission_pct"]["value"] == 10


def test_same_physical_mirror_cannot_fill_both_roles_without_quantity(client, db):
    case, before = old_project(db)
    mirror = client.post(
        "/api/inventory/items", json={"name": "一件R250镜", "category": "mirror", "roc_mm": 250, "quantity": 1}
    ).json()
    url = f"/api/cases/{case.id}/optical-project/replace-component"
    first = client.post(
        url,
        json={
            "base_revision_id": before["revision_id"],
            "inventory_id": mirror["id"],
            "target_role": "pump_input",
            "change_note": "选为输入镜",
        },
    ).json()
    request = {
        "base_revision_id": first["revision_id"],
        "inventory_id": mirror["id"],
        "target_role": "laser_output",
        "change_note": "也选为输出镜",
    }
    response = client.post(url, json=request)
    assert response.status_code == 422 and "同一件实物" in response.text
    assert read_project(db, case.id)["revision_id"] == first["revision_id"]
    assert client.patch(f"/api/inventory/items/{mirror['id']}", json={"quantity": 2}).status_code == 200
    second = client.post(url, json=request)
    assert second.status_code == 200, second.text


def test_all_physics_consumers_use_current_project_not_old_scalar_facts(client, db):
    case, before = old_project(db)
    item = add_crystal(client)
    response = client.post(
        f"/api/cases/{case.id}/optical-project/replace-crystal",
        json={"base_revision_id": before["revision_id"], "inventory_id": item["id"], "change_note": "新晶体"},
    )
    assert response.status_code == 200, response.text
    saved = response.json()
    db.refresh(case)
    # Deliberately stale old parameters must not override the new full model.
    params = {
        **case.parameters,
        "R1_mm": 100,
        "R2_mm": "flat",
        "L_mm": 26,
        "crystal": {"n": 2.16, "thickness_mm": 11, "position_mm": 3},
    }
    physics = compute_case_physics(params)
    assert physics["available"]
    assert "cavity_design_search" not in physics["results"]
    assert "cavity_design" not in physics["results"]
    result = physics["results"]["current_optical_project"]
    assert result["project_revision_id"] == saved["revision_id"]
    assert result["planes"]["tangential"]["m"] == saved["analysis"]["planes"]["tangential"]["m"]
    assert any("动力学" in warning for warning in result["scope"])
    broken = compute_case_physics({**params, "current_optical_system": {"mode": "invalid"}})
    assert not broken["available"]
    assert "cavity_design_search" not in broken["results"]


def test_additive_schema_upgrades_existing_inventory_without_erasing_rows(tmp_path):
    from sqlalchemy import Column, MetaData, Table, create_engine, inspect, text
    from sqlalchemy.orm import Session
    from app.database import sync_additive_schema

    database = create_engine("sqlite:///" + str(tmp_path / "old-inventory.db"))
    old_metadata = MetaData()
    Table(
        "inventory_items",
        old_metadata,
        *[
            Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable, server_default=c.server_default)
            for c in InventoryItem.__table__.columns
            if c.name != "crystal_spec"
        ],
    )
    old_metadata.create_all(database)
    with database.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO inventory_items(id,name,category,raw_spec,quantity) VALUES(7,'原库存','gain_crystal','旧标签原文',1)"
            )
        )
    assert "crystal_spec" not in {c["name"] for c in inspect(database).get_columns("inventory_items")}
    changes = sync_additive_schema(database)
    assert any("crystal_spec" in change for change in changes)
    assert sync_additive_schema(database) == []
    with Session(database) as session:
        original = session.get(InventoryItem, 7)
        assert original.name == "原库存" and original.raw_spec == "旧标签原文"
        assert original.crystal_spec is None
    database.dispose()


def test_agent_requires_current_user_quote_for_replacement(client, db):
    case, before = old_project(db)
    item = add_crystal(client)
    task = AgentTask(case_id=case.id, mode="workbench", status="running", goal="把晶体换成我刚选择的新晶体")
    db.add(task)
    db.commit()
    session = session_for_case(db, case, None)
    db.commit()
    request = {
        "base_revision_id": before["revision_id"],
        "inventory_id": item["id"],
        "target_role": "crystal",
        "orientation": "reversed",
        "change_note": "用户本轮指示更换",
        "source_quote": "旧任务中的替换指示",
    }
    with pytest.raises(ValueError, match="本轮用户原话"):
        asyncio.run(
            execute(
                "replace_component",
                ReplaceComponentTool(**request),
                db=db,
                case=case,
                task=task,
                provider=None,
                session_id=session.id,
                attachment_ids=[],
            )
        )
    assert read_project(db, case.id)["revision_id"] == before["revision_id"]
    request["source_quote"] = "把晶体换成我刚选择的新晶体"
    result = asyncio.run(
        execute(
            "replace_component",
            ReplaceComponentTool(**request),
            db=db,
            case=case,
            task=task,
            provider=None,
            session_id=session.id,
            attachment_ids=[],
        )
    )
    assert result["project"]["component_context"]["crystal_spec"]["segments"][0]["role"] == "saturable_absorber"
    assert result["project"]["system"]["elements"][2]["name"].startswith("Cr:YAG")


def test_loans_reduce_available_components_for_agent_and_frontend(client, db):
    case, before = old_project(db)
    mirror = client.post(
        "/api/inventory/items", json={"name": "两件R250镜", "category": "mirror", "roc_mm": 250, "quantity": 2}
    ).json()
    assert (
        client.post(
            f"/api/inventory/items/{mirror['id']}/borrow", json={"borrower_name": "同事", "quantity": 1}
        ).status_code
        == 200
    )
    endpoint = f"/api/cases/{case.id}/optical-project/replace-component"
    first = client.post(
        endpoint,
        json={
            "base_revision_id": before["revision_id"],
            "inventory_id": mirror["id"],
            "target_role": "pump_input",
            "change_note": "选用剩下一件",
        },
    ).json()
    denied = client.post(
        endpoint,
        json={
            "base_revision_id": first["revision_id"],
            "inventory_id": mirror["id"],
            "target_role": "laser_output",
            "change_note": "不能重复使用唯一剩余镜片",
        },
    )
    assert denied.status_code == 422 and "借出1" in denied.text
    assert read_project(db, case.id)["revision_id"] == first["revision_id"]
    assert client.patch(f"/api/inventory/items/{mirror['id']}", json={"quantity": 3}).status_code == 200
    accepted = client.post(
        endpoint,
        json={
            "base_revision_id": first["revision_id"],
            "inventory_id": mirror["id"],
            "target_role": "laser_output",
            "change_note": "库存三件扣借出一件，尚可用两件",
        },
    )
    assert accepted.status_code == 200, accepted.text


def test_restore_legacy_project_retracts_new_crystal_snapshot_and_facts(client, db):
    case, legacy = old_project(db)
    item = add_crystal(client)
    selected = client.post(
        f"/api/cases/{case.id}/optical-project/replace-crystal",
        json={"base_revision_id": legacy["revision_id"], "inventory_id": item["id"], "change_note": "换新晶体"},
    ).json()
    restore = client.post(
        f"/api/cases/{case.id}/optical-project",
        json={
            "base_revision_id": selected["revision_id"],
            "project": legacy["project"],
            "change_note": "恢复原13mm工程",
            "unlock_quote": "用户在工程编辑器明确解锁",
        },
    )
    assert restore.status_code == 200, restore.text
    db.refresh(case)
    assert "current_crystal" not in case.parameters
    assert "crystal" not in case.parameters["component_selection"]
    facts = case.parameters["design_facts"]
    assert facts["crystal_length_mm"]["value"] == 13
    for key in (
        "gain_material",
        "crystal_inventory_id",
        "has_saturable_absorber",
        "absorber_length_mm",
        "absorber_material",
        "doping_pct",
    ):
        assert key not in facts
    physics = compute_case_physics(case.parameters)["results"]["current_optical_project"]
    assert physics["planes"]["tangential"]["m"] == legacy["analysis"]["planes"]["tangential"]["m"]


def test_unlock_cannot_keep_false_segment_snapshot_and_restore_uses_frozen_spec(client, db):
    case, before = old_project(db)
    item = add_crystal(client)
    first = client.post(
        f"/api/cases/{case.id}/optical-project/replace-crystal",
        json={"base_revision_id": before["revision_id"], "inventory_id": item["id"], "change_note": "换20mm新晶体"},
    ).json()
    broken = deepcopy(first["project"])
    broken["system"]["elements"][2]["length_mm"] = 6
    denied = client.post(
        f"/api/cases/{case.id}/optical-project",
        json={
            "base_revision_id": first["revision_id"],
            "project": broken,
            "change_note": "只改段长",
            "unlock_quote": "用户在工程编辑器明确解锁",
        },
    )
    assert denied.status_code == 422 and "分段快照不一致" in denied.text
    assert read_project(db, case.id)["revision_id"] == first["revision_id"]
    edited = deepcopy(item["crystal_spec"])
    edited["segments"][0]["length_mm"] = 6
    assert client.patch(f"/api/inventory/items/{item['id']}", json={"crystal_spec": edited}).status_code == 200
    second = client.post(
        f"/api/cases/{case.id}/optical-project/replace-crystal",
        json={
            "base_revision_id": first["revision_id"],
            "inventory_id": item["id"],
            "change_note": "明确选用修改后的22mm分段",
        },
    ).json()
    restore = client.post(
        f"/api/cases/{case.id}/optical-project",
        json={
            "base_revision_id": second["revision_id"],
            "project": first["project"],
            "change_note": "恢复20mm历史条件",
            "unlock_quote": "用户在工程编辑器明确解锁",
        },
    )
    assert restore.status_code == 200, restore.text
    db.refresh(case)
    assert case.parameters["current_crystal"]["spec"]["segments"][0]["length_mm"] == 4
    assert case.parameters["design_facts"]["crystal_length_mm"]["value"] == 20
    assert (
        next(row for row in inventory_snapshot(db, case) if row["id"] == item["id"])["crystal_spec"]["segments"][0][
            "length_mm"
        ]
        == 4
    )
    assert client.get(f"/api/inventory/items/{item['id']}").json()["crystal_spec"]["segments"][0]["length_mm"] == 6


def test_crystal_identity_selects_all_segments_and_preserves_explicit_absorber_transmission(client, db):
    case, before = old_project(db)
    payload = crystal_payload()
    payload["crystal_spec"]["segments"][-1].update(initial_transmission_pct=90, initial_transmission_wavelength_nm=1064)
    item = client.post("/api/inventory/items", json=payload).json()
    uid = before["project"]["system"]["elements"][2]["uid"]
    response = client.post(
        f"/api/cases/{case.id}/optical-project/replace-component",
        json={
            "base_revision_id": before["revision_id"],
            "inventory_id": item["id"],
            "target_element_uid": uid,
            "change_note": "按明确测定T0的新复合晶体替换整块旧晶体",
        },
    )
    assert response.status_code == 200, response.text
    parts = [p for p in response.json()["project"]["parts"] if p["role"] == "crystal"]
    assert len(parts) == 4 and all(p["inventory_id"] == item["id"] for p in parts)
    db.refresh(case)
    assert case.parameters["design_facts"]["absorber_initial_transmission_pct"]["value"] == 90
    assert case.parameters["design_facts"]["absorber_transmission_wavelength_nm"]["value"] == 1064
    assert "oc_transmission_pct" in case.parameters["design_facts"]

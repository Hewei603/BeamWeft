import asyncio
from copy import deepcopy

import pytest

from app.models import AgentTask, ExperimentCase, GeneratedContent
from app.workbench.projects import PreviewReplacement, ReplaceComponentTool, SaveProject, read_project, save_project
from app.workbench.study_inputs import begin_study
from app.workbench.tools import execute


def lens(client, name, focal=None, quantity=1):
    response = client.post("/api/inventory/items", json={"category": "lens", "name": name,
                           "quantity": quantity, "focal_length_mm": focal, "raw_spec": name})
    assert response.status_code == 201, response.text
    return response.json()


def bench(db, occupied=None, crystal=False):
    case = ExperimentCase(title="交汇的光束", goal="沿选定波长核对探测截面", cavity_type="custom", parameters={})
    db.add(case)
    db.commit()
    surface = {"uid": "same/id", "name": "同名实物", "kind": "lens", "focal_mm": 100, "aoi_deg": 2}
    registration = {"uid": "station", "name": "检测面", "target_uid": "common", "depth_mm": 33}
    def arm(uid, wavelength):
        component = ({"uid": surface["uid"], "name": "样品", "kind": "space", "length_mm": 3.5, "n": 1.7}
                     if crystal and uid == "west" else dict(surface))
        part = {"element_index": 0, "role": "crystal" if component["kind"] == "space" else "lens",
                "source": "台架", "locked_parameters": ["kind", "length_mm" if component["kind"] == "space" else "focal_mm"]}
        if occupied and uid == "east":
            part.update(inventory_id=occupied, ownership="owned")
        return {"uid": uid, "name": "同名支路", "wavelength_nm": wavelength, "tangential": {"m2": 1,
                "wavefront": "waist", "radius_mm": .2}, "target_n": 1, "target_uid": "common",
                "elements": [component], "parts": [part]}

    project = {"title": "换件实测", "assumptions": ["东侧光源按束腰标定"], "notes": "台架记录",
               "system": {"mode": "single_pass", "wavelength_nm": 1040, "input_radius_mm": .3,
               "elements": [dict(surface), {"uid": "common", "kind": "space", "name": "共享段", "length_mm": 40}]},
               "incident_paths": [arm("west", 917), arm("east", 815)], "observation_planes": [registration]}
    saved = save_project(db, case.id, SaveProject(project=project, change_note="建立光路"))
    return case, saved


def test_path_owner_controls_candidates_live_preview_and_install(client, db):
    selected = lens(client, "New lens", 54)
    occupied = lens(client, "Other lens", 100)
    unknown = lens(client, "Unknown lens")
    case, saved = bench(db, occupied["id"])
    root = f"/api/cases/{case.id}/optical-project"
    target = {"target_path_uid": "west", "target_element_uid": "same/id"}
    def choices(**fields):
        response = client.get(root + "/replacement-candidates", params={"revision_id": saved["revision_id"], **target, **fields})
        assert response.status_code == 200, response.text
        return response.json()
    all_options = choices()
    rows = {item["id"]: item for item in all_options["items"]}
    assert all_options["wavelength_nm"] == 917 and rows[selected["id"]]["eligible"]
    assert "917" in rows[selected["id"]]["wavelength_note"]
    assert rows[occupied["id"]]["occupied_quantity"] == 1 and not rows[occupied["id"]]["eligible"]
    assert "焦距未知" in rows[unknown["id"]]["reason"]
    assert choices(search="other", limit=1)["total"] == 1
    assert choices(skip=2, limit=1)["items"][0]["id"] == unknown["id"]
    req = {**target, "base_revision_id": saved["revision_id"], "inventory_id": selected["id"]}
    before_count = db.query(GeneratedContent).filter_by(case_id=case.id).count()
    preview = client.post(root + "/replace-component/preview", json=req)
    assert preview.status_code == 200, preview.text
    result = preview.json()
    assert db.query(GeneratedContent).filter_by(case_id=case.id).count() == before_count
    assert result["project"]["system"] == saved["project"]["system"]
    assert result["project"]["incident_paths"][1] == saved["project"]["incident_paths"][1]
    def width(data):
        return next(b["radii_mm"]["tangential"] for b in data["analysis"]["sections"]["planes"][0]["beams"] if b["uid"] == "west")
    assert width(result) != pytest.approx(width(saved))
    assert client.patch(f"/api/inventory/items/{selected['id']}", json={"focal_length_mm": 63}).status_code == 200
    expired = client.post(root + "/replace-component", json={**req, "preview_token": result["preview_token"], "change_note": "采用预览"})
    assert expired.status_code == 409 and "预览" in expired.text
    assert read_project(db, case.id)["revision_id"] == saved["revision_id"]
    approved = client.post(root + "/replace-component/preview", json=req).json()
    installed = client.post(root + "/replace-component", json={**req, "preview_token": approved["preview_token"], "change_note": "安装"})
    assert installed.status_code == 200, installed.text
    actual = installed.json()
    assert actual["analysis"] == approved["analysis"]
    assert actual["replacement"] == approved["replacement"]
    assert actual["replacement"]["target_path_uid"] == "west"
    assert actual["project"]["system"] == saved["project"]["system"]
    assert actual["project"]["incident_paths"][1] == saved["project"]["incident_paths"][1]
    new_arm = actual["project"]["incident_paths"][0]
    assert new_arm["elements"][0]["focal_mm"] == 63 and new_arm["elements"][0]["aoi_deg"] == 2
    assert new_arm["parts"][0]["inventory_id"] == selected["id"] and new_arm["parts"][0]["locked_parameters"]
    changed = deepcopy(actual["project"])
    changed["incident_paths"][0]["elements"][0]["focal_mm"] = 125
    forged = client.post(root, json={"project": changed, "base_revision_id": actual["revision_id"], "change_note": "修订",
                                     "unlock_quote": "用户在工程编辑器明确解锁"})
    assert forged.status_code == 422 and "库存快照" in forged.text


def test_scoped_private_crystal_and_agent_working_snapshot(client, db):
    stock = client.post("/api/inventory/items", json={"category": "gain_crystal", "name": "双段样品", "quantity": 1,
                        "crystal_spec": {"segments": [{"material": "YAG", "role": "undoped", "length_mm": 2},
                                                       {"material": "Nd:YAG", "role": "gain", "length_mm": 4}]}}).json()
    case, saved = bench(db, crystal=True)
    working = begin_study(db, case.id, None, source_id=saved["revision_id"])["model_basis"]
    inputs = {"base_revision_id": working["revision_id"], "target_path_uid": "west", "target_element_uid": "same/id",
              "inventory_id": stock["id"], "orientation": "reversed"}
    goal = "只在独立试算中换用这块样品"
    task = AgentTask(case_id=case.id, mode="workbench", status="running", goal=goal)
    db.add(task)
    db.commit()
    def tool(name, args):
        return asyncio.run(execute(name, args, db=db, case=case, task=task, provider=None, session_id=None,
                                   attachment_ids=[], working=working))
    plan = tool("preview_component_replacement", PreviewReplacement(**inputs))
    assert plan["optical_results"]["observation_planes"][0]["beams"]
    installed = tool("replace_component", ReplaceComponentTool(**inputs, change_note=goal, source_quote=goal,
                                                               preview_token=plan["preview_token"]))
    assert installed["model_kind"] == "draft" and read_project(db, case.id)["revision_id"] == saved["revision_id"]
    project = installed["project"]
    assert project["system"] == saved["project"]["system"]
    assert project["incident_paths"][1] == saved["project"]["incident_paths"][1]
    chosen = project["incident_paths"][0]
    assert [el["length_mm"] for el in chosen["elements"]] == [4, 2]
    assert all(el["n"] > 1 for el in chosen["elements"])
    assert {p["inventory_id"] for p in chosen["parts"]} == {stock["id"]}
    assert project["assemblies"][0]["path_uid"] == "west"
    context = project["component_context"]
    assert "crystal_spec" not in context and context["path_contexts"]["west"]["wavelength_nm"] == 917
    assert installed["replacement"]["new_path_length_mm"] == 6
    assert "东侧光源按束腰标定" in project["assumptions"] and project["notes"].startswith("台架记录")
    assert not any("冷腔本征模" in w for w in installed["scope_warnings"])
    assert "current_crystal" not in case.parameters
    task.status = "completed"
    db.commit()
    retry = client.post(f"/api/cases/{case.id}/optical-project/replace-component",
                        json={**inputs, "preview_token": plan["preview_token"], "change_note": goal})
    assert retry.status_code == 200, retry.text
    assert retry.json()["revision_id"] == installed["revision_id"]
    assert read_project(db, case.id)["revision_id"] == saved["revision_id"]

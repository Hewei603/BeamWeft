"""Removing mounted stock must reconcile the revision, its physics and current experiment facts."""
from copy import deepcopy

import pytest

from app.models import ExperimentCase, GeneratedContent
from app.workbench.projects import read_project


def create_case(db, title):
    case = ExperimentCase(title=title, goal="核对安装中的实物", cavity_type="custom",
                          parameters={"design_facts": {"pump_power_w": {"value": 3}}})
    db.add(case)
    db.commit()
    return case, f"/api/cases/{case.id}/optical-project"


def save(client, root, project, revision=None, unlock=False):
    return client.post(root, json={"project": project, "base_revision_id": revision, "change_note": "核对安装位置",
                                   "unlock_quote": "用户在工程编辑器明确解锁" if unlock else ""})


def sample(uid):
    return {"uid": uid, "kind": "space", "name": uid, "length_mm": 3, "n": 1.81}


def unmount(project, members):
    changed = deepcopy(project)
    train = changed["system"]["elements"]
    changed["assemblies"] = [g for g in changed["assemblies"] if not members.intersection(g["element_uids"])]
    changed["parts"] = [p for p in changed["parts"] if train[p["element_index"]]["uid"] not in members]
    for i, element in enumerate(train):
        if element["uid"] in members:
            train[i] = {**sample("air-"+str(i)), "length_mm": element["length_mm"], "n": 1}
    return changed


@pytest.mark.parametrize("remove_current", [True, False])
def test_whole_mounts_from_one_stock_item_have_independent_frozen_facts(client, db, remove_current):
    case, root = create_case(db, "同一库存的两个不同实物")
    seed = {"title": "两个样品", "assumptions":["偏振待测"], "system": {"mode": "single_pass", "wavelength_nm": 1064, "input_radius_mm": .3,
            "elements": [sample("first"), sample("gap"), sample("second")]}, "assemblies": [],
            "parts": [{"element_index": i, "role": "crystal", "source": "样品架"} for i in (0, 2)]}
    saved = save(client, root, seed).json()
    spec = {"segments": [{"material": "YAG", "role": "undoped", "length_mm": 1},
                         {"material": "Nd:YAG", "role": "gain", "length_mm": 2}]}
    stock = client.post("/api/inventory/items", json={"name": "实验室库存", "category": "gain_crystal",
                        "quantity": 2, "raw_spec": "第一份读数", "crystal_spec": spec}).json()
    def install(previous, uid):
        response = client.post(root+"/replace-component", json={"base_revision_id": previous["revision_id"],
                    "target_element_uid": uid, "inventory_id": stock["id"], "change_note": "装入本位置"})
        assert response.status_code == 200, response.text
        return response.json()
    first = install(saved, "first")
    revised = deepcopy(spec)
    revised["segments"][1]["length_mm"] = 4
    stock_url = f"/api/inventory/items/{stock['id']}"
    assert client.patch(stock_url, json={"crystal_spec": revised, "raw_spec": "第二份读数"}).status_code == 200
    both = install(first, "second")
    project = both["project"]
    assert project["assumptions"] == seed["assumptions"] and project["notes"].count("采用库存 #") == 2
    ids = [set(g["element_uids"]) for g in project["component_context"]["crystal_groups"]]
    removed, kept = (ids[1], ids[0]) if remove_current else (ids[0], ids[1])

    partial = unmount(project, {next(iter(removed))})
    rejected = save(client, root, partial, both["revision_id"], unlock=True)
    assert rejected.status_code == 422 and "一部分分段" in rejected.text
    changed = unmount(project, removed)
    changed.pop("assemblies")  # A pre-assembly editor can remove a whole identified object.
    assert save(client, root, changed, both["revision_id"]).status_code == 422
    response = save(client, root, changed, both["revision_id"], unlock=True)
    assert response.status_code == 200, response.text
    one = response.json()
    context = one["project"]["component_context"]
    assert {frozenset(g["element_uids"]) for g in context["crystal_groups"]} == {frozenset(kept)}
    assert set(context["crystal_element_uids"]) == kept
    assert len(one["project"]["assemblies"]) == 1
    expected = (3, "第一份读数") if remove_current else (5, "第二份读数")
    db.refresh(case)
    facts = case.parameters["design_facts"]
    assert facts["crystal_length_mm"]["value"] == expected[0]
    assert facts["crystal_part"]["quote"] == expected[1]
    assert not any("多块分离晶体" in w for w in one["analysis"].get("scope_warnings", []))

    last = save(client, root, unmount(one["project"], kept), one["revision_id"], unlock=True)
    assert last.status_code == 200, last.text
    clean = last.json()["project"]["component_context"]
    assert not {"crystal_spec","crystal_sections","crystal_groups","crystal_inventory_id","crystal_name","last_replacement"} & clean.keys()
    db.refresh(case)
    assert "current_crystal" not in case.parameters and "crystal" not in case.parameters["component_selection"]
    assert "crystal_length_mm" not in case.parameters["design_facts"] and case.parameters["design_facts"]["pump_power_w"]["value"] == 3
    assert read_project(db, case.id, both["revision_id"])["project"] == project
    assert client.get(stock_url).json()["crystal_spec"]["segments"][1]["length_mm"] == 4


def test_ownership_can_survive_focus_edits_and_be_detached_independently_of_parameter_locks(client, db):
    _, root = create_case(db, "已绑定但焦距待核实")
    item = client.post("/api/inventory/items", json={"name": "台架透镜", "category": "lens", "quantity": 2}).json()
    lens = {"name": "同名透镜", "kind": "lens", "focal_mm": 35}
    project = {"title": "两片透镜", "system": {"mode": "single_pass", "wavelength_nm": 633, "input_radius_mm": .2,
               "elements": [{**lens, "uid": uid} for uid in ("a","b")]}, "parts": [
               {"element_index": i, "ownership": "owned", "inventory_id": item["id"], "source": "台架"} for i in (0, 1)]}
    first = save(client, root, project).json()
    project["system"]["elements"][0]["focal_mm"] = 36
    changed = save(client, root, project, first["revision_id"])
    assert changed.status_code == 200, changed.text
    revision = changed.json()["revision_id"]
    project["parts"][0]["inventory_id"] = None
    unbound = save(client, root, project, revision)
    assert unbound.status_code == 200, unbound.text
    project["parts"].pop(0)
    project["parts"][0]["element_index"] = 0
    project["system"]["elements"].pop(0)
    detached = save(client, root, project, unbound.json()["revision_id"])
    assert detached.status_code == 200 and detached.json()["project"]["parts"][0]["inventory_id"] == item["id"]
    assert client.get(f"/api/inventory/items/{item['id']}").json()["quantity"] == 2


def test_old_installation_assumptions_require_a_matching_historical_transition(client, db):
    case, root = create_case(db, "两条同名支路，装入同一批透镜")
    user_line = "旋转夹具后复测"
    seed = {"title": "两路探测", "assumptions":[user_line], "notes":"实验记录",
            "system":{"mode":"single_pass","wavelength_nm":780,"input_radius_mm":.3,"elements":[sample("host")]},
            "incident_paths":[{"uid":uid, "name":"同名支路", "target_uid":"host", "wavelength_nm":780,
                               "elements":[{"uid":"spot", "name":"lens", "kind":"lens", "focal_mm":70}],
                               "parts":[{"element_index":0,"role":"lens","source":"台架"}]} for uid in ("west","east")]}
    current = save(client, root, seed).json()
    item = client.post("/api/inventory/items",json={"name":"同批镜头","category":"lens","quantity":2,"focal_length_mm":52}).json()
    prior = [user_line]
    notes = []
    for uid in ("west","east"):
        installed = client.post(root+"/replace-component",json={"base_revision_id":current["revision_id"],
            "target_path_uid":uid, "target_element_uid":"spot", "inventory_id":item["id"],"change_note":"按标签装入"})
        assert installed.status_code == 200, installed.text
        current = installed.json()
        doc = current["project"]
        notes.append(doc["notes"].splitlines()[-1])
        # Materialize the stock action exactly as the previous release stored it.
        # This data setup simulates old revisions and never changes product history.
        row = db.get(GeneratedContent,current["revision_id"])
        old = deepcopy(row.content)
        prior = list(dict.fromkeys([*prior,notes[-1],*doc["component_context"]["path_contexts"][uid]["scope_warnings"]]))
        old["project"]["assumptions"] = prior
        row.content = old
        db.commit()
    assert len(set(notes)) == 2
    changed = deepcopy(old["project"])
    unverifiable = "「另一个台架」采用库存 #33「镜头」。与被换实物有关的旧假设请对照版本 #9876 复核。"
    changed["assumptions"] += [unverifiable, "用户记录：库存 #33 需核实"]
    saved = save(client,root,changed,current["revision_id"])
    assert saved.status_code == 200, saved.text
    revision = saved.json()
    assert revision["project"]["assumptions"] == [user_line, unverifiable, "用户记录：库存 #33 需核实"]
    assert revision["project"]["component_context"]["unverified_installation_notes"] == [unverifiable]
    assert any("原文已保留" in line for line in revision["analysis"]["scope_warnings"])
    assert revision["project"]["notes"] == changed["notes"] and all(text in changed["notes"] for text in notes)
    assert read_project(db,case.id,current["revision_id"])["project"]["assumptions"] == prior


def test_unmounting_a_mirror_withdraws_its_current_curvature_from_legacy_tools(client, db):
    case, root = create_case(db, "只撤下输出镜")
    case.parameters = {**case.parameters, "R1_mm":900}  # An independent input with no mounted M1 role.
    db.commit()
    item = client.post("/api/inventory/items",json={"category":"mirror","name":"OC","quantity":1,"roc_mm":125}).json()
    project = {"title":"检测台架","system":{"mode":"single_pass","wavelength_nm":780,"input_radius_mm":.3,
               "elements":[sample("spacer"),{"uid":"oc","name":"OC","kind":"mirror","radius_mm":125}]},
               "parts":[{"role":"laser_output","element_index":1,"source":"在位","inventory_id":item["id"]}]}
    saved = save(client, root, project).json()
    db.refresh(case)
    assert case.parameters["R2_mm"] == 125
    project["parts"].clear()
    project["system"]["elements"].pop()
    response = save(client, root, project, saved["revision_id"])
    assert response.status_code == 200, response.text
    db.refresh(case)
    assert "R2_mm" not in case.parameters and "output_roc_mm" not in case.parameters["design_facts"]
    assert case.parameters["R1_mm"] == 900

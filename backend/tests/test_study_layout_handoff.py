import asyncio
from copy import deepcopy

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from app.auth.security import Principal
from app.case_package import write_package
from app.models import AgentTask, ExperimentCase, GeneratedContent, InventoryItem
from app.open_case import open_package
from app.study_api import Study
from app.workbench.observation import OpticalLayout
from app.workbench.project_outputs import LAYOUT
from app.workbench.projects import SaveProject, read_project, save_project
from app.workbench.studies import run_study
from app.workbench.study_layouts import begin_study_input
from app.workbench.study_search import StudySearch, run_search
from app.workbench.tools import REGISTRY, STUDIES, execute


def layout():
    return {"system": {"mode": "single_pass", "wavelength_nm": 800, "input_radius_mm": .35, "elements": [
        {"uid": "lens", "kind": "lens", "name": "Lens", "focal_mm": 100},
        {"uid": "rail", "kind": "space", "name": "Rail", "length_mm": 30}]}}


def test_object_choices_read_the_exact_frozen_port_without_returning_prescriptions(client, db):
    cid = client.post("/api/cases", json={"title": "Frozen object identities"}).json()["id"]
    states = layout()
    states["observation_planes"] = [{"uid": key, "name": "Camera", "target_uid":"rail", "depth_mm": depth}
                                    for key, depth in (("entrance", 0), ("sensor", 15))]
    rid = save_project(db, cid, SaveProject(project={"title":"Two stations", **states}, change_note="Freeze"))["revision_id"]
    url = f"/api/cases/{cid}/studies"

    def objects(reference):
        response = client.post(url + "/layout-objects", json=reference)
        assert response.status_code == 200, response.text
        return response.json()

    ports = client.get(url + "/project-outputs", params={"revision_id":rid}).json()["outputs"]
    frozen = objects({"revision_id":rid, "port":"v1/layout"})
    assert frozen["observation_planes"] == [{"uid":"entrance","name":"Camera"},{"uid":"sensor","name":"Camera"}]
    prefix = next(key for key, item in ports.items() if key.startswith("layout-prefix-v1/") and item.get("plane_uid") == "sensor")
    assert objects({"revision_id":rid, "port":prefix})["observation_planes"] == [{"uid":"sensor","name":"Camera"}]
    key = next(key for key, item in ports.items() if item["port"]["quantity"] != LAYOUT.quantity)
    assert client.post(url + "/layout-objects", json={"revision_id":rid,"port":key}).status_code == 422

    states["network"] = {"components":[{"uid":"split","name":"Splitter","model":{"kind":"coupler","through_fraction":.52}},
                                       {"uid":"matrix","name":"Custom","model":{"kind":"matrix","ports":["in","out"]}}]}
    run = GeneratedContent(case_id=cid, content_type="scientific_study", content={
        "available_outputs":{"proposed":LAYOUT.model_dump(mode="json")}, "output":{"proposed":states}})
    db.add(run)
    db.commit()
    ref = {"run_id":run.id, "port":"proposed"}
    assert objects(ref) == {**frozen, "network":{"components":[
        {"uid":"split","name":"Splitter","model":{"ports":["1","2","3","4"]}},
        {"uid":"matrix","name":"Custom","model":{"ports":["in","out"]}}]}}
    other = client.post("/api/cases", json={"title":"Different case"}).json()["id"]
    assert client.post(f"/api/cases/{other}/studies/layout-objects", json=ref).status_code == 404


def propagate(db, cid, inputs, connections=None):
    study, provider = STUDIES["propagate_coherent_field"]
    return run_study(db.get_bind(), cid, study, provider, {"samples": 128, "window_mm": 3, **inputs},
                     connections=connections)["study_record"]["id"]


def candidate(db, cid, rid, field, element, value):
    search = StudySearch(run_id=rid, registration="Use the same camera frame and reference",
                         axes=[{"paths": [["layout", "system", "elements", element, field]], "samples": [[value]]}],
                         measures=[{"output": "z_mm", "direction": "max"}])
    row, = run_search(db.get_bind(), cid, search, STUDIES)["rows"]
    assert row["state"] == "feasible", row
    return row["run_id"]


def agent(db, cid, name, **values):
    task = AgentTask(case_id=cid, mode="workbench", status="running", goal="继续候选计算使用的光路")
    db.add(task)
    db.commit()
    args = REGISTRY[name][0].model_validate(values)
    return asyncio.run(execute(name, args, db=db, case=db.get(ExperimentCase, cid), task=task, working=None,
                               provider=None, session_id=None, attachment_ids=[]))


def test_frozen_candidate_becomes_an_independent_recalculable_project_via_api_and_agent(db, client):
    cid = client.post("/api/cases", json={"title": "Optical bench"}).json()["id"]
    base = save_project(db, cid, SaveProject(project={"title": "Physical setup", **layout()}, change_note="Fixed bench"))
    values = layout()
    del values["system"]["elements"][0]["uid"]  # Old and hand-written layouts often lack assigned IDs.
    original = propagate(db, cid, {"layout": values})
    run = candidate(db, cid, original, "length_mm", 1, 42)
    url = f"/api/cases/{cid}/studies/runs/{run}"
    source = deepcopy(db.get(GeneratedContent, run).content)
    metadata = client.get(url).json()["layout_inputs"]
    assert metadata == agent(db, cid, "read_artifact", content_id=run, path=["layout_inputs"])["content"]
    assert metadata == [{"input_key": "layout", "label": LAYOUT.label, "kind": "frozen"}]
    opened = client.post(url + "/project", json={"input_key": "layout"}).json()
    model = opened["model_basis"]
    assert model["kind"] == "draft" and model["source_revision_id"] is None and model["base_revision_id"] is None
    rid = model["revision_id"]
    project = read_project(db, cid, rid)
    assert project["study_input"]["run_id"] == run and project["project"]["system"]["elements"][1]["length_mm"] == 42
    assert project["project"]["parts"] == []
    assert agent(db, cid, "begin_optical_study", study_input={"run_id": run, "input_key": "layout"})["model_basis"] == model
    assert client.post(url+"/project", json={"input_key": "window_mm"}).status_code == 422
    other = client.post("/api/cases", json={"title": "Other case"}).json()["id"]
    assert client.post(f"/api/cases/{other}/studies/runs/{run}/project", json={"input_key": "layout"}).status_code == 422
    again = propagate(db, cid, {}, {"layout": {"revision_id": rid, "port": "v1/layout", "registration": "Same optical train"}})
    assert db.get(GeneratedContent, again).content["output"]["z_mm"] == source["output"]["z_mm"]
    changed = deepcopy(project["project"])
    changed["system"]["elements"][1]["length_mm"] = 50
    edit = client.post(f"/api/cases/{cid}/optical-project/variants", json={"project": changed, "source_revision_id": rid})
    assert edit.is_success and edit.json()["source_revision_id"] == rid
    assert db.get(GeneratedContent, run).content == source
    assert read_project(db, cid, rid)["project"] == project["project"]
    assert read_project(db, cid)["revision_id"] == base["revision_id"]


def test_bound_prefix_opens_the_complete_historical_project_preserving_locks_and_stock(db, client):
    cid = client.post("/api/cases", json={"title": "Camera bench"}).json()["id"]
    stock = client.post("/api/inventory/items", json={"category": "other", "name": "Measured relay lens"}).json()
    part = {"element_index": 0, "inventory_id": stock["id"], "source": "Measured relay lens", "locked_parameters": ["focal_mm"]}
    document = {"title": "Lab bench", **layout(), "parts": [part], "notes": "Keep the downstream arm",
                "observation_planes": [{"uid": "det", "name": "CCD", "target_uid": "rail", "depth_mm": 10}]}
    base = save_project(db, cid, SaveProject(project=document, change_note="Mounts and CCD"))
    original = propagate(db, cid, {}, {"layout": {"revision_id": base["revision_id"],
        "port": "layout-prefix-v1/det/primary/layout", "registration": "Single pass to the CCD"}})
    run = candidate(db, cid, original, "length_mm", 1, 15)
    saved = db.get(GeneratedContent, run).content
    url = f"/api/cases/{cid}/studies/runs/{run}"
    ref = saved["connections"]["layout"]
    newer = save_project(db, cid, SaveProject(project={**document, "notes": "Moved the experiment"}, change_note="Later",
                                              base_revision_id=base["revision_id"]))
    info, = client.get(url).json()["layout_inputs"]
    assert info["kind"] == "project" and info["revision_id"] == ref["revision_id"] and info["scope"] == "layout_prefix"
    opened = client.post(url+"/project", json={"input_key": "layout"}).json()
    assert opened["model_basis"]["revision_id"] == ref["revision_id"]
    assert opened["project"]["parts"] == base["project"]["parts"]
    assert opened["project"]["notes"] == document["notes"] and opened["project"]["observation_planes"][0]["depth_mm"] == 15
    assert opened["project"]["system"]["elements"][1]["length_mm"] == 30
    fork = agent(db, cid, "begin_optical_study", study_input={"run_id": run, "input_key": "layout"}, variant_name="CCD alternative")
    assert fork["model_basis"]["source_revision_id"] == ref["revision_id"]
    assert fork["model_basis"]["base_revision_id"] == base["revision_id"] and fork["project"] == opened["project"]
    official = client.post(f"/api/cases/{cid}/studies/runs/{original}/project", json={"input_key": "layout"}).json()
    assert official["model_basis"]["kind"] == "draft" and official["model_basis"]["base_revision_id"] == base["revision_id"]
    assert official["project"] == base["project"] and read_project(db, cid)["revision_id"] == newer["revision_id"]
    # A damaged import can point at another valid revision in the same case. It must not open a different train.
    broken = deepcopy(saved)
    broken["connections"]["layout"]["revision_id"] = base["revision_id"]
    db.get(GeneratedContent, run).content = broken
    db.commit()
    rejected = client.post(url+"/project", json={"input_key": "layout"})
    assert rejected.status_code == 422 and "不一致" in rejected.json()["detail"]


@pytest.mark.parametrize("refs,expected", [(None, "legacy"), ({"layout": {"run_id": 997, "port": "emitted"}}, "derived"),
                                       ({"layout": {"revision_id": 997, "port": "v1/layout"}}, "missing"),
                                       ({"layout": {"revision_id": 997}}, "malformed")])
def test_partial_history_never_substitutes_the_current_project(db, client, refs, expected):
    cid = client.post("/api/cases", json={"title": "Restored records"}).json()["id"]
    record = {"definition": {"input_ports": {"layout": LAYOUT.model_dump()}}, "inputs": {"layout": layout()}}
    if refs is not None:
        record["connections"] = refs
    row = GeneratedContent(case_id=cid, content_type="scientific_study", content=record)
    db.add(row)
    db.commit()
    url = f"/api/cases/{cid}/studies/runs/{row.id}/project"
    response = client.post(url, json={"input_key": "layout"})
    if expected in {"missing", "malformed"}:
        assert response.status_code == 422 and db.query(GeneratedContent).filter_by(content_type="optical_snapshot").count() == 0
    else:
        assert response.is_success, response.text
        source = response.json()["study_input"]
        assert source["history_unknown"] if expected == "legacy" else source["upstream_run_id"] == 997
        assert response.json()["model_basis"]["base_revision_id"] is None


def test_native_file_preserves_bound_and_derived_lineage_over_two_workspaces(db, client, tmp_path):
    other = client.post("/api/cases", json={"title": "Unrelated equipment"}).json()["id"]
    save_project(db, other, SaveProject(project={"title": "Other bench", **layout()}, change_note="Other"))
    cid = client.post("/api/cases", json={"title": "Transfer bench"}).json()["id"]
    stock = client.post("/api/inventory/items", json={"name": "Tracked lens", "category": "other"}).json()
    part = {"element_index": 0, "inventory_id": stock["id"], "source": "Original specification", "locked_parameters": ["focal_mm"]}
    source = save_project(db, cid, SaveProject(project={"title": "Optics", **layout(), "parts": [part]}, change_note="Setup"))
    rid = source["revision_id"]
    direct = propagate(db, cid, {}, {"layout": {"revision_id": rid, "port": "v1/layout", "registration": "Same rail"}})
    bound_id = begin_study_input(db, cid, direct, "layout")["model_basis"]["revision_id"]
    producer = Study("record_optics", "Optical input", "1", "transfer fixture", OpticalLayout,
                     lambda p: {"geometry": p.model_dump(mode="json"), "status": "completed"}, output_ports={"geometry": LAYOUT})
    parent = run_study(db.get_bind(), cid, producer, "test lab", layout())["study_record"]["id"]
    derived = propagate(db, cid, {}, {"layout": {"run_id": parent, "port": "geometry", "registration": "Same frame"}})
    copy_id = begin_study_input(db, cid, derived, "layout")["model_basis"]["revision_id"]
    assert len({rid, direct, bound_id, parent, derived, copy_id}) == 6
    archive = tmp_path / "source.lclaw"
    payload = client.get(f"/api/cases/{cid}/project-file", params={"revision_id": copy_id})
    assert payload.is_success
    archive.write_bytes(payload.content)
    for reception in ("receiver", "forwarded"):
        folder, meta = open_package(archive, tmp_path / reception)
        assert meta["opening_revision_id"] == copy_id
        engine = create_engine(URL.create("sqlite", database=str(folder / "laserclaw.db")))
        try:
            with Session(engine) as peer:
                assert peer.get(ExperimentCase, other) is None
                first = read_project(peer, cid, bound_id)
                assert first["study_input"]["run_id"] == direct and first["study_input"]["revision_id"] == rid
                assert first["project"]["parts"][0]["inventory_id"] == stock["id"]
                assert peer.get(InventoryItem, stock["id"]).name == "Tracked lens"
                recovered = read_project(peer, cid, copy_id)
                origin = recovered["study_input"]
                assert origin["run_id"] == derived and origin["upstream_run_id"] == parent
                assert peer.get(GeneratedContent, parent).content["output"]["geometry"] == peer.get(GeneratedContent, derived).content["inputs"]["layout"]
                assert begin_study_input(peer, cid, derived, "layout")["model_basis"]["revision_id"] == copy_id
                assert begin_study_input(peer, cid, direct, "layout")["model_basis"]["revision_id"] == bound_id
                if reception == "receiver":
                    archive = tmp_path / "forwarded.lclaw"
                    write_package(peer, peer.get(ExperimentCase, cid), archive, Principal("local", "user"), copy_id)
        finally:
            engine.dispose()


def test_unmaterialized_inline_layout_transfers_its_own_component_bindings(db, client, tmp_path):
    cid = client.post("/api/cases", json={"title": "Scientific inputs only"}).json()["id"]
    stock = client.post("/api/inventory/items", json={"name": "Green reference arm", "category": "other"}).json()
    part = {"element_index": 0, "inventory_id": stock["id"], "source": "Survey", "locked_parameters": ["length_mm"]}
    track = {"uid": "secondary", "name": "Separate arm", "target_uid": "rail", "target_n": 1, "wavelength_nm": 532,
             "tangential": {"radius_mm": .3, "wavefront": "waist", "m2": 1},
             "elements": [{"uid": "arm-rail", "name": "Spacer", "kind": "space", "length_mm": 4}], "parts": [part]}
    run = propagate(db, cid, {"layout": {**layout(), "incident_paths": [track]}})
    archive = tmp_path / "inputs-only.lclaw"
    archive.write_bytes(client.get(f"/api/cases/{cid}/project-file").content)
    folder, _ = open_package(archive, tmp_path / "recipient")
    engine = create_engine(URL.create("sqlite", database=str(folder/"laserclaw.db")))
    try:
        with Session(engine) as peer:
            assert read_project(peer, cid) is None
            restored = begin_study_input(peer, cid, run, "layout")
            bound, = restored["project"]["incident_paths"][0]["parts"]
            assert bound["locked_parameters"] == ["length_mm"]
            assert peer.get(InventoryItem, bound["inventory_id"]).name == stock["name"]
            assert read_project(peer, cid) is None
    finally:
        engine.dispose()

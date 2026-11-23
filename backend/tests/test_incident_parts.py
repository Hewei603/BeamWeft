import io
import json
import zipfile
from copy import deepcopy

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from app.case_package import HISTORY
from app.models import InventoryItem
from app.open_case import open_package
from app.workbench.projects import OpticalProject, ReplaceComponent, SaveProject, evaluate_project, read_project, replace_component
from app.workbench.study_inputs import begin_study, save_working_project


def test_branch_bindings_survive_insertion_save_and_portable_project(client, db, tmp_path):
    stock = InventoryItem(name="准直镜", category="lens", quantity=1, focal_length_mm=35)
    db.add(stock)
    db.commit()
    cid = client.post("/api/cases", json={"title": "支路实物"}).json()["id"]
    base = f"/api/cases/{cid}"
    project = {"title": "两条光路", "system": {"mode": "single_pass", "wavelength_nm": 1064, "elements": [
        {"uid": "unbound", "name": "主路透镜", "kind": "lens", "focal_mm": 70},
        {"uid": "target", "name": "共同介质", "kind": "space", "length_mm": 5, "n": 1.8}]},
        "incident_paths": [{"uid": "incident", "name": "入射支路", "target_uid": "target", "target_n": 1.9,
            "wavelength_nm": 808, "elements": [{"uid": "lens", "name": "准直镜", "kind": "lens", "focal_mm": 35}],
            "parts": [{"role": "collimator", "element_index": 0, "inventory_id": stock.id, "ownership": "owned",
                       "locked_parameters": ["kind", "focal_mm"], "source": "镜筒标签"}]}]}

    def save(model, previous=None):
        return client.post(base + "/optical-project", json={"project": model, "base_revision_id": previous, "change_note": "编辑支路元件"})

    first = save(project)
    assert first.status_code == 200, first.text
    previous = first.json()["revision_id"]
    with pytest.raises(ValueError, match="可用数量不足"):
        replace_component(db, cid, ReplaceComponent(inventory_id=stock.id, base_revision_id=previous,
                                                    target_element_uid="unbound", change_note="选用同一库存"))
    branch = project["incident_paths"][0]
    branch["elements"].insert(0, {"name": "透镜前空气", "kind": "space", "length_mm": 5})
    branch["parts"][0]["element_index"] = 1
    second = save(project, previous)
    assert second.status_code == 200, second.text
    revision = second.json()["revision_id"]
    comparison = client.post(base + "/optical-project/input-comparison",
                             json={"reference_revision_id": previous, "revision_id": revision}).json()
    assert comparison["total"] == 1 and comparison["items"][0]["path"][-2] == "elements"
    for change in ("remove_branch", "change_focal", "drop_lock"):
        edit = deepcopy(second.json()["project"])
        arm = edit["incident_paths"][0]
        if change == "remove_branch":
            edit["incident_paths"] = []
        elif change == "change_focal":
            arm["elements"][1]["focal_mm"] = 80
        else:
            arm["parts"][0]["locked_parameters"] = []
        rejected = save(edit, revision)
        assert rejected.status_code == 422 and "locked" in rejected.text.lower()
    study = begin_study(db, cid, None, source_id=revision)["model_basis"]
    unlocked = deepcopy(second.json()["project"])
    unlocked["incident_paths"][0]["parts"][0]["locked_parameters"] = []
    trial = SaveProject(project=unlocked, change_note="调整光路", base_revision_id=study["revision_id"])
    with pytest.raises(ValueError, match="[Ll]ocked"):
        save_working_project(db, cid, trial, study)
    alternative = save_working_project(db, cid, trial, study, allow_unlock=True)
    assert alternative["project"]["incident_paths"][0]["parts"][0]["locked_parameters"] == []
    assert read_project(db, cid)["revision_id"] == revision
    bundle = client.get(base + "/project-file").content
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        inventory = json.loads(archive.read(HISTORY))["inventory_specs"]
        assert [(item["id"], item["focal_length_mm"]) for item in inventory] == [(stock.id, 35)]
    package = tmp_path / "beamline.lclaw"
    package.write_bytes(bundle)
    folder, _ = open_package(package, tmp_path / "opened")
    engine = create_engine(URL.create("sqlite", database=str(folder / "laserclaw.db")))
    try:
        with Session(engine) as peer:
            restored = read_project(peer, cid, revision)
            assert restored["project"] == second.json()["project"]
            assert peer.get(InventoryItem, stock.id).quantity == 0
            replayed = evaluate_project(peer, OpticalProject.model_validate(restored["project"]))
            assert replayed["analysis"] == restored["analysis"]
    finally:
        engine.dispose()


def test_incident_parts_validate_against_their_own_elements():
    base = {"title": "path", "system": {"mode": "single_pass", "elements": [{"kind": "space", "uid": "root", "name": "air"}]},
            "incident_paths": [{"uid": "arm", "name": "arm", "target_uid": "root",
                                "elements": [{"uid": "a", "name": "a", "kind": "space"}]}]}
    assert "parts" not in OpticalProject.model_validate(base).model_dump()["incident_paths"][0]
    base["incident_paths"][0]["parts"] = [{"source": "s", "element_index": 1}]
    with pytest.raises(ValidationError, match="Part index"):
        OpticalProject.model_validate(base)
    base["incident_paths"][0]["parts"] = [{"source": "s", "element_index": 0, "locked_parameters": ["other"]}]
    with pytest.raises(ValidationError, match="not an element property"):
        OpticalProject.model_validate(base)

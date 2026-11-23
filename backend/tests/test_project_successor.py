from copy import deepcopy

import pytest

from app.workbench import projects


def test_late_project_writer_loses_its_claim_without_changing_the_case(db, client, monkeypatch):
    case_id = client.post("/api/cases", json={"title": "Two independent editors"}).json()["id"]
    model = {"title":"Initial","system":{"mode":"single_pass","wavelength_nm":808,"input_radius_mm":.4,
             "elements":[{"uid":"gap","kind":"space","name":"gap","length_mm":20}]}}
    def save(project, parent):
        return projects.save_project(db, case_id, projects.SaveProject(project=project, base_revision_id=parent, change_note="edit"))
    first = save(model, None)
    mine = deepcopy(model)
    mine["system"]["elements"][0]["length_mm"] = 37
    good = save(mine, first["revision_id"])
    with monkeypatch.context() as late:
        # Model the earlier snapshot held by a second worker. The database,
        # independently of the Python lock, must reject the second successor.
        late.setattr(projects, "read_project", lambda db, case: first)
        with pytest.raises(projects.ProjectRevisionConflict):
            save(model, first["revision_id"])
    assert projects.read_project(db, case_id)["revision_id"] == good["revision_id"]
    assert len(projects.revisions(db, case_id)) == 2
    assert save(model, good["revision_id"])["parent_revision_id"] == good["revision_id"]

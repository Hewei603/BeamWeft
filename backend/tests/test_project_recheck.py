from copy import deepcopy

import pytest

from app.models import GeneratedContent
from app.workbench.model_comparison import compare_values, recheck_project


@pytest.mark.parametrize("mode", ["single_pass", "standing_wave"])
def test_recheck_uses_selected_frozen_snapshot_without_modifying_main_project(client, db, mode):
    root = f"/api/cases/{client.post('/api/cases', json={'title': 'Recheck'}).json()['id']}/optical-project"
    space = {"name": "Drift", "kind": "space", "length_mm": 35, "n": 1}
    elements = [space] if mode == "single_pass" else [
        {"name": "M1", "kind": "mirror"}, space, {"name": "M2", "kind": "mirror", "radius_mm": 200}]
    project = {"title": "Baseline", "system": {"mode": mode, "wavelength_nm": 633, "elements": elements,
                                               **({"input_radius_mm": .31} if mode == "single_pass" else {})}}
    response = client.post(root, json={"project": project, "change_note": "Baseline"})
    assert response.is_success, response.text
    first = response.json()
    variant_input = deepcopy(first["project"])
    next(el for el in variant_input["system"]["elements"] if el["kind"] == "space")["length_mm"] = 45
    variant = client.post(root+"/variants", json={"project": variant_input, "variant_name": "45 mm",
        "base_revision_id": first["revision_id"], "source_revision_id": first["revision_id"]}).json()
    revision = variant["revision_id"]
    row = db.get(GeneratedContent, revision)
    original = deepcopy(row.content)
    count = db.query(GeneratedContent).count()
    def recheck():
        response = client.post(f"{root}/{revision}/recheck")
        assert response.status_code == 200, response.text
        return response.json()
    replay = recheck()
    assert replay["total_differences"] == 0 and replay["compared_numbers"] > 10
    assert "input_interpretation" not in replay
    assert replay["saved_solver_receipt"] == replay["recomputed_solver_receipt"] == original["analysis"]["solver_receipt"]
    db.refresh(row)
    assert row.content == original
    # Simulate an older archive which contains a measured discrepancy but has no method receipt.
    legacy = deepcopy(original)
    del legacy["analysis"]["solver_receipt"]
    legacy["analysis"]["wavelength_nm"] = 636
    next(el for el in legacy["project"]["system"]["elements"] if el["kind"] == "space").pop("n")
    row.content = legacy
    db.commit()
    result = recheck()
    assert result["saved_solver_receipt"] is None and result["recomputed_solver_receipt"]
    change, = result["input_interpretation"]["differences"]
    assert change["path"][-1] == "n" and change["recomputed"] == 1 and "saved" not in change
    assert result["differences"] == [{"path": ["wavelength_nm"], "kind": "numeric", "saved": 636, "recomputed": 633}]
    db.refresh(row)
    assert row.content == legacy
    other_case = client.post("/api/cases", json={"title": "Other"}).json()["id"]
    assert client.post(f"/api/cases/{other_case}/optical-project/{revision}/recheck").status_code == 422
    assert client.post(f"{root}/0/recheck").status_code == 422
    assert db.query(GeneratedContent).count() == count
    assert client.get(root).json()["current"]["revision_id"] == first["revision_id"]


def test_output_comparison_counts_all_fields_but_bounds_examples():
    old = {"values": [False, 1], "caption": "old", "missing": [], "engine": "old", "system": {"legacy": 5}}
    new = {"values": [0, 1.0000000000000002, 5], "caption": "new", "added": None, "solver_receipt": {}}
    result = compare_values(old, new, limit=3, exclude={"engine", "solver_receipt", "system"})
    assert result["compared_numbers"] == 1
    assert result["difference_counts"] == {"structure": 4, "numeric": 1, "other": 1}
    assert len(result["differences"]) == 3 and result["total_differences"] == 6
    assert result["differences"][0] == {"path": ["added"], "kind": "structure", "recomputed": None}


def test_pre_identity_archive_exposes_new_defaults_and_unrecognised_fields():
    record = {"revision_id": 41, "project": {"title": "Pre-ID layout", "archived_extension": {"width": 1},
        "system": {"mode": "single_pass", "wavelength_nm": 633, "input_radius_mm": .3,
                   "elements": [{"kind": "space", "name": "Drift", "length_mm": 30}]}},
        "analysis": {"status": "completed"}}
    original = deepcopy(record)
    # Sparse older models can pick up many defaults. Keep the front page bounded,
    # and make sure that a historical identity is never invented on repeated rechecks.
    left, right = recheck_project(record), recheck_project(record)
    assert record == original and left == right
    parsed = left["input_interpretation"]
    assert parsed["total_differences"] > 0
    assert {"path": ["archived_extension"], "kind": "structure", "saved": {"fields": 1}} in parsed["differences"]

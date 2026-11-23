from copy import deepcopy
import math

import pytest

from app.auth.security import Principal
from app.physics.materials import n_real
from app.workbench.component_models import (
    CaptureModel, LibraryQuery, PlaceModel, capture_model, library, place_model,
)
from app.workbench.projects import OpticalProject, read_project

LOCAL = Principal(actor="anonymous", role="user")


def project(client, title, project, previous=None):
    if previous:
        case, revision = previous
    else:
        case = client.post("/api/cases", json={"title": title}).json()["id"]
        revision = None
    response = client.post(f"/api/cases/{case}/optical-project",
                           json={"base_revision_id": revision, "change_note": title, "project": project})
    assert response.is_success, response.text
    return case, response.json()["revision_id"]


def source():
    return {"title": "Reference bench", "system": {"mode": "single_pass", "wavelength_nm": 808,
            "input_radius_mm": .4, "elements": [
                {"uid": "L", "kind": "matrix", "name": "cyl pair", "matrix_t": [1, 0, -.0125, 1],
                 "matrix_s": [1, 0, -.01, 1]}, {"uid": "air", "kind": "space", "name": "spacer", "length_mm": 16}]},
            "single_pass_power": {"wavelength_nm": 808, "input_w": {"low": 1.7},
                                  "stages": [{"scope": "host", "element_uid": "L", "law": {"mode": "net",
                                      "retained": {"low": .92, "basis": "measured", "source": "808 nm characterization"}}}]}}


def recipe(rev, **extra):
    return CaptureModel(project_revision_id=rev, first_uid="L", last_uid="air", name="Relay", **extra)


@pytest.mark.parametrize("model,ref_supplied", [("nbk7", True), (None, True), (None, False)])
def test_reusable_medium_carries_spectral_evidence_across_projects(client, db, model, ref_supplied):
    n0 = n_real("nbk7", 1064) if model else 1.575
    plate = {"uid": "plate", "kind": "space", "length_mm": 9, "name": "specimen", "n": n0}
    if ref_supplied:
        plate["index_reference"] = {"model": model, "wavelength_nm": 1064}
    a, rev = project(client, "source", {"title": "model origin", "system": {"mode":"single_pass", "wavelength_nm":1064,
        "input_radius_mm":.055, "elements":[plate]}, "single_pass_power": {"wavelength_nm":1064,
        "stages":[{"scope":"host","element_uid":"plate","law":{"mode":"net","retained":{"low":.96,"basis":"measured",
        "source":"single-wavelength measurement"}}}]}})
    capture = CaptureModel(project_revision_id=rev,name="test piece",first_uid="plate",last_uid="plate",include_power=True)
    source_model = capture_model(db, LOCAL, a, capture)
    assert source_model["cross_wavelength_geometry"]
    distal = {"uid":"rail","name":"target rail","kind":"space","length_mm":18}
    b, base = project(client,"second wavelength",{"title":"405 nm bench","system":{
        "mode":"single_pass","wavelength_nm":405,"input_radius_mm":.055,"elements":[distal]}})
    args = PlaceModel(model_revision_id=source_model["model_revision_id"],project_revision_id=base,before_uid="rail")
    with pytest.raises(ValueError,match="传输参数"):
        place_model(db, LOCAL, b, args)
    assert read_project(db,b)["revision_id"] == base
    saved = place_model(db, LOCAL, b, args.model_copy(update={"geometry_only":True}))
    placed = saved["project"]["system"]["elements"][0]
    assert placed["uid"] != "plate" and placed["index_reference"] == {"model":model,"wavelength_nm":405}
    assert placed["component_origin"]["wavelength_nm"] == 1064
    assert not saved["project"]["parts"] and saved["project"].get("single_pass_power") is None
    if model:
        assert placed["n"] == pytest.approx(n_real(model,405), rel=1e-11)
        assert saved["analysis"]["planes"]["tangential"]["output"]["radius_mm"] is not None
    else:
        assert placed["n"] is None
        assert any(issue["element_uid"] == placed["uid"] for issue in saved["analysis"]["input_issues"])
    assert read_project(db,a,rev)["project"]["system"]["elements"][0]["n"] == n0


def test_assembly_membership_survives_repeated_cross_path_placement(db, client):
    model = source()
    model["assemblies"] = [{"uid":"barrel", "name":"机械整体", "element_uids":["L","air"]}]
    case, revision = project(client, "portable barrel", model)
    entry = capture_model(db, LOCAL, case, recipe(revision))
    model["incident_paths"] = [{"uid":"probe", "name":"measurement", "target_uid":"air", "wavelength_nm":808}]
    case, revision = project(client, "second path", model, (case, revision))
    request = PlaceModel(model_revision_id=entry["model_revision_id"], project_revision_id=revision, path_uid="probe")
    first = place_model(db, LOCAL, case, request)
    request.project_revision_id = first["revision_id"]
    second = place_model(db, LOCAL, case, request)
    groups = second["project"]["assemblies"]
    assert len({g["uid"] for g in groups}) == 3
    assert [g["path_uid"] for g in groups] == ["primary","probe","probe"]
    path = second["project"]["incident_paths"][0]["elements"]
    assert groups[1]["element_uids"] + groups[2]["element_uids"] == [e["uid"] for e in path]
    request.project_revision_id = second["revision_id"]
    request.before_uid = path[1]["uid"]
    with pytest.raises(ValueError, match="实物的外侧"):
        place_model(db, LOCAL, case, request)
    request.before_uid = path[2]["uid"]
    assert len(place_model(db, LOCAL, case, request, preview=True)["project"]["assemblies"]) == 4
    assert read_project(db, case)["revision_id"] == second["revision_id"]


@pytest.mark.parametrize("groups", [
    [{"uid":"tube", "name":"tube", "element_uids":["air","L"]}],
    [{"uid":"tube", "name":"tube", "element_uids":["L","missing"]}],
    [{"uid":"tube", "name":"tube", "path_uid":"absent", "element_uids":["L"]}],
    [{"uid":uid, "name":"tube", "element_uids":["air"]} for uid in ("a","b")],
    [{"uid":"same", "name":"tube", "element_uids":[uid]} for uid in ("L","air")],
])
def test_inconsistent_or_ambiguous_boundaries_are_rejected(groups):
    with pytest.raises(ValueError):
        OpticalProject.model_validate({**source(),"assemblies":groups})


def test_older_client_preserves_membership_on_edit_and_can_explicitly_dissolve(client, db):
    model = source()
    model["assemblies"] = [{"uid":"body","name":"relay","element_uids":["L","air"]}]
    case, rev = project(client,"first",model)
    original = read_project(db,case,rev)["project"]
    del model["assemblies"]
    model["notes"] = "operator corrected the calibration date"
    _, second = project(client,"text edit",model,(case,rev))
    assert read_project(db,case)["project"]["assemblies"] == original["assemblies"]
    model["assemblies"] = []
    project(client,"independent elements",model,(case,second))
    assert read_project(db,case)["project"]["assemblies"] == []
    assert read_project(db,case)["analysis"]["planes"] == read_project(db,case,rev)["analysis"]["planes"]
    assert read_project(db,case,rev)["project"] == original


def test_traceable_geometry_and_loss_across_a_pump_branch_and_frozen_revisions(db, client):
    original = source()
    a, rev = project(client, "source", original)
    v1 = capture_model(db, LOCAL, a, recipe(rev, include_power=True))
    assert capture_model(db, LOCAL, a, recipe(rev, include_power=True)) == v1
    host = {"title": "Second bench", "system": {"mode": "standing_wave", "wavelength_nm": 1064,
            "elements": [{"uid":"M1","name":"M1","kind":"mirror"}, {"uid":"cell","name":"cell","kind":"space","length_mm":20},
                         {"uid":"M2","name":"M2","kind":"mirror","radius_mm":180}]},
            "incident_paths": [{"uid":"arm","name":"pump","target_uid":"cell","target_n":1,"wavelength_nm":808,
                                "tangential":{"radius_mm":.4,"wavefront":"waist","m2":1}}],
            "observation_planes":[{"uid":"entry","name":"entry","target_uid":"cell","depth_mm":0}]}
    b, base = project(client, "recipient", host)
    placement = PlaceModel(model_revision_id=v1["model_revision_id"], project_revision_id=base, path_uid="arm")
    preview = place_model(db, LOCAL, b, placement, preview=True)
    assert read_project(db, b)["revision_id"] == base
    placed = place_model(db, LOCAL, b, placement)
    path = placed["project"]["incident_paths"][0]
    assert [el["name"] for el in path["elements"]] == ["cyl pair", "spacer"]
    assert path["power_budget"]["input_w"] is None
    stage = path["power_budget"]["stages"][0]
    assert stage["scope"] == "arm" and stage["element_uid"] == path["elements"][0]["uid"]
    assert stage["law"]["retained"]["low"] == .92
    assert "L" not in [el["uid"] for el in path["elements"]]
    assert path["elements"][0]["component_origin"]["instance_uid"] != preview["project"]["incident_paths"][0]["elements"][0]["component_origin"]["instance_uid"]
    assert placed["project"]["parts"] == [] and placed["project"]["system"] == read_project(db, b, base)["project"]["system"]
    for plane, f in [("tangential", 80), ("sagittal", 100)]:
        # Independent closed form for a thin lens at a waist followed by free propagation.
        w = .4
        expected = w * math.hypot(1 - 16 / f, 16 * .000808 / (math.pi * w*w))
        assert read_project(db, a, rev)["analysis"]["planes"][plane]["output"]["radius_mm"] == pytest.approx(expected)
    original["system"]["elements"][0]["matrix_t"][2] = -.02
    _, rev2 = project(client, "recalibration", original, (a, rev))
    v2 = capture_model(db, LOCAL, a, recipe(rev2, previous_model_revision_id=v1["model_revision_id"]))
    assert v2["version"] == 2 and v2["family"] == v1["family"]
    assert read_project(db, b)["project"]["incident_paths"][0]["elements"][0]["matrix_t"][2] == -.0125
    assert read_project(db, a, rev)["project"]["system"]["elements"][0]["matrix_t"][2] == -.0125
    with pytest.raises(ValueError, match="新版本"):
        capture_model(db, LOCAL, a, recipe(rev, note="competing update", previous_model_revision_id=v1["model_revision_id"]))
    with pytest.raises(ValueError, match="新版本"):
        place_model(db, LOCAL, b, placement)
    with pytest.raises(ValueError, match="波长"):
        place_model(db, LOCAL, b, PlaceModel(model_revision_id=v2["model_revision_id"], project_revision_id=placed["revision_id"],
                                             before_uid="M2"))
    first = library(db, LOCAL, LibraryQuery(limit=1))
    assert first["models"][0]["version"] == 2
    assert library(db, LOCAL, LibraryQuery(before_id=first["next_before_id"]))["models"] == [v1]


def test_model_scope_and_reference_integrity(db, client):
    a, rev = project(client, "source", source())
    alice = Principal(actor="alice", role="user", user_id=123)
    v1 = capture_model(db, alice, a, recipe(rev, include_power=True))
    assert library(db, LOCAL, LibraryQuery())["models"] == []
    with pytest.raises(ValueError, match="找不到"):
        library(db, LOCAL, LibraryQuery(model_revision_id=v1["model_revision_id"]))
    with pytest.raises(ValueError, match="找不到"):
        place_model(db, LOCAL, a, PlaceModel(model_revision_id=v1["model_revision_id"], project_revision_id=rev))
    single = source()
    single["parts"] = [{"element_index":0,"source":"fixed","locked_parameters":["matrix_t"]}]
    b, base = project(client, "before locked lens", single)
    placed = place_model(db, alice, b, PlaceModel(model_revision_id=v1["model_revision_id"], project_revision_id=base, before_uid="L"))
    els = placed["project"]["system"]["elements"]
    assert [e["uid"] for e in els[2:]] == ["L", "air"]
    assert placed["project"]["parts"][0]["element_index"] == 2
    assert placed["project"]["single_pass_power"]["input_w"]["low"] == 1.7
    with pytest.raises(ValueError, match="终点"):
        capture_model(db, alice, a, CaptureModel(project_revision_id=rev, name="invalid", first_uid="air", last_uid="L"))
    with pytest.raises(ValueError, match="belong"):
        capture_model(db, alice, b, recipe(rev))
    wrong = deepcopy(single)
    wrong["single_pass_power"]["wavelength_nm"] = 810
    c, bad = project(client, "incorrect budget", wrong)
    with pytest.raises(ValueError, match="预算"):
        capture_model(db, alice, c, recipe(bad, include_power=True))
    assert capture_model(db, alice, c, recipe(bad))["power_stages"] == 0


def test_edited_wavelength_carries_a_visible_assumption_through_preview_and_recapture(db, client):
    a, rev = project(client, "reference", source())
    preset = capture_model(db, LOCAL, a, recipe(rev))
    placed = place_model(db, LOCAL, a, PlaceModel(model_revision_id=preset["model_revision_id"], project_revision_id=rev))
    project_data = deepcopy(placed["project"])
    project_data["system"]["wavelength_nm"] = 850
    _, changed = project(client, "change wavelength", project_data, (a, placed["revision_id"]))
    retuned = read_project(db, a, changed)
    note, = retuned["analysis"]["model_warnings"]
    assert "808 nm" in note and "850 nm" in note
    preview = client.post(f"/api/cases/{a}/optical-project/preview", json=project_data["system"])
    assert preview.json()["model_warnings"] == [note]
    uids = [el["uid"] for el in project_data["system"]["elements"][-2:]]
    capture = CaptureModel(project_revision_id=changed, first_uid=uids[0], last_uid=uids[1], name="retuned")
    with pytest.raises(ValueError, match="依据或假设"):
        capture_model(db, LOCAL, a, capture)
    qualified = capture_model(db, LOCAL, a, capture.model_copy(update={"note": "Sensitivity study, same optical power assumed."}))
    assert qualified["wavelength_nm"] == 850


def test_builtin_catalog_matches_manual_and_agent_library_and_solver_models(client, db):
    from typing import get_args
    from app.workbench.authoring import EditableElement
    from app.workbench.optics import OpticalElement, OpticalScan, ScanAxis, _matrix
    import numpy as np

    case_id = client.post("/api/cases", json={"title": "独立空白工程"}).json()["id"]
    catalog = client.get(f"/api/cases/{case_id}/optical-project").json()["element_catalog"]
    assert library(db, LOCAL, LibraryQuery(include_builtin=True))["element_catalog"] == catalog
    models = catalog["models"]
    assert set(models) == set(get_args(OpticalElement.model_fields["kind"].annotation))
    assert {p["kind"] for p in catalog["presets"].values() if "prescription" not in p} == set(models) - {"registered"}
    assert {field for m in models.values() for field in m["parameters"]} == set(catalog["parameters"])
    for scan in (OpticalScan, ScanAxis):
        alternatives = scan.model_json_schema()["properties"]["parameter"]["anyOf"]
        assert set(alternatives[0]["enum"]) == set(catalog["parameters"])
        assert "pattern" in alternatives[1]
    for key, preset in catalog["presets"].items():
        draft = EditableElement.model_validate(preset)
        if key in {"space", "thick_lens", "grin"}:
            assert draft.n is None
            with pytest.raises(ValueError, match="折射率"):
                OpticalElement.model_validate(preset)
            # The researcher supplies the index before asking the solver to use this material.
            preset = {**preset, "n": 1.46}
            if "index_reference" in preset:
                preset["index_reference"] = {"wavelength_nm": 632.8, "model": None}
        element = OpticalElement.model_validate(preset)
        assert set(models[element.kind]["parameters"]) <= OpticalElement.model_fields.keys()
        for axis in ("tangential", "sagittal"):
            assert np.isclose(np.linalg.det(_matrix(element, axis, 632.8)), 1)
    with pytest.raises(ValueError, match="未注册"):
        _matrix(element.model_copy(update={"kind": "unimplemented"}), "tangential")

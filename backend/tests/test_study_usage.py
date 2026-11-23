"""Forward dependency queries must follow frozen bindings across unrelated kinds of work."""
import asyncio

import pytest

from app.models import Attachment, ExperimentCase, GeneratedContent
from app.workbench import tools
from app.workbench.contracts import ArtifactQuery
from app.workbench.study_usage import StudyUsage, _port_change, study_usage


@pytest.fixture
def archive(db):
    case = ExperimentCase(title="A", cavity_type="custom", goal="compare revisions")
    other = ExperimentCase(title="B", cavity_type="custom", goal="independent work")
    db.add_all([case, other])
    db.flush()

    def record(rid, content, kind="scientific_study", owner=case):
        db.add(GeneratedContent(id=rid, case_id=owner.id, content_type=kind, content=content))

    def study(rid, connections, **extra):
        record(rid, {"definition": {"title": "same displayed title"}, "connections": connections, **extra})

    def project(length):
        return {"project": {"title": "two saved geometries", "assemblies": [], "system": {
            "mode": "single_pass", "wavelength_nm": 532,
            "elements": [{"kind": "space", "uid": "bench/left", "name": "gap", "length_mm": length}]}}}

    record(51, project(125), "optical_project")
    record(85, project(190), "optical_snapshot")
    record(86, project(0), "optical_project")
    record(87, project(80), "optical_project", other)
    db.add_all([Attachment(id=51, case_id=case.id, filepath="run/a", filename="data.csv"),
                Attachment(id=85, case_id=case.id, filepath="run/b", filename="data.csv"),
                Attachment(id=87, case_id=other.id, filepath="other", filename="data.csv")])
    length = {"revision_id": 51, "port": "v1/primary/element/bench%2Fleft/length"}
    wavelength = {"revision_id": 51, "port": "v1/primary/wavelength"}
    # The terminal has a smaller ID than both parents, as in an imported archive.
    study(45, {"a": length})
    study(47, {"b": wavelength})
    study(38, {"x": {"run_id": 45, "port": "x"}, "b": wavelength})
    study(21, {"y": {"run_id": 38, "port": "y"}, "z": {"run_id": 45, "port": "z"}})
    study(22, {"x": {"run_id": 47, "port": "w"}})
    study(9, {}, inputs={"literal": 125, "notes": "copied from project #51"})
    study(65, {"t": {"table": {"attachment_id": 51}, "column": 3}}, originals=[
        {"attachment_id": 51, "input_paths": [["frames", 0], ["calibration"]]}])
    study(66, {"signal": {"run_id": 65, "port": "spectrum"}})
    study(67, {}, originals=[{"attachment_id": 51}])  # pre-role records only identify the file
    study(68, {}, originals=[{"attachment_id": 85}])
    record(8, {"connections": {"wrong": length}}, owner=other)
    db.commit()
    return case.id


def test_changed_inputs_and_independent_branches(db, archive):
    def query(**kwargs):
        return study_usage(db.get_bind(), archive, StudyUsage(source_id=51, **kwargs))
    result = query(compare_revision_id=85)
    records = {run["id"]: run for run in result["items"]}
    assert result["total"] == 5 and result["direct_count"] == 3 and result["terminal_count"] == 2
    assert result["review_count"] == 3
    assert {key: val["state"] for key, val in records.items()} == {
        21: "review", 22: "same", 38: "review", 45: "changed", 47: "same"}
    assert records[21]["via"] == [38, 45]
    change = records[45]["inputs"][0]
    assert (change["previous"], change["current"], change["current_unit"]) == (125, 190, "mm")
    assert records[38]["inputs"][0]["state"] == "same"  # its other input comes from a changed calculation
    first = query(view="terminal", limit=1)
    last = query(view="terminal", limit=1, offset=first["next_offset"])
    assert [first["items"][0]["id"], last["items"][0]["id"]] == [22, 21]
    assert last["next_offset"] is None
    missing = {item["id"]: item for item in query(compare_revision_id=86)["items"]}
    assert missing[45]["state"] == "unavailable" and missing[21]["state"] == "review"


def test_tables_raw_roles_and_study_links(db, archive):
    def query(**kwargs):
        return study_usage(db.get_bind(), archive, StudyUsage(**kwargs))
    records = {run["id"]: run for run in query(attachment_id=51)["items"]}
    assert records.keys() == {65, 66, 67}
    assert records[65]["inputs"] == [{"input": "t", "column": 3}, {"input_path": ["frames", 0]},
                                     {"input_path": ["calibration"]}]
    assert records[67]["inputs"] == [{"input_path": None}]
    assert records[66]["via"] == [65] and records[66]["terminal"]
    derived = query(source_id=45)
    assert derived["total"] == derived["direct_count"] == 2
    assert {run["id"] for run in query(attachment_id=85)["items"]} == {68}
    assert query(source_id=9)["total"] == 0
    for invalid in ({"source_id": 87}, {"attachment_id": 87}, {"source_id": 51, "compare_revision_id": 87},
                    {"source_id": 51, "attachment_id": 51}, {"source_id": 45, "compare_revision_id": 85}):
        with pytest.raises(ValueError):
            query(**invalid)


def test_equal_readings_with_different_conditions():
    def power(wavelength):
        return {"out": {"value": 2, "port": {"quantity": "power", "shape": "scalar", "unit": "W",
                "scale": 1, "label": "P", "conditions": {"wavelength": {"label": "λ"}},
                "condition_values": {"wavelength": wavelength}}}}
    change = _port_change(power(532), power(780), "out")
    assert change["state"] == "conditions" and not change["value_changed"]
    assert change["condition_changes"] == [{"key": "wavelength", "label": "λ", "previous": 532, "current": 780}]


def test_weight_evidence_shares_file_lookup_with_studies_and_opens_owned_comparison(client, db, archive):
    receipt = {"attachment_id": 51, "relative_path": "first/data.csv", "sha256": "a" * 64}
    def save(rid, baseline, originals, owner=archive):
        db.add(GeneratedContent(id=rid, case_id=owner, content_type="study_search", content={
            "title": "同名方案", "baseline_id": baseline, "source_search_id": 96,
            "adjustment_evaluation": {"distribution": {"source": "data.csv 见附件 #51", "originals": originals}}}))

    save(100, 9, [receipt])
    save(101, 65, [{**receipt, "attachment_id": 85}])    # Same name, different attachment.
    save(102, 9, [])                                     # Free text alone cannot bind a file.
    save(103, 9, [receipt], db.get(GeneratedContent, 8).case_id)
    save(104, 8, [receipt])                              # Explicit citation, wrong-case baseline.
    save(105, 9, "data.csv 见附件 #51")                  # Damaged receipts remain unlinked.
    db.commit()
    route = f"/api/cases/{archive}/studies/usage"
    result = client.post(route, json={"attachment_id": 51}).json()
    agent = asyncio.run(tools.execute("find_study_usage", StudyUsage(attachment_id=51), db=db,
        case=db.get(ExperimentCase, archive), task=None, provider=None, session_id=None, attachment_ids=[]))
    assert agent == result
    records = {run["id"]: run for run in result["items"]}
    assert records.keys() == {100, 104, 65, 66, 67}
    assert (result["total"], result["direct_count"], result["terminal_count"]) == (5, 4, 4)
    assert result["unreadable_binding_count"] == 1
    assert records[100]["workbench_view"] == {"run_id": 9, "comparison_id": 100}
    assert records[100]["inputs"] == [{"input_label": "工况权重依据", "relative_path": "first/data.csv", "sha256": "a" * 64}]
    assert records[100]["terminal"] and not records[100]["via"]
    assert records[104]["workbench_view"] is None and records[104]["navigation_issue"]
    assert records[66]["via"] == [65]
    first = client.post(route, json={"attachment_id": 51, "view": "terminal", "limit": 2}).json()
    second = client.post(route, json={"attachment_id": 51, "view": "terminal", "limit": 2,
                                      "offset": first["next_offset"]}).json()
    assert [run["id"] for run in first["items"] + second["items"]] == [104, 100, 67, 66]
    assert second["next_offset"] is None
    assert client.post(route, json={"source_id": 9}).json()["total"] == 0


def test_saved_result_discloses_shared_project_basis_without_inferring_from_literals(client, db, archive):
    root = f"/api/cases/{archive}/studies/runs/"
    def read(rid):
        response = client.get(root + str(rid))
        assert response.status_code == 200
        return response.json()["project_basis"]

    expected = {"id": 51, "title": "two saved geometries", "model_kind": "saved", "used_by": [38, 45]}
    assert read(21) == {"complete": True, "projects": [expected]}
    # A draft and a formal project can legitimately contribute to one calculation.
    old = db.get(GeneratedContent, 21)
    old.content = {**old.content, "connections": {**old.content["connections"], "draft": {"revision_id": 85, "port": "v1/layout"}}}
    db.commit()
    mixed = read(21)
    assert mixed == {"complete": True, "projects": [expected, {"id": 85, "title": "two saved geometries", "model_kind": "draft", "used_by": [21]}]}
    result = asyncio.run(tools.execute("read_artifact", ArtifactQuery(content_id=21, path="project_basis"), db=db,
        case=db.get(ExperimentCase, archive), task=None, provider=None, session_id=None, attachment_ids=[]))
    assert result["content"] == mixed
    assert read(9) == read(66) == {"complete": True, "projects": []}  # Hand entry or original file.

    # A damaged chain or an older record without binding metadata must remain readable.
    for inputs in ({}, {"connections": {"other": {"revision_id": 87}}},
                   {"connections": {"other": {"run_id": 8}}}, {"connections": {"cycle": {"run_id": 21}}}):
        parent = db.get(GeneratedContent, 38)
        parent.content = {"definition": {"title": "old"}, **inputs}
        db.commit()
        basis = read(21)
        assert not basis["complete"] and basis["message"]
        assert 87 not in {item["id"] for item in basis["projects"]}


@pytest.mark.parametrize("bad", [
    [], ["reference"], {"a": None}, {"a": {"run_id": [], "port": "x"}},
    {"a": {"revision_id": {}, "port": "x"}}, {"a": {"revision_id": True, "port": "x"}},
    {"a": {"revision_id": 2**66, "port": "x"}}, {"a": {"run_id": -1, "port": "x"}},
    {"a": {"table": None}}, {"a": {"table": {"attachment_id": []}}},
    {"a": {"run_id": 45, "revision_id": 51, "port": "x"}}, {"a": {"run_id": 45}},
])
def test_damaged_bindings_leave_outputs_readable_and_prevent_ambiguous_replays(client, db, archive, bad):
    root = f"/api/cases/{archive}/studies"
    parent = db.get(GeneratedContent, 38)
    parent.content = {**parent.content, "connections": bad, "output": {"reading": 1.75}}
    db.commit()
    for rid in (38, 21):
        response = client.get(root + f"/runs/{rid}")
        assert response.status_code == 200
        result = response.json()
        if rid == 38:
            assert result["output"]["reading"] == 1.75 and result["connections"] == bad
        basis = result["project_basis"]
        assert not basis["complete"] and basis["invalid_bindings_at"] == 38
        assert "#38" in basis["message"]
        failed = client.post(root+"/replay", json={"run_id": rid})
        assert failed.status_code == 422 and failed.json()["detail"] == basis["message"]
    usage = client.post(root+"/usage", json={"source_id": 51}).json()
    assert usage["unreadable_binding_count"] == 1
    assert {item["id"] for item in usage["items"]} == {21, 22, 45, 47}

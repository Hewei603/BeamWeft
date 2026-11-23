"""One context contract across task types; no experiment-specific template."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.models import AgentTask, AgentToolCall, Attachment, ExperimentCase, GeneratedContent
from app.workbench import service
from app.workbench.context import ContextRefs, attachment_page, selected_context, validate_context_refs, workspace_view
from app.workbench.projects import SaveProject, read_project, save_project


def project_case(db):
    case = ExperimentCase(title="单程光束传输", description="通用工程上下文验收", cavity_type="other", goal="传输分析")
    db.add(case)
    db.commit()
    first = save_project(db, case.id, SaveProject(project={
        "title": "单程参考", "system": {"mode": "single_pass", "wavelength_nm": 532,
        "input_radius_mm": 0.2, "elements": [{"uid": "propagation", "name": "自由空间", "kind": "space", "length_mm": 20}]},
        "parts": [{"element_index": 0, "role": "relay_path", "source": "仿真假设"}],
        "assumptions": ["理想近轴输入"]}, change_note="建立参考"))
    return case, first


def test_archive_directory_preserves_paths_paging_and_explicit_older_evidence(client, db):
    case, _ = project_case(db)
    other, _ = project_case(db)
    originals = []
    for i in range(35):
        original = Attachment(case_id=case.id, filename="signal.dat", filepath="unused",
                              relative_path=f"Day{i:02}/试件%_a/signal.dat" if i else None)
        db.add(original)
        originals.append(original)
    db.add(Attachment(case_id=other.id, filename="signal.dat", filepath="unused", relative_path="other/signal.dat"))
    db.commit()
    first = attachment_page(db, case.id, query="试件%_a SIGNAL", page_size=10)
    assert first["total_matches"] == 34
    second = attachment_page(db, case.id, query="试件%_a SIGNAL", before_id=first["next_before_id"], page_size=24)
    assert [a["id"] for a in first["items"]+second["items"]] == [a.id for a in originals[:0:-1]]
    assert second["next_before_id"] is None
    assert not attachment_page(db, case.id, query="other")["items"]
    assert attachment_page(db, case.id, query="试件_a")["items"] == []
    assert attachment_page(db, case.id, query="day01\\试件")["items"][0]["id"] == originals[1].id
    context = service.context_for_case(db, case, 0, [originals[0].id])
    assert len(context["attachments"]) == 12 and context["archive"]["total_files"] == 35
    assert context["archive"]["selected_files"][0]["id"] == originals[0].id
    url = f"/api/cases/{case.id}/workbench"
    light = client.get(url, params={"include_attachments":False}).json()
    assert light["archive_index"] == {"count":35, "newest_id":originals[-1].id}
    assert "attachments" not in light and "attachments" not in light["workspace"]
    full = client.get(url).json()
    assert len(full["attachments"]) == 35 and full["attachments"] == full["workspace"]["attachments"]


def test_historical_context_does_not_restore_current_engineering(db):
    case, first = project_case(db)
    model = deepcopy(first["project"])
    model["system"]["elements"][0]["length_mm"] = 35
    second = save_project(db, case.id, SaveProject(base_revision_id=first["revision_id"], project=model, change_note="调整传播距离"))
    context = selected_context(db, case.id, {"project_revision_id": first["revision_id"], "element_uids": ["propagation"]})
    assert context["project"]["system"]["elements"][0]["length_mm"] == 20
    assert context["is_current_project"] is False
    assert read_project(db, case.id)["revision_id"] == second["revision_id"]
    assert workspace_view(db, case)["elements"][0]["length_mm"] == 35


def test_element_reference_requires_its_actual_project(db):
    case, first = project_case(db)
    with pytest.raises(ValidationError):
        ContextRefs(element_uids=["propagation"])
    with pytest.raises(ValueError, match="引用元件"):
        validate_context_refs(db, case.id, ContextRefs(project_revision_id=first["revision_id"], element_uids=["missing"]))


def test_scoped_selections_validate_and_survive_snapshot_capture(client, db, monkeypatch):
    case, first = project_case(db)
    async def no_provider(task_id):
        pass
    monkeypatch.setattr(service, "execute_run", no_provider)
    model = deepcopy(first["project"])
    path = {"uid":"probe/a", "name":"same", "target_uid":"propagation",
            "elements":[{"uid":"propagation", "kind":"space", "length_mm":35, "name":"same"}]}
    planes=[{"uid":"detector/a","name":"screen","position_reference":"fixed_z","z_mm":5},
            {"uid":"detector/b","name":"screen","target_uid":"removed-mount","depth_mm":3}]
    model.update(observation_planes=planes, incident_paths=[path], network={"components":[{"uid":"tap/b", "name":"same", "model":{
        "kind":"matrix", "ports":["left/1"], "matrix":[[[1, 0]]]}}]})
    url = f"/api/cases/{case.id}/workbench/runs"
    arm = {"path_uid":"probe/a", "element_uid":"propagation"}
    port = {"component":"tap/b", "port":"left/1"}
    for invalid in ({"path_uid":"probe/a", "element_uid":"removed"}, {**arm, "path_uid":"removed"},
                    {**port, "port":"left"}, {**port, "component":"removed"},
                    {"plane_uid":"screen"}, {"plane_uid":"missing","beam_uid":"primary"},
                    {"plane_uid":"detector/b","beam_uid":"missing"}, {"plane_uid":"detector/a",**arm}):
        count = db.query(GeneratedContent).count()
        refused = client.post(url, json={"message":"检查选择", "context_refs":{"project_objects":[invalid]},
                                          "working_draft":{"project":model,"source_revision_id":first["revision_id"],"base_revision_id":first["revision_id"]}})
        assert refused.status_code == 422
        assert db.query(GeneratedContent).count() == count and not db.query(AgentTask).count()
    selections = [arm, {"path_uid":"probe/a","element_uid":None}, port,
                  {"plane_uid":"detector/a","beam_uid":None}, {"plane_uid":"detector/a","beam_uid":"primary"},
                  {"plane_uid":"detector/b","beam_uid":"probe/a"}]
    body = {"message":"读取所选对象", "context_refs":{"project_objects":[*selections, arm, {"plane_uid":"detector/a"}]}}
    assert client.post(url, json=body).status_code == 422
    response = client.post(url, json={**body, "working_draft":{"project":model, "source_revision_id":first["revision_id"],
                                                              "base_revision_id":first["revision_id"]}})
    try:
        assert response.status_code == 202, response.text
        request = db.query(AgentToolCall).filter_by(task_id=response.json()["task_id"],tool_name="workbench_request").one()
        context = selected_context(db, case.id, request.input_json["context_refs"])
        assert context["project_objects"] == selections
        assert context["project_revision_id"] == response.json()["model_basis"]["revision_id"]
        assert context["project"]["incident_paths"][0]["elements"][0]["length_mm"] == 35
        assert context["project"]["system"]["elements"][0]["length_mm"] == 20
        assert context["project"]["observation_planes"][1]["target_uid"] == "removed-mount"
        with pytest.raises(ValueError):
            selected_context(db, case.id, {"project_revision_id": first["revision_id"], "project_objects":selections})
        with pytest.raises(ValueError,match="找不到观察面 detector/a"):
            selected_context(db, case.id, {"project_revision_id": first["revision_id"], "project_objects":[{"plane_uid":"detector/a"}]})
    finally:
        service.ACTIVE_RUNS.clear()


@pytest.mark.parametrize("kind", ["attachment", "artifact", "tool", "project"])
def test_references_cannot_silently_use_another_case(db, kind):
    case, first = project_case(db)
    other = ExperimentCase(title="另一个实验", cavity_type="other", goal="另一个任务")
    db.add(other)
    db.flush()
    attachment = Attachment(case_id=other.id, filename="measurement.csv", filepath="/not-read.csv")
    artifact = GeneratedContent(case_id=other.id, content_type="data", content={})
    task = AgentTask(case_id=other.id, goal="独立实验", mode="workbench")
    db.add_all([attachment, artifact, task])
    db.flush()
    call = AgentToolCall(task_id=task.id, tool_name="fit_power_curve")
    db.add(call)
    db.flush()
    inputs = {"attachment": {"attachment_ids": [attachment.id]}, "artifact": {"artifact_ids": [artifact.id]},
              "tool": {"tool_call_ids": [call.id]}, "project": {"project_revision_id": first["revision_id"]}}
    with pytest.raises(ValueError):
        validate_context_refs(db, other.id if kind == "project" else case.id, ContextRefs(**inputs[kind]))


def test_frontend_reference_survives_request_and_uses_same_context(client, db, monkeypatch):
    case, first = project_case(db)
    async def no_live_provider(task_id):
        return None
    monkeypatch.setattr(service, "execute_run", no_live_provider)
    refs = {"project_revision_id": first["revision_id"], "element_uids": ["propagation"]}
    response = client.post(f"/api/cases/{case.id}/workbench/runs", json={"message": "比较这个对象的传播距离", "context_refs": refs})
    try:
        assert response.status_code == 202, response.text
        request = db.query(AgentToolCall).filter_by(task_id=response.json()["task_id"], tool_name="workbench_request").one()
        assert request.input_json["context_refs"]["element_uids"] == ["propagation"]
        assert request.input_json["principal"]["actor"]
        view = client.get(f"/api/cases/{case.id}/workbench").json()
        assert view["workspace"]["mode"] == "single_pass"
        assert view["workspace"]["project_revision_id"] == first["revision_id"]
        assert view["messages"][-1]["metadata"]["context_refs"]["element_uids"] == ["propagation"]
    finally:
        service.ACTIVE_RUNS.clear()


def test_draft_object_is_bound_to_captured_model_or_entire_run_rolls_back(client, db, monkeypatch):
    case, first = project_case(db)
    async def no_live_provider(task_id):
        pass
    monkeypatch.setattr(service, "execute_run", no_live_provider)
    draft = deepcopy(first["project"])
    draft["system"]["elements"].append({"uid": "draft-only", "kind": "lens", "name": "L1", "focal_mm": 35})
    url = f"/api/cases/{case.id}/workbench/runs"
    payload = {"message": "检查所选透镜", "working_draft": {"project": draft, "base_revision_id": first["revision_id"],
               "source_revision_id": first["revision_id"]}, "context_refs": {"element_uids": ["missing"]}}
    counts = (db.query(AgentTask).count(), db.query(GeneratedContent).count())
    assert client.post(url, json=payload).status_code == 422
    assert (db.query(AgentTask).count(), db.query(GeneratedContent).count()) == counts
    assert client.post(url, json={"message": "检查元件", "context_refs": {"element_uids": ["propagation"]}}).status_code == 422
    assert (db.query(AgentTask).count(), db.query(GeneratedContent).count()) == counts
    payload["context_refs"]["element_uids"] = ["draft-only"]
    response = client.post(url, json=payload)
    try:
        assert response.status_code == 202, response.text
        snapshot = response.json()["model_basis"]["revision_id"]
        request = db.query(AgentToolCall).filter_by(task_id=response.json()["task_id"], tool_name="workbench_request").one()
        context = selected_context(db, case.id, request.input_json["context_refs"])
        assert context["project_revision_id"] == snapshot
        assert context["element_uids"] == ["draft-only"]
        assert context["project"]["system"]["elements"][-1]["focal_mm"] == 35
        assert read_project(db, case.id)["revision_id"] == first["revision_id"]
    finally:
        service.ACTIVE_RUNS.clear()

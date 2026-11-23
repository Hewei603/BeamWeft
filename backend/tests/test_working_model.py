"""The model a user asks about stays identifiable throughout a study and its adoption."""
import asyncio
import json
from copy import deepcopy

import pytest

from app.models import AgentTask, AgentToolCall, ExperimentCase, GeneratedContent
from app.workbench import service
from app.workbench.projects import SaveProject, read_project, save_project
from app.workbench.study_inputs import WorkingDraft, basis, capture_snapshot
from app.workbench.tools import REGISTRY, execute


def new_case(db):
    case = ExperimentCase(title="光学试算", cavity_type="other", goal="比较模型", parameters={})
    db.add(case)
    db.commit()
    return case


def model():
    return {"title":"模式匹配", "system":{"mode":"standing_wave","wavelength_nm":1064,"elements":[
        {"uid":"m1","name":"M1","kind":"mirror"},
        {"uid":"air","name":"自由空间","kind":"space","length_mm":60},
        {"uid":"m2","name":"M2","kind":"mirror","radius_mm":180}]},
        "incident_paths":[{"uid":"pump","name":"入射束","target_uid":"air","target_n":1,"wavelength_nm":808,
            "tangential":{"radius_mm":0.4,"m2":1,"wavefront":"waist"},
            "power_budget":{"wavelength_nm":808,"input_w":{"low":1},"stages":[
            {"element_uid":"air","scope":"host","law":{"mode":"net","retained":{"low":0.9}}}]}}]}


def save(db, case, project, parent=None):
    return save_project(db, case.id, SaveProject(project=project, base_revision_id=parent, change_note="验收"))


def draft(db, case, project, parent=None):
    record = capture_snapshot(db, case.id, WorkingDraft(project=project, source_revision_id=parent, base_revision_id=parent))
    db.commit()
    return record


def run_tool(db, case, task, working, name, **values):
    args = REGISTRY[name][0].model_validate(values)
    return asyncio.run(execute(name, args, db=db, case=case, task=task, working=working, provider=None,
                               session_id=None, attachment_ids=[]))


def test_named_variants_share_the_editor_and_agent_history_without_moving_the_head(client, db):
    from app.workbench.study_inputs import case_for_planning

    case = new_case(db)
    original = save(db, case, model())
    base = original["revision_id"]
    url = f"/api/cases/{case.id}/optical-project"
    payload = dict(project=model(), base_revision_id=base, source_revision_id=base, variant_name="  A_50%  ")
    first = client.post(url + "/variants", json=payload)
    assert first.status_code == 200, first.text
    a = first.json()
    assert client.post(url + "/variants", json=payload).json()["revision_id"] == a["revision_id"]
    task = AgentTask(case_id=case.id, mode="workbench", status="running", goal="比较两份独立方案")
    db.add(task)
    db.commit()
    opened = run_tool(db, case, task, basis(original), "begin_optical_study", variant_name="A050B")["model_basis"]
    assert opened["revision_id"] != a["revision_id"] and opened["base_revision_id"] == base
    changed = model()
    changed["system"]["elements"][1]["length_mm"] = 110
    next_head = save(db, case, changed, base)
    changed["system"]["elements"][1]["length_mm"] = 95
    b = run_tool(db, case, task, opened, "save_optical_project", project=changed,
                 base_revision_id=opened["revision_id"], change_note="调整空气段")
    assert b["source_revision_id"] == opened["revision_id"] and b["variant_name"] == "A050B"
    assert b["base_revision_id"] == base
    view = case_for_planning(db, case, basis(b))
    assert view.parameters["current_optical_system"]["elements"][1]["length_mm"] == 95
    assert view.parameters["current_optical_scope_warnings"] == b["analysis"].get("scope_warnings", [])
    assert case.parameters["current_optical_project_revision_id"] == next_head["revision_id"]
    rows = run_tool(db, case, task, basis(b), "read_optical_project", list_versions=True, variant_name="_50%")["items"]
    assert json.loads(json.dumps(rows)) == client.get(url + "/versions", params={"variant_name":"_50%"}).json()["items"]
    assert [(r["id"], r["variant_name"], r["base_revision_id"]) for r in rows] == [(a["revision_id"], "A_50%", base)]
    bad = {**payload, "source_revision_id":b["revision_id"], "base_revision_id":next_head["revision_id"]}
    assert client.post(url + "/variants", json=bad).status_code == 422
    foreign = new_case(db)
    assert client.post(f"/api/cases/{foreign.id}/optical-project/variants", json=payload).status_code == 422
    assert client.get(url).json()["current"]["revision_id"] == next_head["revision_id"]


def test_named_study_from_empty_keeps_its_identity_on_first_agent_save(db):
    case = new_case(db)
    task = AgentTask(case_id=case.id, mode="workbench", status="running", goal="从零搭一份试算")
    db.add(task)
    db.commit()
    working = run_tool(db, case, task, basis(None), "begin_optical_study", variant_name="初稿")["model_basis"]
    first = run_tool(db, case, task, working, "save_optical_project", project=model(),
                     base_revision_id=None, change_note="首次搭建")
    assert first["variant_name"] == "初稿" and first["base_revision_id"] is None
    assert read_project(db, case.id) is None


@pytest.mark.parametrize("initially_empty", [False, True])
def test_question_pins_model_even_if_project_changes_before_tools_run(client, db, monkeypatch, initially_empty):
    case = new_case(db)
    first = None if initially_empty else save(db, case, model())["revision_id"]
    async def queued(_):
        pass
    monkeypatch.setattr(service, "execute_run", queued)
    reply = client.post(f"/api/cases/{case.id}/workbench/runs", json={"message":"请计算这份光路"})
    assert reply.status_code == 202, reply.text
    task = db.get(AgentTask, reply.json()["task_id"])
    try:
        accepted = reply.json()["model_basis"]
        assert accepted["revision_id"] == first
        internal = db.query(AgentToolCall).filter_by(task_id=task.id, tool_name="workbench_request").one()
        assert internal.input_json["model_basis"] == accepted
        modified = model()
        modified["system"]["elements"][1]["length_mm"] = 85
        newest = save(db, case, modified, first)
        view = run_tool(db, case, task, accepted, "read_optical_project")["project_revision"]
        if initially_empty:
            assert view is None
        else:
            assert view["project"]["system"]["elements"][1]["length_mm"] == 60
        head = run_tool(db, case, task, accepted, "read_optical_project", case_head=True)["project_revision"]
        assert head["revision_id"] == newest["revision_id"]
        with pytest.raises(ValueError, match="本轮"):
            run_tool(db, case, task, accepted, "save_optical_project", project=modified,
                     base_revision_id=newest["revision_id"], change_note="错误基线")
        if initially_empty:
            assert run_tool(db, case, task, accepted, "analyze_path_power")["status"] == "needs_input"
    finally:
        service.ACTIVE_RUNS.discard(task.id)


def test_invalid_draft_rolls_back_without_phantom_question(client, db):
    case, other = new_case(db), new_case(db)
    own = save(db, case, model())["revision_id"]
    foreign = save(db, other, model())["revision_id"]
    url = f"/api/cases/{case.id}/workbench/runs"
    for source, base, refs in ((foreign,own,{}), (own,foreign,{}), (own,own,{"project_revision_id":own})):
        reply = client.post(url, json={"message":"只计算","working_draft":{
            "project":model(),"source_revision_id":source,"base_revision_id":base},"context_refs":refs})
        assert reply.status_code == 422, reply.text
        assert db.query(AgentTask).filter_by(case_id=case.id).count() == 0
        assert db.query(GeneratedContent).filter_by(case_id=case.id,content_type="optical_snapshot").count() == 0
    assert client.get(f"/api/cases/{other.id}/optical-project/model-input", params={"revision_id":own}).status_code == 404


def test_branch_uses_incident_power_and_scan_records_only_its_actual_system(db):
    case = new_case(db)
    original = save(db, case, model())
    params = deepcopy(case.parameters)
    changed = deepcopy(original["project"])
    changed["incident_paths"][0]["power_budget"]["input_w"]["low"] = 2
    snap = draft(db, case, changed, original["revision_id"])
    task = AgentTask(case_id=case.id, mode="workbench", status="running", goal="比较")
    db.add(task)
    db.commit()
    def tool(name, **args):
        return run_tool(db, case, task, basis(snap), name, **args)
    power = tool("analyze_path_power")
    assert power["revision_id"] == snap["revision_id"]
    assert power["power_budgets"][0]["summary"]["outgoing"]["watts"] == pytest.approx([1.8, 1.8])
    scan = dict(system=changed["system"], element_index=1, parameter="length_mm", start=50, stop=70, count=3)
    linked = tool("scan_optical_system", **scan)
    assert linked["project_revision_id"] == snap["revision_id"]
    assert any("快照" in warning for warning in linked["scope_warnings"])
    scan["system"]["elements"][2]["radius_mm"] = 45
    assert tool("scan_optical_system", **scan)["project_revision_id"] is None
    child = tool("save_optical_project", base_revision_id=snap["revision_id"], project=changed, change_note="改变镜曲率")
    assert child["base_revision_id"] == original["revision_id"] and child["source_revision_id"] == snap["revision_id"]
    assert child["analysis"]["planes"]["tangential"]["stable"] is False
    assert read_project(db, case.id)["revision_id"] == original["revision_id"] and case.parameters == params
    assert read_project(db, case.id, snap["revision_id"])["project"]["system"]["elements"][2]["radius_mm"] == 180


def test_adoption_is_retryable_but_cannot_replay_over_a_different_successor(client, db):
    case = new_case(db)
    original = save(db, case, model())
    base = original["revision_id"]
    changed = deepcopy(original["project"])
    changed["system"]["elements"][1]["length_mm"] = 74
    first, other = [draft(db, case, changed, base)["revision_id"] for _ in range(2)]
    url = f"/api/cases/{case.id}/optical-project/adopt-snapshot"
    request = {"base_revision_id":base, "snapshot_id":first}
    installed = client.post(url, json=request)
    assert installed.status_code == 200, installed.text
    installed = installed.json()
    assert installed["adopted_snapshot_id"] == first
    later = save(db, case, {**changed, "notes":"实验员记录"}, installed["revision_id"])
    replay = client.post(url, json=request)
    assert replay.status_code == 200 and replay.json()["revision_id"] == installed["revision_id"]
    request["snapshot_id"] = other
    assert client.post(url, json=request).status_code == 409
    assert read_project(db, case.id)["revision_id"] == later["revision_id"]
    with pytest.raises(ValueError, match="工程基线"):
        capture_snapshot(db, case.id, WorkingDraft(project=changed, source_revision_id=first,
                                                   base_revision_id=later["revision_id"]))
    request.update(reviewed_against=later["revision_id"], reviewed_project={**later["project"], "title":"核对后的试算"})
    merged = client.post(url, json=request)
    assert merged.status_code == 200, merged.text
    assert merged.json()["reviewed_snapshot_id"] == other
    assert merged.json()["parent_revision_id"] == later["revision_id"]
    assert client.post(url, json=request).json()["revision_id"] == merged.json()["revision_id"]
    request["reviewed_project"]["notes"] = "另一份合并结果"
    assert client.post(url, json=request).status_code == 409


def test_hypothetical_locked_hardware_cannot_be_published_without_unlock(client, db):
    case = new_case(db)
    original = model()
    original["parts"] = [{"element_index":2,"role":"laser_output","source":"实物记录","locked_parameters":["radius_mm"]}]
    first = save(db, case, original)
    changed = deepcopy(first["project"])
    changed["system"]["elements"][2]["radius_mm"] = 120
    trial = draft(db, case, changed, first["revision_id"])
    response = client.post(f"/api/cases/{case.id}/optical-project/adopt-snapshot",
        json={"base_revision_id":first["revision_id"],"snapshot_id":trial["revision_id"]})
    assert response.status_code == 422
    assert read_project(db, case.id)["project"]["system"]["elements"][2]["radius_mm"] == 180


@pytest.mark.parametrize("from_empty", [True, False])
def test_study_can_start_from_scratch_or_saved_and_continue_an_older_candidate(db, from_empty):
    case = new_case(db)
    original = None if from_empty else save(db, case, model())
    task = AgentTask(case_id=case.id,mode="workbench",status="running",goal="先试一下，再决定")
    db.add(task)
    db.commit()
    initial = basis(original)
    opened = run_tool(db, case, task, initial, "begin_optical_study")["model_basis"]
    changed = model()
    changed["system"]["elements"][1]["length_mm"] = 45
    candidate = run_tool(db, case, task, opened, "save_optical_project", base_revision_id=opened["revision_id"],
                         project=changed, change_note="试算候选")
    assert candidate["model_kind"] == "draft" and candidate["base_revision_id"] == initial["revision_id"]
    assert basis(read_project(db, case.id)) == initial
    newest = save(db, case, model(), initial["revision_id"])
    continued = run_tool(db, case, task, basis(newest), "begin_optical_study", revision_id=candidate["revision_id"])
    assert continued["model_basis"] == basis(candidate)
    assert continued["project"]["system"]["elements"][1]["length_mm"] == 45
    assert read_project(db, case.id)["revision_id"] == newest["revision_id"]

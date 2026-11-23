from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from app.api.workbench import recover, task_json
from app.database import Base
from app import run_ownership
from app.run_ownership import RunLease, owner_state
from app.models import AgentChatMessage, AgentChatSession, AgentStep, AgentTask, AgentToolCall, ExperimentCase, GeneratedContent
from app.workbench import service


@pytest.fixture(autouse=True)
def local_witness_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(run_ownership, "_folder", lambda:tmp_path / "leases")


def add_run(db, case_id, status="running", stamp="released"):
    row = AgentTask(case_id=case_id, mode="workbench", status=status, goal="比较已测条件",
                    created_at=datetime(2001, 1, 1, tzinfo=timezone.utc))
    db.add(row)
    db.flush()
    if stamp == "released":
        lease = RunLease(db.get_bind())
        stamp = lease.stamp
        lease.close()
    if stamp is not None:
        call(db, row, "workbench_request", "completed", inputs={"execution_owner":stamp})
    return row


def call(db, run, name, state, output=None, inputs=None):
    row = AgentToolCall(task_id=run.id, tool_name=name, status=state, input_json=inputs or {}, output_json=output or {})
    db.add(row)
    db.flush()
    return row


def test_direct_open_of_old_task_recovers_only_its_scope_and_preserves_history(client, db):
    case_id = client.post("/api/cases", json={"title":"受中断的实验"}).json()["id"]
    other_id = client.post("/api/cases", json={"title":"另一个实验"}).json()["id"]
    run, other = add_run(db, case_id), add_run(db, other_id)
    done = call(db, run, "save_optical_project", "completed", {"revision_id":5})
    partial = call(db, run, "analyze_optical_system", "partial", {"status":"partial", "known":True})
    failed = call(db, run, "scan_optical_system", "failed", {"error":"缺少扫描边界"})
    pending = call(db, run, "scan_optical_system", "running")
    no_output = call(db, run, "empty_trace", "completed")
    other_call = call(db, other, "unrelated", "running")
    step = AgentStep(task_id=run.id, step_index=1, title="扫描", status="running")
    db.add(step)
    db.commit()
    url = f"/api/cases/{case_id}/workbench/runs/{run.id}"
    result = client.get(url).json()
    assert result["status"] == "failed" and not result["active"]
    assert result["result_origin"] == "system_receipt" and result["final_content_id"] is None
    assert result["result"]["status"] == "partial"
    assert result["result"]["evidence_tool_call_ids"] == [done.id, partial.id]
    assert no_output.id not in result["result"]["evidence_tool_call_ids"]
    assert "模型最终答复" in result["result"]["summary"]
    assert [x["id"] for x in result["tools"] if x["status"] == "failed"] == [failed.id, pending.id]
    assert result["steps"][0]["status"] == "failed"
    assert client.get(url).json() == result
    assert client.get(f"/api/cases/{other_id}/workbench/runs/{run.id}").status_code == 404
    db.refresh(other_call)
    assert other_call.status == "running"
    assert not db.query(AgentChatMessage).count() and not db.query(GeneratedContent).count()


def test_preexisting_cancelled_and_still_active_tasks_keep_their_disposition(client, db, monkeypatch):
    case_id = client.post("/api/cases", json={"title":"取消与正在运行"}).json()["id"]
    lease = RunLease(db.get_bind())
    cancelled, active = add_run(db, case_id, "cancelled"), add_run(db, case_id, stamp=lease.stamp)
    db.commit()
    monkeypatch.setattr(service, "ACTIVE_RUNS", set())
    url = f"/api/cases/{case_id}/workbench/runs/"
    stopped = client.get(url+str(cancelled.id)).json()
    assert stopped["status"] == stopped["result"]["status"] == "cancelled"
    assert stopped["result_origin"] == "system_receipt"
    live = client.get(url+str(active.id)).json()
    assert live["status"] == "running" and live["result"] is None and live["active"]
    lease.close()


def test_legacy_unknown_remote_and_different_databases_never_certify_a_dead_worker(client, db, tmp_path):
    cid = client.post("/api/cases", json={"title":"同秒启动窗口"}).json()["id"]
    remote = {"version":1, "storage":"unknown", "token":"1"*32}
    legacy = add_run(db, cid, stamp=None)
    unknown = add_run(db, cid, stamp=remote)
    lease = RunLease(db.get_bind())
    try:
        other = create_engine(URL.create("sqlite", database=str(tmp_path / "other.db")))
        try:
            with other.connect():
                assert owner_state(other, lease.stamp) == "unverified"
        finally:
            other.dispose()
        assert owner_state(db.get_bind(), lease.stamp) == "live"
        for token in ("../../anything", "0"*32):
            assert owner_state(db.get_bind(), {**lease.stamp,"token":token}) == "unverified"
    finally:
        lease.close()
    db.commit()
    base = f"/api/cases/{cid}/workbench/runs/"
    for task in (legacy,unknown):
        out = client.get(base+str(task.id)).json()
        assert out["status"] == "running" and not out["active"]
        assert out["result_origin"] == "system_receipt" and out["result"]["status"] == "unverified"
        assert out["execution_owner_state"] == "unverified"
    client.post(base+str(legacy.id)+"/stop")
    assert client.get(base+str(legacy.id)).json()["status"] == "cancelled"
    assert client.get(base+str(unknown.id)).json()["status"] == "running"


@pytest.mark.asyncio
async def test_witness_cleanup_follows_durable_terminal_commit_and_retries_a_windows_reader(client, db, monkeypatch):
    cid = client.post("/api/cases", json={"title":"完成与中断"}).json()["id"]
    bind = db.get_bind()
    lease = RunLease(bind)
    row = add_run(db,cid,stamp=lease.stamp)
    db.commit()
    attempts = []
    unlink = Path.unlink
    def briefly_busy(path, *args, **kwargs):
        if path == lease.file:
            attempts.append(path)
            if len(attempts) == 1:
                raise PermissionError("Read handle has not closed yet")
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", briefly_busy)
    async def completed(task_id):
        with Session(bind) as session:
            session.get(AgentTask,task_id).status = "completed"
            session.commit()
    await lease.execute(completed,row.id)
    assert len(attempts) == 2 and not lease.file.exists()
    abrupt = RunLease(bind)
    unfinished = add_run(db,cid,stamp=abrupt.stamp)
    db.commit()
    async def broken(_):
        raise RuntimeError("test interruption")
    with pytest.raises(RuntimeError):
        await abrupt.execute(broken,unfinished.id)
    assert abrupt.file.exists() and owner_state(bind,abrupt.stamp) == "released"
    recover(db,cid,unfinished.id)
    db.refresh(unfinished)
    assert unfinished.status == "failed" and not abrupt.file.exists()


def test_recovery_can_display_an_actual_completed_finish_but_rejects_unowned_evidence(client, db):
    case_id = client.post("/api/cases", json={"title":"已收到模型答复"}).json()["id"]
    run = add_run(db, case_id)
    output = call(db, run, "query", "completed", {"matched":12})
    answer = {"status":"completed","summary":"模型记录的实际答复","evidence_tool_call_ids":[output.id]}
    call(db, run, "finish", "completed", {"message":"已校验答复"}, answer)
    other = add_run(db, case_id)
    call(db, other, "finish", "completed", {"message":"旧记录"}, answer)
    db.commit()
    path = f"/api/cases/{case_id}/workbench/runs/"
    recovered = client.get(path+str(run.id)).json()
    assert recovered["result_origin"] == "recorded_model_finish"
    assert recovered["status"] == "failed" and recovered["result"]["summary"] == answer["summary"]
    rejected = client.get(path+str(other.id)).json()
    assert rejected["result_origin"] == "system_receipt" and not rejected["result"]["evidence_tool_call_ids"]


def test_legacy_artifact_needs_a_matching_validated_finish_for_model_attribution(client, db):
    cid = client.post("/api/cases", json={"title":"未标注来源的历史记录"}).json()["id"]
    answer = service.validate_finish({"summary":"旧实验摘要","status":"completed"}, {}).model_dump()
    for saved_text, expected in ((answer["summary"], "recorded_model_finish"), ("归档中的其他文本", "archived")):
        run = add_run(db,cid,status="completed",stamp=None)
        call(db,run,"finish","completed",{"accepted":True},answer)
        artifact = GeneratedContent(case_id=cid,content_type="workbench",content={"workbench":{**answer,"summary":saved_text}})
        db.add(artifact)
        db.flush()
        run.final_content_id = artifact.id
        db.commit()
        record = client.get(f"/api/cases/{cid}/workbench/runs/{run.id}").json()
        assert record["result_origin"] == expected and record["result"]["summary"] == saved_text
        db.refresh(artifact)
        assert "result_origin" not in artifact.content


def test_concurrent_first_reads_then_repeated_terminal_writes_have_one_result(tmp_path):
    engine = create_engine(URL.create("sqlite", database=str(tmp_path / "recovery.db")))
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            project = ExperimentCase(title="多窗口", cavity_type="custom", goal="")
            db.add(project)
            db.flush()
            cid = project.id
            chat = AgentChatSession(case_id=cid)
            db.add(chat)
            run = add_run(db, cid)
            rid = run.id
            evidence = call(db, run, "read", "completed", {"power":.01}).id
            sid = chat.id
            db.commit()
        def view_or_save(write, barrier):
            with Session(engine) as db:
                barrier.wait(timeout=10)
                recover(db, cid, rid)
                task = db.get(AgentTask, rid)
                if write:
                    result, origin = service.result_from_trace(task)
                    service._save_result(db, task.case, task, {"session_id":sid}, result, {}, 0, origin=origin)
                return task_json(task)
        for write in (False, True):
            barrier = Barrier(4)
            with ThreadPoolExecutor(max_workers=4) as workers:
                results = list(workers.map(lambda _:view_or_save(write, barrier), range(4)))
            assert all(r["result_origin"] == "system_receipt" and r["result"]["evidence_tool_call_ids"] == [evidence]
                       for r in results)
            with Session(engine) as db:
                assert db.query(GeneratedContent).count() == int(write)
                assert db.query(AgentChatMessage).count() == int(write)
    finally:
        engine.dispose()

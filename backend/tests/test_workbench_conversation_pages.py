from sqlalchemy import event

from app.models import AgentChatMessage, AgentChatSession, AgentStep, AgentTask, ExperimentCase, GeneratedContent


def test_dialogue_pages_are_complete_stable_and_confined_to_the_project_session(client, db):
    def shared(name):
        case_id = client.post("/api/cases", json={"title":name}).json()["id"]
        session = AgentChatSession(case_id=case_id)
        db.add(session)
        db.flush()
        db.get(ExperimentCase, case_id).parameters = {"workbench_session_id":session.id}
        return case_id, session.id

    case_id, sid = shared("optical train")
    _, other = shared("power trace")
    ids = []
    for i in range(47):
        row = AgentChatMessage(session_id=sid, role="user", content=f"Observation {i}", metadata_json={"index":i})
        db.add_all([row, AgentChatMessage(session_id=other, role="user", content="other project")])
        db.flush()
        ids.append(row.id)
    db.commit()
    url = f"/api/cases/{case_id}/workbench"
    all_messages = client.get(url).json()["messages"]
    first = client.get(url, params={"message_limit":20}).json()
    assert [m["id"] for m in first["messages"]] == ids[-20:]
    db.add(AgentChatMessage(session_id=sid, role="assistant", content="Late reply"))
    db.commit()
    loaded = first["messages"]
    cursor = first["message_page"]["next_before_id"]
    while cursor is not None:
        page = client.get(url+"/messages", params={"before_id":cursor, "limit":15}).json()
        assert page["message_page"]["session_id"] == sid
        assert page["messages"][-1]["id"] < cursor
        loaded = page["messages"] + loaded
        cursor = page["message_page"]["next_before_id"]
    assert loaded == all_messages
    assert client.get(url+"/messages", params={"before_id":ids[0]}).json()["messages"] == []
    assert client.get(url, params={"message_limit":101}).status_code == 422
    db.get(ExperimentCase, case_id).parameters = {"workbench_session_id":other}
    db.commit()
    assert client.get(url, params={"message_limit":1}).json()["messages"] == []
    assert client.get(url+"/messages", params={"before_id":100000}).json()["messages"] == []


def test_recent_task_summaries_batch_steps_and_results(client, db):
    case_id = client.post("/api/cases", json={"title":"temperature sweep"}).json()["id"]
    for i in range(26):
        final = GeneratedContent(case_id=case_id,content_type="workbench",content={"workbench":{"summary":str(i)}})
        db.add(final)
        db.flush()
        task = AgentTask(case_id=case_id,mode="workbench",status="completed",goal="measure",final_content_id=final.id)
        db.add(task)
        db.flush()
        db.add(AgentStep(task_id=task.id,step_index=1,title="compare",status="completed",result_summary=str(i)))
    db.commit()
    sql = []
    def count(conn, cursor, statement, parameters, context, many):
        sql.append(statement.lower())
    event.listen(db.get_bind(),"before_cursor_execute",count)
    try:
        data = client.get(f"/api/cases/{case_id}/workbench",params={"include_latest_tools":False}).json()
    finally:
        event.remove(db.get_bind(),"before_cursor_execute",count)
    assert [run["steps"][0]["summary"] for run in data["runs"]] == [str(i) for i in range(25,0,-1)]
    assert data["runs"][0]["result"]["summary"] == "25"
    assert sum("from agent_steps" in statement for statement in sql) == 1
    assert sum("from generated_contents" in statement and "generated_contents.id in (" in statement for statement in sql) == 1

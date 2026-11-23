from app.models import AgentChatMessage, AgentChatSession, AgentTask, AgentToolCall, ExperimentCase, GeneratedContent


def test_historical_writes_and_subsequent_canvas_edits_are_distinct(client, db):
    case = client.post("/api/cases", json={"title": "single pass"}).json()["id"]
    other = client.post("/api/cases", json={"title": "another bench"}).json()["id"]
    chat = AgentChatSession(case_id=case)
    task = AgentTask(case_id=case, mode="workbench", goal="build", status="failed")
    db.add_all([chat, task])
    db.flush()
    db.get(ExperimentCase, case).parameters = {"workbench_session_id": chat.id}
    db.add(AgentChatMessage(session_id=chat.id, role="assistant", content="A partial result",
                            metadata_json={"workbench_run_id": task.id}))

    def snapshot(label, source=None, *, key=None, owner=case, saved=False):
        row = GeneratedContent(case_id=owner, content_type="optical_snapshot", operation_key=key,
                               content={"source_revision_id": source, "variant_name": label, "saved_variant": saved})
        db.add(row)
        db.flush()
        return row.id

    def record(target, tool="save_optical_project", status="completed"):
        db.add(AgentToolCall(task_id=task.id, tool_name=tool, status=status, output_json={"revision_id": target}))

    first = snapshot("lens")
    root = snapshot("lens", first)
    record(first)
    record(root)
    record(snapshot("external", owner=other))
    record(snapshot("only read"), tool="read_optical_project")
    record(snapshot("failed"), status="error")
    draft = snapshot("lens", root)
    left = snapshot("lens", draft, key="variant:historical")
    snapshot("lens", left, key="width_draft_v2:measurement")
    right = snapshot("lens", root, saved=True)
    db.commit()

    url = f"/api/cases/{case}/workbench"
    response = client.get(url, params={"include_latest_tools": False})
    assert response.status_code == 200, response.text
    model = {"kind": "draft", "revision_id": root, "variant_name": "lens"}
    run = response.json()["runs"][0]
    assert run["editable_models"] == [model]
    assert [r["revision_id"] for r in run["canvas_models"]] == [left, right]
    metadata = response.json()["messages"][0]["metadata"]
    assert metadata["editable_models"] == [model] and "canvas_models" not in metadata
    assert client.get(f"{url}/runs/{task.id}").json()["canvas_models"] == run["canvas_models"]

    # The normal project URL must keep opening the design beyond one task-history page.
    db.add_all(AgentTask(case_id=case, mode="workbench", status="completed", goal="read")
               for _ in range(25))
    db.commit()
    archived = client.get(url, params={"include_latest_tools": False}).json()
    assert all("editable_models" not in recent for recent in archived["runs"])
    assert archived["canvas_models"] == run["canvas_models"]

    independent = snapshot("mirrors", saved=True)
    db.commit()
    all_choices = client.get(url, params={"include_latest_tools": False}).json()["canvas_models"]
    assert [ref["revision_id"] for ref in all_choices] == [left, right, independent]

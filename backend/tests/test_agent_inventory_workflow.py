"""Generic evidence-to-inventory mutations, shared with the normal UI API."""
import asyncio
from copy import deepcopy

import pytest
from sqlalchemy.orm import sessionmaker

from app.auth.security import Principal
from app.inventory.crystals import ManualItemCreate
from app.inventory.service import create_item
from app.models import AgentTask, AgentToolCall, Attachment, AuditLog, ExperimentCase, GeneratedContent, InventoryItem, User
from app.workbench import service, tools
from app.workbench.contracts import CreateInventoryItem, UpdateInventoryItem


@pytest.fixture
def inventory_task(db):
    case = ExperimentCase(title="通用实验", description="已有透镜与腔镜", cavity_type="custom", goal="记录实物", parameters={"unchanged": True})
    db.add(case)
    db.flush()
    task = AgentTask(case_id=case.id, mode="workbench", status="running",
                     goal="这是我的新透镜，请录入元件库；把旧镜的曲率改为R200；位置改为平台A。")
    db.add(task)
    db.flush()
    db.add(AgentToolCall(task_id=task.id, tool_name="workbench_request", input_json={
        "principal": {"actor":"local-experimenter", "role":"user", "user_id":None}}))
    db.commit()
    return case, task


def run_tool(db, pair, name, values):
    case, task = pair
    args = tools.REGISTRY[name][0].model_validate(values)
    return asyncio.run(tools.execute(name, args, db=db, case=case, task=task, provider=None,
                                    session_id=None, attachment_ids=[]))


def create_args(**changes):
    return {"source_quote":"这是我的新透镜，请录入元件库", "stock_confirmed":True,
            "item":{"name":"现有准直透镜", "category":"lens", "focal_length_mm":100,
                    "diameter_mm":25, "quantity":1, "raw_spec":"焦距疑似100mm"}, **changes}


def test_agent_creates_generic_lens_with_evidence_not_assumed_focal_length(db, inventory_task):
    case, task = inventory_task
    attachment = Attachment(case_id=case.id, filename="lens.jpg", filepath="unused", file_type="image/jpeg")
    db.add(attachment)
    db.flush()
    result = run_tool(db, inventory_task, "create_inventory_item", create_args(
        source_attachment_ids=[attachment.id], unverified_fields=["focal_length_mm"],
        uncertainty_notes=["标签焦距遮挡，100mm仅为猜测，需测焦确认"]))
    db.commit()
    item = db.get(InventoryItem, result["inventory_id"])
    assert item.category == "lens" and item.quantity == 1
    assert item.focal_length_mm is None and item.diameter_mm == 25
    assert item.raw_spec == "焦距疑似100mm"
    assert item.parse_confidence == "partial"
    assert any("focal_length_mm" in note for note in item.parse_notes)
    audit = db.query(AuditLog).filter_by(action="inventory.create").one()
    assert audit.actor == "local-experimenter"
    assert audit.metadata_json["source_attachment_ids"] == [attachment.id]
    assert audit.metadata_json["task_id"] == task.id
    assert result["installed_hardware_changed"] is False
    assert case.parameters == {"unchanged": True}


def test_unconfirmed_new_record_is_not_available_physical_stock(db, inventory_task):
    result = run_tool(db, inventory_task, "create_inventory_item", create_args(stock_confirmed=False))
    assert result["item"]["available_quantity"] == 0
    item = db.get(InventoryItem, result["inventory_id"])
    assert item.quantity == 0 and any("实物归属" in note for note in item.parse_notes)


def test_uncertain_derived_crystal_doping_is_not_reintroduced_from_segments(db, inventory_task):
    item = create_item(db, ManualItemCreate(name="新复合晶体", category="gain_crystal", crystal_spec={
        "width_mm":3, "height_mm":3, "segments":[
            {"material":"Nd:YAG", "role":"gain", "length_mm":10, "doping_pct":1},
            {"material":"YAG", "role":"undoped", "length_mm":4}]}), Principal(actor="local", role="user"))
    db.commit()
    result = run_tool(db, inventory_task, "update_inventory_item", {
        "inventory_id":item.id, "source_quote":"位置改为平台A", "patch":{"location":"平台A"},
        "unverified_fields":["doping_pct"], "uncertainty_notes":["1%读数待核验"]})
    assert result["item"]["doping_pct"] is None
    assert result["item"]["crystal_spec"]["segments"][0]["doping_pct"] is None
    assert result["item"]["crystal_spec"]["segments"][0]["uncertain"] is True
    assert result["item"]["thickness_mm"] == 14


def test_withdrawing_material_preserves_independent_lengths_roles_and_original_label(db, inventory_task):
    initial = create_args(item={"name":"doublet","category":"gain_crystal","raw_spec":"pieces measured before joining",
        "crystal_spec":{"segments":[{"material":"Nd:YAG","role":"gain","length_mm":5},
                                     {"material":"YAG","role":"undoped","length_mm":4}]}})
    original = run_tool(db, inventory_task, "create_inventory_item", initial)
    update = {"inventory_id":original["inventory_id"],"source_quote":"位置改为平台A","patch":{"location":"平台A"},"unverified_fields":["material"]}
    item = run_tool(db, inventory_task, "update_inventory_item", update)["item"]
    assert item["material"] is None and item["thickness_mm"] == 9
    assert [s["material"] for s in item["crystal_spec"]["segments"]] == [None,None]
    assert [s["role"] for s in item["crystal_spec"]["segments"]] == ["gain","undoped"]
    assert [s["length_mm"] for s in item["crystal_spec"]["segments"]] == [5,4]
    assert item["raw_spec"] == "pieces measured before joining"


def test_withdrawing_case_inscription_does_not_clear_separately_identified_sections(db, inventory_task):
    initial = create_args(item={"name":"encapsulated crystal","category":"gain_crystal",
        "crystal_spec":{"material_label":"crate 21","segments":[{"material":"Nd:YAG","role":"gain","length_mm":5}]}})
    original = run_tool(db, inventory_task, "create_inventory_item", initial)
    update = {"inventory_id":original["inventory_id"],"source_quote":"位置改为平台A","patch":{"location":"平台A"},"unverified_fields":["material"]}
    item = run_tool(db, inventory_task, "update_inventory_item", update)["item"]
    assert not item["crystal_spec"].get("material_label")
    assert item["crystal_spec"]["segments"][0]["material"] == "Nd:YAG"
    assert item["material"] == "Nd:YAG"


@pytest.mark.parametrize("category", ["mirror", "window", "nonlinear_crystal", "other"])
def test_create_is_not_a_gain_crystal_only_tool(db, inventory_task, category):
    result = run_tool(db, inventory_task, "create_inventory_item", create_args(
        item={"name":"已有元件", "category":category}))
    assert result["item"]["category"] == category


def test_update_shared_service_preserves_coatings_and_historical_project(db, client, inventory_task):
    case, _ = inventory_task
    original = client.post("/api/inventory/items", json={"name":"旧平镜", "category":"mirror", "roc_is_flat":True,
        "coatings":[{"surface":"S1", "wl_min_nm":1064, "wl_max_nm":1064, "function":"PR",
                     "value_type":"T", "comparator":"=", "value_pct":5}]}).json()
    frozen = {"project":{"parts":[{"inventory_id":original["id"]}], "source":"原平镜"}}
    revision = GeneratedContent(case_id=case.id, content_type="optical_project", content=deepcopy(frozen))
    db.add(revision)
    db.commit()
    result = run_tool(db, inventory_task, "update_inventory_item", {
        "inventory_id":original["id"], "source_quote":"把旧镜的曲率改为R200", "patch":{"roc_mm":200}})
    db.commit()
    item = db.get(InventoryItem, original["id"])
    assert item.roc_mm == 200 and item.roc_is_flat is False
    assert len(item.coatings) == 1 and item.coatings[0].value_pct == 5
    assert item.quantity == 1  # unrelated updates do not erase known stock
    assert db.get(GeneratedContent, revision.id).content == frozen
    assert case.parameters == {"unchanged": True}
    assert result["installed_hardware_changed"] is False
    # Ordinary UI editing uses the same model merge, including ROC/flat semantics.
    response = client.patch(f"/api/inventory/items/{item.id}", json={"roc_is_flat":True})
    assert response.status_code == 200 and response.json()["roc_mm"] is None
    assert len(response.json()["coatings"]) == 1


@pytest.mark.parametrize("change,error", [
    ({"source_quote":"模型建议新增一片透镜"}, "本轮用户原话"),
    ({"source_attachment_ids":[999999]}, "不属于当前案例"),
    ({"unverified_fields":["nonexistent"]}, "待核验字段"),
])
def test_invalid_evidence_never_creates_inventory(db, inventory_task, change, error):
    with pytest.raises(ValueError, match=error):
        run_tool(db, inventory_task, "create_inventory_item", create_args(**change))
    assert db.query(InventoryItem).count() == 0
    assert db.query(AuditLog).count() == 0


def test_foreign_case_attachment_cannot_be_used_as_provenance(db, inventory_task):
    other = ExperimentCase(title="其他案例", cavity_type="custom", goal="记录实物")
    db.add(other)
    db.flush()
    attachment = Attachment(case_id=other.id, filename="other.png", filepath="unused", file_type="image/png")
    db.add(attachment)
    db.flush()
    with pytest.raises(ValueError, match="不属于当前案例"):
        run_tool(db, inventory_task, "create_inventory_item", create_args(source_attachment_ids=[attachment.id]))
    assert db.query(InventoryItem).count() == 0


@pytest.mark.parametrize("role,active,allowed", [("user",True,False), ("reviewer",True,True), ("admin",False,False)])
def test_agent_honors_live_user_permission_instead_of_a_stale_snapshot(db, inventory_task, role, active, allowed):
    _, task = inventory_task
    user = User(email="experimenter@example.test", display_name="实验员", role=role, is_active=active)
    db.add(user)
    db.flush()
    task.user_id = user.id
    request = db.query(AgentToolCall).filter_by(task_id=task.id, tool_name="workbench_request").one()
    request.input_json = {"principal":{"actor":"forged", "role":"admin", "user_id":user.id}}
    db.commit()
    if allowed:
        result = run_tool(db, inventory_task, "create_inventory_item", create_args())
        db.flush()
        assert result["inventory_id"]
        assert db.query(AuditLog).one().actor == user.email
    else:
        with pytest.raises(ValueError):
            run_tool(db, inventory_task, "create_inventory_item", create_args())
        assert db.query(InventoryItem).count() == 0


def test_cancelled_task_cannot_write_inventory(db, inventory_task):
    inventory_task[1].status = "cancelled"
    db.commit()
    with pytest.raises(ValueError, match="任务已停止"):
        run_tool(db, inventory_task, "create_inventory_item", create_args())
    assert db.query(InventoryItem).count() == 0


def test_tool_discovery_accepts_unclassified_items_and_hides_legacy_alias():
    schemas = {tool["name"]:tool for tool in tools.schemas()}
    assert "create_inventory_item" in schemas and "update_inventory_item" in schemas
    assert "replace_crystal" not in schemas and "replace_crystal" in tools.REGISTRY
    item_schema = CreateInventoryItem.model_json_schema()["$defs"]["AgentInventoryItem"]
    assert "category" not in item_schema["required"] and item_schema["properties"]["category"]["default"] == "other"
    assert "inventory_id" in UpdateInventoryItem.model_json_schema()["required"]


def test_unclassified_item_has_the_same_identity_in_direct_and_agent_creation(client, db, inventory_task):
    spec = {"segments":[{"role":"undoped","material":"fused_silica","length_mm":2}]}
    item = {"name":"labelled sample","crystal_spec":spec}
    direct = client.post("/api/inventory/items",json=item)
    assert direct.status_code == 201, direct.text
    via_agent = run_tool(db, inventory_task, "create_inventory_item", create_args(item=item))["item"]
    assert direct.json()["category"] == via_agent["category"] == "other"
    assert direct.json()["crystal_spec"] == via_agent["crystal_spec"]
    assert direct.json()["thickness_mm"] == via_agent["thickness_mm"] == 2
    for obj in [direct.json(),via_agent]:
        assert obj["crystal_spec"]["segments"][0]["role"] == "undoped"


def test_domain_service_keeps_existing_inventory_permission_rule(db):
    with pytest.raises(ValueError, match="权限"):
        create_item(db, ManualItemCreate(name="窗口", category="window"), Principal(actor="viewer", role="user", user_id=1))


def test_normal_agent_loop_can_create_then_correct_a_lens(db, client, monkeypatch):
    import json
    monkeypatch.setattr(service, "SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    case = ExperimentCase(title="光束整形", description="登记已有透镜", cavity_type="custom", goal="记录实物")
    db.add(case)
    db.commit()

    class Provider:
        turns = 0

        async def agent_turn(self, instructions, messages, schemas):
            self.turns += 1
            outputs = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            if self.turns == 1:
                name, args = "create_inventory_item", {"source_quote":"登记我的透镜", "stock_confirmed":True,
                                                       "item":{"name":"现有透镜", "category":"lens", "focal_length_mm":100}}
            elif self.turns == 2:
                name, args = "update_inventory_item", {"source_quote":"位置在平台A", "inventory_id":outputs[-1]["result"]["inventory_id"], "patch":{"location":"平台A"}}
            else:
                name, args = "finish", {"status":"completed", "summary":"已登记透镜并补充存放位置，工程未变更。",
                                         "evidence_tool_call_ids":[o["tool_call_id"] for o in outputs]}
            return {"items":[], "calls":[{"id":str(self.turns), "name":name, "arguments":json.dumps(args)}]}

    monkeypatch.setattr(service, "get_ai_provider", lambda:Provider())
    service.ACTIVE_RUNS.clear()
    response = client.post(f"/api/cases/{case.id}/workbench/runs", json={"message":"登记我的透镜，位置在平台A"})
    assert response.status_code == 202
    state = client.get(f"/api/cases/{case.id}/workbench").json()
    run = state["runs"][0]
    assert run["status"] == "completed"
    assert [c["name"] for c in run["tools"]] == ["create_inventory_item", "update_inventory_item", "finish"]
    assert all(c["status"] == "completed" for c in run["tools"])
    item = db.query(InventoryItem).one()
    assert item.category == "lens" and item.location == "平台A" and item.focal_length_mm == 100
    assert db.query(AuditLog).filter(AuditLog.action.in_(["inventory.create", "inventory.update"])).count() == 2
    assert not db.query(GeneratedContent).filter_by(content_type="optical_project").count()

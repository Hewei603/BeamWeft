"""
实验案例API测试
"""


def test_create_case(client):
    """测试创建案例"""
    case_data = {
        "title": "测试案例",
        "description": "这是一个测试案例",
        "cavity_type": "linear",
        "goal": "测试目标",
        "parameters": {"波长": "1064nm"},
        "symptoms": ["无输出"]
    }

    response = client.post("/api/cases", json=case_data)
    assert response.status_code == 201
    data = response.json()
    assert data["title"] == case_data["title"]
    assert data["cavity_type"] == case_data["cavity_type"]
    assert "id" in data


def test_list_cases(client):
    """测试获取案例列表"""
    # 创建几个案例
    for i in range(3):
        client.post("/api/cases", json={
            "title": f"案例 {i}",
            "cavity_type": "linear",
            "goal": f"目标 {i}"
        })

    response = client.get("/api/cases")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3


def test_library_pages_search_notes_and_only_count_saved_evidence(client, db):
    from app.models import Attachment, GeneratedContent
    path = "/api/cases/library"
    ids = [client.post("/api/cases", json={"title": t, "description": note}).json()["id"]
           for t, note in [("A", "cal 80%_a"), ("B", "cal 80xxa"), ("C", "CAL 80%_A")]]
    for kind in ["optical_project", "scientific_study", "optical_snapshot"]:
        db.add(GeneratedContent(case_id=ids[0], content_type=kind, content={"private_payload": "large model"}))
    db.add(Attachment(case_id=ids[0], filename="trace", filepath="unused"))
    db.commit()
    first = client.get(path, params={"q": "80%_a", "limit": 1}).json()
    assert [i["id"] for i in first["items"]] == [ids[2]]
    client.post("/api/cases", json={"title":"New CAL 80%_A"})
    second = client.get(path, params={"q": "80%_a", "before": first["next_before"], "limit":1}).json()
    assert second["next_before"] is None
    item, = second["items"]
    revision = db.query(GeneratedContent).filter_by(content_type="optical_project", case_id=ids[0]).one()
    assert (item["id"], item["revision_id"], item["file_count"], item["study_count"]) == (ids[0], revision.id, 1, 1)
    assert "parameters" not in item and "private_payload" not in str(item)
    assert client.get(path, params={"limit":0}).status_code == 422


def test_directory_keeps_the_local_file_view_separate_from_the_project_head(client, db, monkeypatch):
    from app.models import GeneratedContent

    pinned = client.post("/api/cases", json={"title": "只有独立方案"}).json()["id"]
    other = client.post("/api/cases", json={"title": "另一个空白工程"}).json()["id"]
    model = GeneratedContent(case_id=pinned, content_type="optical_snapshot", content={"project": {}})
    db.add(model)
    db.commit()
    marker = {"case_id": pinned, "opening_revision_id": model.id}
    monkeypatch.setattr(client.app.state, "opened_case", marker, raising=False)
    def library():
        return {item["id"]: item for item in client.get("/api/cases/library").json()["items"]}
    rows = library()
    assert rows[pinned]["opening_revision_id"] == model.id and rows[pinned]["revision_id"] is None
    assert "opening_revision_id" not in rows[other]
    marker["opening_revision_id"] = None
    assert "opening_revision_id" not in library()[pinned]


def test_get_case(client):
    """测试获取单个案例"""
    # 创建案例
    create_response = client.post("/api/cases", json={
        "title": "测试案例",
        "cavity_type": "ring",
        "goal": "测试目标"
    })
    case_id = create_response.json()["id"]

    # 获取案例
    response = client.get(f"/api/cases/{case_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == case_id
    assert data["title"] == "测试案例"


def test_get_nonexistent_case(client):
    """测试获取不存在的案例"""
    response = client.get("/api/cases/999")
    assert response.status_code == 404


def test_update_case(client):
    """测试更新案例"""
    # 创建案例
    create_response = client.post("/api/cases", json={
        "title": "原标题",
        "cavity_type": "linear",
        "goal": "原目标"
    })
    case_id = create_response.json()["id"]

    # 更新案例
    update_data = {
        "title": "新标题",
        "goal": "新目标"
    }
    response = client.put(f"/api/cases/{case_id}", json=update_data)
    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "新标题"
    assert data["goal"] == "新目标"


def test_delete_case(client):
    """测试删除案例"""
    # 创建案例
    create_response = client.post("/api/cases", json={
        "title": "待删除案例",
        "cavity_type": "linear",
        "goal": "测试目标"
    })
    case_id = create_response.json()["id"]

    # 删除案例
    response = client.delete(f"/api/cases/{case_id}")
    assert response.status_code == 204

    # 验证已删除
    get_response = client.get(f"/api/cases/{case_id}")
    assert get_response.status_code == 404


def test_open_document_protects_its_own_case_in_detail_and_library(client, monkeypatch):
    document_id = client.post("/api/cases", json={"title": "Opened document"}).json()["id"]
    other_id = client.post("/api/cases", json={"title": "Other project"}).json()["id"]
    monkeypatch.setattr(client.app.state, "opened_case", {"case_id": document_id, "recovered_from": "previous"}, raising=False)
    detail = client.get(f"/api/cases/{document_id}").json()
    assert detail["is_open_document"] and detail["recovered_from"] == "previous"
    assert client.get(f"/api/cases/{other_id}").json()["recovered_from"] is None
    items = {item["id"]: item for item in client.get("/api/cases/library").json()["items"]}
    assert items[document_id]["is_open_document"] and not items[other_id].get("is_open_document")
    assert client.delete(f"/api/cases/{document_id}").status_code == 409
    assert client.get(f"/api/cases/{document_id}").status_code == 200
    assert client.delete(f"/api/cases/{other_id}").status_code == 204


def test_invalid_cavity_type(client):
    """测试无效的腔型"""
    response = client.post("/api/cases", json={
        "title": "测试",
        "cavity_type": "invalid",
        "goal": "测试"
    })
    assert response.status_code == 422
def test_project_details_support_independent_edits_conflict_and_clearing(client):
    case = client.post("/api/cases", json={"title": "透镜台"}).json()
    url = f"/api/cases/{case['id']}/details"
    assert case["goal"] == "" and case["description"] is None
    def edit(changes, expected):
        return client.patch(url, json={"changes": changes, "expected": expected})
    assert edit({"goal":"比较重复性"},{"goal":""}).status_code == 200
    assert edit({"description":"实验笔记"},{"description":None}).json()["goal"] == "比较重复性"
    assert edit({"goal":"旧窗口覆盖"},{"goal":""}).status_code == 409
    assert edit({"goal":""},{"goal":"比较重复性"}).json()["description"] == "实验笔记"
    assert edit({"title":"   "},{"title":"透镜台"}).status_code == 422
    assert edit({"owner_id":"3"},{"owner_id":"1"}).status_code == 422

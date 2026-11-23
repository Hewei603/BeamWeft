from copy import deepcopy

from sqlalchemy import event

from app.models import GeneratedContent
from app.workbench.model_comparison import CompareProjectInputs, _changes, compare_project_inputs
from app.workbench.projects import read_project, read_project_inputs
from app.workbench.study_inputs import basis


def test_saved_and_ephemeral_inputs_compare_real_objects_without_claiming_legacy_identity(client, db):
    root = "/api/cases/" + str(client.post("/api/cases",json={"title":"对照"}).json()["id"]) + "/optical-project"
    project = {"title":"基准","system":{"mode":"single_pass","wavelength_nm":532,"input_radius_mm":.2,
        "elements":[{"uid":"e1","kind":"space","name":"空气","length_mm":10,"n":1},
                    {"uid":"e2","kind":"lens","name":"镜","focal_mm":65}]},
        "parts":[{"element_index":1,"source":"标签"}],"assumptions":["标称值"]}
    first = client.post(root,json={"project":project,"change_note":"登记"}).json()
    draft = deepcopy(first["project"])
    rows = draft["system"]["elements"]
    rows.reverse()
    rows[0]["focal_mm"] = None
    rows.insert(1,{"uid":"e3","name":"间隔","kind":"space","length_mm":8,"n":1})
    draft["parts"][0]["element_index"] = 0
    draft["assumptions"] = ["待测"]
    saved = client.post(root+"/variants",json={"project":draft,"source_revision_id":first["revision_id"],
        "base_revision_id":first["revision_id"],"variant_name":"备选"}).json()
    url = root+"/input-comparison"
    request = {"reference_revision_id":first["revision_id"],"revision_id":saved["revision_id"]}
    stored_records = db.query(GeneratedContent).count()
    saved_result = client.post(url,json=request).json()
    assert saved_result["total"] == 4 and saved_result["target"]["variant_name"]=="备选"
    page = client.post(url,json={**request,"limit":2}).json()
    rest = client.post(url,json={**request,"offset":page["next_offset"]}).json()
    assert page["items"] + rest["items"] == saved_result["items"]
    case_id = int(root.split("/")[3])
    statements = []
    loaded = []
    def record_query(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    def record_hydration(session, record):
        loaded.append(type(record))
    engine = db.get_bind()
    db.expunge_all()
    event.listen(engine, "before_cursor_execute", record_query)
    event.listen(db, "loaded_as_persistent", record_hydration)
    try:
        inspection = compare_project_inputs(db, case_id, CompareProjectInputs(**request))
    finally:
        event.remove(engine, "before_cursor_execute", record_query)
        event.remove(db, "loaded_as_persistent", record_hydration)
    assert inspection["total"] == 4 and len(statements) == 1
    assert GeneratedContent not in loaded
    inputs = read_project_inputs(db, case_id, request.values(), summary=True)
    for revision_id, record in inputs.items():
        assert basis(record) == basis(read_project(db, case_id, revision_id))
    result = client.post(url,json={"reference_revision_id":first["revision_id"],
        "working_draft":{"project":draft,"source_revision_id":first["revision_id"]}}).json()
    assert result["target"]["kind"] == "canvas" and result["items"]==saved_result["items"]
    by_kind = {row["kind"]:row for row in result["items"]}
    assert by_kind["added"]["path"][-1]["after_position"] == 2
    assert [item["uid"] for item in by_kind["reordered"]["after"]]==["e2","e1"]
    focal = next(row for row in result["items"] if row["path"][-1]=="focal_mm")
    assert focal["path"][-2]["uid"] == "e2" and focal["before"] == 65 and focal["after"] is None
    assert "焦距" in focal["definition"]
    other = "/api/cases/" + str(client.post("/api/cases",json={"title":"独立"}).json()["id"]) + "/optical-project"
    assert client.post(other+"/input-comparison",json=request).status_code == 422
    assert db.query(GeneratedContent).count() == stored_records
    assert client.get(root).json()["current"]["revision_id"]==first["revision_id"]
    # Without persistent IDs neither an array index nor a display name proves identity.
    changes = list(_changes([{"name":"X","p":0}],[{"name":"X","p":False}]))
    assert len(changes)==1 and changes[0]["path"]==() and changes[0]["kind"]=="changed"

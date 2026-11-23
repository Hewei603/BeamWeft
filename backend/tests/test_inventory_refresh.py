"""Actual XLSX round trips must retain physical-item state and history."""
from io import BytesIO

from openpyxl import Workbook


def _upload(client, rows, filename="refresh.xlsx"):
    workbook = Workbook()
    workbook.active.append(["id", "name", "S1", "S2", "size", "ROC", "quantity", "location", "keeper", "vendor"])
    for index, (name, roc, quantity, location) in enumerate(rows, 1):
        workbook.active.append([index, name, "1064HR", "", "D=25.4mm", roc, quantity, location, "", ""])
    data = BytesIO()
    workbook.save(data)
    workbook.close()
    return client.post("/api/inventory/import", files={"file": (filename, data.getvalue(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})


def _items(client):
    return client.get("/api/inventory/items").json()


def test_manual_edit_round_trips_imported_unclassified_crystal_and_surface_spec(client):
    imported = _upload(client, [("待鉴定晶体", "", 1, "rack C")])
    assert imported.status_code == 200, imported.text
    item, = _items(client)
    assert item["category"] == "crystal_other"
    response = client.patch(f"/api/inventory/items/{item['id']}",json={"vendor":"maker C","cut_angle_phi_deg":251.25})
    assert response.status_code == 200, response.text
    corrected = response.json()
    assert corrected["category"] == "crystal_other" and corrected["cut_angle_phi_deg"] == 251.25
    assert corrected["diameter_mm"] == item["diameter_mm"]
    assert corrected["raw_spec"] == item["raw_spec"]


def _damage_with_history(client, item):
    loan = client.post(f"/api/inventory/items/{item['id']}/borrow",
                       json={"borrower_name": "Test borrower", "quantity": 1}).json()
    response = client.post(f"/api/inventory/loans/{loan['id']}/return",
                           json={"condition_on_return": "damaged", "note": "Recorded chip"})
    assert response.status_code == 200, response.text
    return loan["id"]


def test_reimport_preserves_ids_damage_and_history_after_sorting(client):
    assert _upload(client, [("Mirror A", "R=500mm", 1, "Shelf A"), ("Mirror B", "R=200mm", 2, "Shelf B")]).status_code == 200
    old = {item["name"]: item for item in _items(client)}
    loan_id = _damage_with_history(client, old["Mirror A"])
    response = _upload(client, [("Mirror B", "R=200mm", 4, "New shelf"), ("Mirror A", "R=500mm", 1, "Shelf A")])
    assert response.status_code == 200, response.text
    new = {item["name"]: item for item in _items(client)}
    assert len(new) == 2
    assert new["Mirror A"]["id"] == old["Mirror A"]["id"]
    assert new["Mirror B"]["id"] == old["Mirror B"]["id"]
    assert new["Mirror A"]["effective_condition"] == "damaged"
    assert new["Mirror A"]["condition_note"] == "Recorded chip"
    assert new["Mirror B"]["quantity"] == 4
    assert new["Mirror B"]["location"] == "New shelf"
    assert len(new["Mirror A"]["coatings"]) == 1
    history = client.get("/api/inventory/loans?open_only=false").json()
    assert [loan["id"] for loan in history] == [loan_id]
    refused = client.post(f"/api/inventory/items/{old['Mirror A']['id']}/borrow",
                          json={"borrower_name": "Next borrower", "quantity": 1})
    assert refused.status_code == 409


def test_removed_item_history_is_retained_with_zero_stock_and_restored_on_reappearance(client):
    _upload(client, [("Mirror A", "R=500mm", 1, "A"), ("Mirror B", "R=200mm", 1, "B")])
    item = next(item for item in _items(client) if item["name"] == "Mirror A")
    loan_id = _damage_with_history(client, item)
    assert _upload(client, [("Mirror B", "R=200mm", 1, "B")]).status_code == 200
    retired = next(row for row in _items(client) if row["id"] == item["id"])
    assert retired["quantity"] == 0
    assert retired["effective_condition"] == "damaged"
    assert client.get("/api/inventory/loans?open_only=false").json()[0]["id"] == loan_id
    assert _upload(client, [("Mirror A", "R=500mm", 1, "A"), ("Mirror B", "R=200mm", 1, "B")]).status_code == 200
    restored = next(row for row in _items(client) if row["id"] == item["id"])
    assert restored["quantity"] == 1 and restored["effective_condition"] == "damaged"


def test_spec_edit_with_human_history_is_atomic_conflict(client):
    _upload(client, [("Mirror A", "R=500mm", 1, "A")])
    item = _items(client)[0]
    _damage_with_history(client, item)
    response = _upload(client, [("Mirror A", "R=200mm", 5, "B")])
    assert response.status_code == 409, response.text
    current = _items(client)
    assert len(current) == 1
    assert current[0]["id"] == item["id"] and current[0]["roc_mm"] == 500
    assert current[0]["effective_condition"] == "damaged"


def test_identical_duplicate_rows_do_not_transfer_a_damage_verdict(client):
    rows = [("Mirror A", "R=500mm", 1, "A"), ("Mirror A", "R=500mm", 1, "A")]
    _upload(client, rows)
    item = _items(client)[0]
    _damage_with_history(client, item)
    response = _upload(client, rows)
    assert response.status_code == 409, response.text
    assert len(_items(client)) == 2


def test_duplicate_specs_with_distinct_locations_keep_their_own_history(client):
    rows = [("Mirror A", "R=500mm", 1, "Shelf A"), ("Mirror A", "R=500mm", 1, "Shelf B")]
    _upload(client, rows)
    old = {item["location"]: item for item in _items(client)}
    _damage_with_history(client, old["Shelf A"])
    response = _upload(client, list(reversed(rows)))
    assert response.status_code == 200, response.text
    new = {item["location"]: item for item in _items(client)}
    assert new["Shelf A"]["id"] == old["Shelf A"]["id"]
    assert new["Shelf A"]["effective_condition"] == "damaged"
    assert new["Shelf B"]["id"] == old["Shelf B"]["id"]
    assert new["Shelf B"]["effective_condition"] == "ok"


def test_invalid_workbook_does_not_change_existing_inventory(client):
    _upload(client, [("Mirror A", "R=500mm", 1, "A")])
    item = _items(client)[0]
    _damage_with_history(client, item)
    before = _items(client)
    response = client.post("/api/inventory/import", files={"file": ("refresh.xlsx", b"invalid workbook", "application/octet-stream")})
    assert response.status_code == 400
    assert _items(client) == before
    assert len(client.get("/api/inventory/loans?open_only=false").json()) == 1


def test_refresh_preserves_superseded_main_and_variant_only_arm_bindings(client, db):
    from copy import deepcopy

    from app.models import ExperimentCase
    from app.workbench.projects import SaveProject, read_project, save_project
    from app.workbench.study_inputs import WorkingDraft, capture_snapshot

    rows = [("Mirror A", "R=200mm", 1, "shelf A"), ("Mirror B", "R=100mm", 1, "shelf B"),
            ("Window", "", 8, "shelf W")]
    assert _upload(client, rows).status_code == 200
    original = {item["name"]: item for item in _items(client)}
    case = ExperimentCase(title="Stock record history", goal="Track independent hardware", cavity_type="other", parameters={})
    db.add(case)
    db.commit()
    model = {"title":"folded relay","system":{"mode":"single_pass","wavelength_nm":633,"elements":[
        {"kind":"space","name":"lead in","uid":"gap","length_mm":20},
        {"kind":"mirror","name":"turn","uid":"turn","radius_mm":200}]},
        "parts":[{"element_index":1,"inventory_id":original["Mirror A"]["id"],"source":"stockroom tag"}]}
    older = save_project(db,case.id,SaveProject(project=model,change_note="installed A"))
    model["parts"] = []
    current = save_project(db,case.id,SaveProject(project=model,base_revision_id=older["revision_id"],change_note="removed mirror"))
    model["incident_paths"] = [{"uid":"alignment","name":"alignment arm","target_uid":"gap","target_n":1,"wavelength_nm":633,
        "tangential":{"radius_mm":0.2,"wavefront":"waist","m2":1},
        "elements":[{"kind":"mirror","uid":"fold","name":"turn B","radius_mm":100}],
        "parts":[{"element_index":0,"inventory_id":original["Mirror B"]["id"],"source":"read on back of optic"}]}]
    variant = capture_snapshot(db,case.id,WorkingDraft(project=model,source_revision_id=current["revision_id"],
        base_revision_id=current["revision_id"]),variant_name="alignment arm")
    db.commit()
    snapshots = {r["revision_id"]:deepcopy(read_project(db,case.id,r["revision_id"])) for r in (older,current,variant)}

    # A logistics row would be modified in the same import, before the ambiguous
    # row is noticed. A failure must roll back both that row and newly staged ones.
    for changed in ("Mirror A","Mirror B"):
        edited = [("Window","",3,"new shelf"),("New Lens", "", 1, "N")]
        edited.extend((name,"R=400mm" if name==changed else roc,qty,location) for name,roc,qty,location in rows[:2])
        response = _upload(client,edited)
        assert response.status_code == 409 and f"#{original[changed]['id']}" in response.text
        assert {i["name"]:i for i in _items(client)} == original
    # Actually removing the rows retires the stock, without deleting a version's source.
    assert _upload(client,[rows[-1]]).status_code == 200
    after = {i["name"]:i for i in _items(client)}
    for name in ("Mirror A","Mirror B"):
        assert after[name]["id"] == original[name]["id"] and after[name]["quantity"] == 0
    for revision, snapshot in snapshots.items():
        assert read_project(db,case.id,revision) == snapshot
    assert _upload(client,list(reversed(rows))).status_code == 200
    restored = {i["name"]:i for i in _items(client)}
    assert all((row["id"], row["quantity"]) == (original[name]["id"], original[name]["quantity"]) for name,row in restored.items())


def test_exact_workbook_refresh_keeps_curated_sections_and_their_source_notes(client):
    rows = [("Nd:YAG specimen", "", 2, "Shelf A")]
    assert _upload(client,rows).status_code == 200
    item, = _items(client)
    url = f"/api/inventory/items/{item['id']}"
    spec = {"overall_length_mm":9, "material_label":"specimen certificate",
            "segments":[{"length_mm":4, "material":"Nd:YAG","role":"gain","doping_pct":0.7},
                        {"length_mm":5, "note":"Visible joint; unidentified substrate", "uncertain":True}]}
    saved = client.patch(url,json={"crystal_spec":spec,"dimensions":"9 mm body, clear opening 2.2 mm"}).json()
    assert saved["crystal_spec"]["segments"][0]["doping_pct"] == 0.7
    assert _upload(client,[("Nd:YAG specimen", "", 4, "Shelf D")]).status_code == 200
    updated = client.get(url).json()
    for key in ("crystal_spec","material","dimensions","thickness_mm","doping_pct"):
        assert updated[key] == saved[key]
    assert set(saved["parse_notes"]).issubset(updated["parse_notes"])
    assert updated["location"] == "Shelf D" and updated["quantity"] == 4

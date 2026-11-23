import hashlib
import io
import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from app.api import attachments
from app.auth.security import Principal
from app.case_package import HISTORY, VIEW, PackageError, write_package
from app.document_destinations import Destination
from app.models import (
    AgentChatMessage, AgentChatSession, AgentTask, AgentToolCall, Attachment, CaseComponentItem, CaseModule,
    CaseModuleFile, CoatingSpec, ExperimentCase, GeneratedContent, InventoryItem, KnowledgeSource,
)
from app.open_case import open_package
from app.native_document import NativeDocument, fingerprint
from app.workbench.measurement_view import InspectMeasurement, inspect_measurement
from app.workbench.projects import OpticalProject, evaluate_project


def test_native_file_retains_identity_across_saves_and_detects_disk_changes(client, tmp_path):
    case = client.post("/api/cases", json={"title":"读数: 复核"}).json()
    name = f"/api/cases/{case['id']}"
    assert case["goal"] == ""
    first = client.get(name + "/project-file")
    assert first.status_code == 200 and ".lclaw" in first.headers["content-disposition"]
    source = tmp_path / "shared.lclaw"
    source.write_bytes(first.content)
    root = tmp_path / "workspaces"
    def open_native(path):
        document = NativeDocument(path, root)
        with document.locked() as registry:
            folder = document.workspace(registry, fingerprint(path))
            if not folder.exists():
                open_package(path, folder.parent)
        return document
    document = open_native(source)
    peer_file = tmp_path / "copy.lclaw"
    peer_file.write_bytes(source.read_bytes())
    assert open_native(peer_file).folder != document.folder
    client.put(name, json={"description":"补录"})
    second = client.get(name + "/project-file").content
    same_file_selection = Destination(source, fingerprint(source))
    document.save(lambda path: path.write_bytes(second))
    with pytest.raises(ValueError, match="选择的位置已被其他程序改动"):
        document.save(lambda path: path.write_bytes(first.content), selection=same_file_selection)
    assert source.read_bytes() == second
    assert open_native(source).folder == document.folder
    def broken_write(target):
        target.write_bytes(b"incomplete")
        raise OSError("disk full")
    with pytest.raises(OSError):
        document.save(broken_write)
    assert source.read_bytes() == second
    source.write_bytes(first.content)
    with pytest.raises(ValueError, match="已移动或已被修改"):
        document.save(lambda path: path.write_bytes(second))
    assert source.read_bytes() == first.content
    assert open_native(source).folder != document.folder


def test_file_opening_view_is_an_optical_version_in_its_own_snapshot(client, db, tmp_path):
    case_id = client.post("/api/cases", json={"title": "并列方案"}).json()["id"]
    other_id = client.post("/api/cases", json={"title": "独立工程"}).json()["id"]
    url = f"/api/cases/{case_id}/project-file"
    rows = [GeneratedContent(case_id=case, content_type=kind, content={"project": {}})
            for case, kind in [(case_id, "optical_project"), (case_id, "optical_snapshot"),
                               (case_id, "optical_project"), (case_id, "memo"), (other_id, "optical_snapshot")]]
    db.add_all(rows)
    db.commit()
    for revision in rows[:3]:
        response = client.get(url, params={"revision_id": revision.id})
        assert response.status_code == 200
        file = tmp_path / "view.lclaw"
        file.write_bytes(response.content)
        _, marker = open_package(file, tmp_path / "receiver")
        assert marker["opening_revision_id"] == revision.id
    for unrelated in rows[3:]:
        assert client.get(url, params={"revision_id": unrelated.id}).status_code == 409

    old = client.get(url)
    with zipfile.ZipFile(io.BytesIO(old.content)) as archive:
        assert VIEW not in archive.namelist()
    file.write_bytes(old.content)
    _, marker = open_package(file, tmp_path / "receiver")
    assert marker["opening_revision_id"] is None
    with zipfile.ZipFile(file, "a") as archive:
        archive.writestr(VIEW, json.dumps({"revision_id": rows[3].id}))
    with pytest.raises(PackageError, match="起始光路版本缺失"):
        open_package(file, tmp_path / "receiver")


@pytest.mark.parametrize("extension", ["lclaw", "zip"])
def test_reopen_recovers_deleted_root_without_replacing_the_old_workspace(client, tmp_path, archive_case, extension):
    case, url, _, _ = archive_case
    package = tmp_path / f"source.{extension}"
    package.write_bytes(client.get(url + "/bundle").content)
    root = tmp_path / "receiver"

    def reopen():
        if extension == "zip":
            folder, _ = open_package(package, root)
            return folder
        document = NativeDocument(package, root)
        with document.locked() as registry:
            folder = document.workspace(registry, fingerprint(package))
            if not folder.exists():
                open_package(package, folder.parent)
        return folder

    old = reopen()
    if extension == "lclaw":
        document = NativeDocument(package, root)
        with document.locked() as registry:
            assert document.workspace(registry, fingerprint(package)) == old
        client.put(url, json={"description": "Later disk snapshot"})
        updated = client.get(url + "/project-file").content
        document.save(lambda destination: destination.write_bytes(updated))
    before = fingerprint(package)
    with closing(sqlite3.connect(old / "laserclaw.db")) as db, db:
        db.execute("DELETE FROM experiment_cases WHERE id=?", (case,))
    remnant = old / "uploads" / "local-only-note.txt"
    remnant.write_text("Retain this local evidence")

    restored = reopen()
    assert restored != old and reopen() == restored
    assert remnant.read_text() == "Retain this local evidence"
    assert fingerprint(package) == before
    if extension == "lclaw":
        with pytest.raises(ValueError, match="已移动或已被修改"):
            document.save(lambda destination: destination.write_bytes(b"stale window"))
        assert fingerprint(package) == before
    with closing(sqlite3.connect(restored / "laserclaw.db")) as db:
        result = db.execute("SELECT description FROM experiment_cases WHERE id=?", (case,)).fetchone()
        assert result is not None
        if extension == "lclaw":
            assert result[0] == "Later disk snapshot"
        relative_file = db.execute("SELECT filepath FROM attachments LIMIT 1").fetchone()[0]
        assert Path(relative_file).is_relative_to(restored) and Path(relative_file).is_file()
    with closing(sqlite3.connect(old / "laserclaw.db")) as db:
        assert db.execute("SELECT 1 FROM experiment_cases WHERE id=?", (case,)).fetchone() is None


@pytest.fixture
def archive_case(client, db, tmp_path, monkeypatch):
    monkeypatch.setattr(attachments.settings, "upload_dir", str(tmp_path / "sender"))
    payload = b"time_us,reading_mV\n0,0.1\n1,0.5\n2,1.7\n3,1.2\n"
    case = client.post("/api/cases", json={"title": "Scope handover"}).json()
    url = f"/api/cases/{case['id']}"
    row = client.post(url + "/attachments", files={"file": ("trace.csv", payload, "text/csv")},
                      data={"relative_path": "scope/trace.csv"}).json()
    return case["id"], url, row["id"], payload


def test_recipient_recomputes_and_continues_without_other_cases(client, db, tmp_path, archive_case):
    case, url, file_id, raw = archive_case
    other = client.post("/api/cases", json={"title": "Unrelated experiment"}).json()["id"]
    lens = InventoryItem(name="Bench lens", category="lens", quantity=5, focal_length_mm=80,
                         raw_spec="f=80 mm; coating label illegible", keeper="private keeper")
    unused = InventoryItem(name="Unrelated lens", category="lens")
    db.add_all([lens, unused])
    db.flush()
    db.add(CoatingSpec(item_id=lens.id, surface="S1", wl_min_nm=700, wl_max_nm=900, function="AR"))
    project = {"title": "relay", "system": {"mode": "single_pass", "wavelength_nm": 808, "input_radius_mm": .5,
               "elements": [{"uid": "lens", "kind": "lens", "name": "L", "focal_mm": 80},
                            {"uid": "air", "kind": "space", "name": "air", "length_mm": 25}]},
               "parts": [{"element_index": 0, "inventory_id": lens.id, "source": "Bench notes"}]}
    db.commit()
    response = client.post(url + "/optical-project", json={"project": project, "change_note": "Measured lens"})
    assert response.status_code == 200, response.text
    saved = response.json()
    shared = AgentChatSession(case_id=case)
    private = AgentChatSession(case_id=case)
    db.add_all([shared, private])
    db.flush()
    db.add_all([AgentChatMessage(session_id=shared.id, role="user", content="Use the measured trace"),
                AgentChatMessage(session_id=private.id, role="user", content="Private conversation")])
    db.get(ExperimentCase, case).parameters = {"workbench_session_id": shared.id}
    run = AgentTask(case_id=case, mode="workbench", status="completed", goal="Find peak")
    db.add(run)
    db.flush()
    db.add(AgentToolCall(task_id=run.id, tool_name="workbench_request",
                         input_json={"principal": {"actor": "private identity"}, "session_id": shared.id}))
    call = AgentToolCall(task_id=run.id, tool_name="inspect_measurement_table", output_json={"maximum": [4, 2, 1.7]})
    db.add(call)
    module = CaseModule(case_id=case, module_type="measurement", title="Measurements")
    db.add(module)
    db.flush()
    for idx, data in enumerate([b"one", b"two"]):
        original = tmp_path / f"input-{idx}.csv"
        original.write_bytes(data)
        db.add(CaseModuleFile(module_id=module.id, filename="same.csv", file_role="input", filepath=str(original)))
    db.add(CaseComponentItem(module_id=module.id, case_id=case, name="Bench clamp"))
    other_artifact = GeneratedContent(case_id=other, content_type="memo", content={"summary": "Private result"})
    db.add(other_artifact)
    db.commit()

    response = client.get(url + "/bundle")
    assert response.status_code == 200, response.text[:200] if response.status_code != 200 else ""
    assert f"beamweft-case-{case}-bundle.zip" in response.headers["content-disposition"]
    package = tmp_path / "case.zip"
    package.write_bytes(response.content)
    with zipfile.ZipFile(package) as z:
        history = z.read(HISTORY)
        assert all(secret not in history for secret in [b"Private conversation", b"private identity", b"private keeper", b"Unrelated lens"])
    folder, marker = open_package(package, tmp_path / "reception")
    assert marker["case_id"] == case and not marker["legacy"]
    engine = create_engine(URL.create("sqlite", database=str(folder / "laserclaw.db")))
    try:
        with Session(engine) as peer:
            assert peer.query(ExperimentCase).count() == peer.query(AgentChatSession).count() == 1
            assert peer.query(AgentChatMessage).one().content == "Use the measured trace"
            assert peer.get(AgentToolCall, call.id).output_json == call.output_json
            restored = peer.get(Attachment, file_id)
            assert Path(restored.filepath).read_bytes() == raw
            src = peer.query(KnowledgeSource).filter_by(attachment_id=file_id).one()
            assert src.metadata_json["filepath"] == restored.filepath
            assert Path(restored.filepath).is_relative_to(folder) and src.chunks
            assert peer.get(InventoryItem, unused.id) is None
            spec = peer.get(InventoryItem, lens.id)
            assert spec.quantity == 0 and spec.focal_length_mm == 80 and spec.keeper is None
            assert spec.raw_spec == "f=80 mm; coating label illegible"
            assert spec.coatings[0].surface == "S1"
            assert [Path(f.filepath).read_bytes() for f in peer.query(CaseModuleFile).order_by(CaseModuleFile.id)] == [b"one", b"two"]
            assert peer.query(CaseComponentItem).one().module_id == module.id
            archived = peer.get(GeneratedContent, saved["revision_id"])
            computed = evaluate_project(peer, OpticalProject.model_validate(archived.content["project"]))
            assert computed["analysis"] == archived.content["analysis"]
            assert inspect_measurement(peer, case, InspectMeasurement(attachment_id=file_id, x_upper=1))["maximum"] == (3, 1, .5)
            edit = GeneratedContent(case_id=case, content_type="memo", content={"summary": "Recipient note"})
            peer.add(edit)
            peer.commit()
            assert edit.id > other_artifact.id
        same, _ = open_package(package, tmp_path / "reception")
        assert same == folder
        with Session(engine) as peer:
            assert peer.query(GeneratedContent).filter_by(content_type="memo").one().content["summary"] == "Recipient note"
    finally:
        engine.dispose()


def test_missing_original_active_run_and_damaged_archive(client, db, tmp_path, monkeypatch, archive_case):
    case, url, file_id, raw = archive_case
    task = AgentTask(case_id=case, mode="workbench", status="pending", goal="new scan")
    db.add(task)
    db.commit()
    assert client.get(url + "/bundle").status_code == 409
    task.status = "cancelled"
    db.commit()
    from app.workbench import service
    monkeypatch.setattr(service, "ACTIVE_RUNS", {task.id})
    assert client.get(url + "/bundle").status_code == 409
    service.ACTIVE_RUNS.clear()
    intact = client.get(url + "/bundle")
    assert intact.status_code == 200
    original = Path(db.get(Attachment, file_id).filepath)
    original.write_bytes(b"overwritten measurement")
    assert "指纹" in client.get(url + "/bundle").json()["detail"]
    original.unlink()
    assert "trace.csv" in client.get(url + "/bundle").json()["detail"]
    bad = tmp_path / "damaged.zip"
    with zipfile.ZipFile(io.BytesIO(intact.content)) as source, zipfile.ZipFile(bad, "w") as output:
        for info in source.infolist():
            data = source.read(info)
            output.writestr(info.filename, data if data != raw else raw + b"changed")
    with pytest.raises(PackageError, match="trace.csv"):
        open_package(bad, tmp_path / "copies")
    assert list((tmp_path / "copies").iterdir()) == []


def test_legacy_case_and_redistribution_keep_provenance(client, tmp_path, archive_case):
    case, url, file_id, raw = archive_case
    response = client.get(url + "/bundle")
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    manifest.pop("package_format")
    manifest.pop("history_sha256")
    manifest.pop("source_id_limits")
    manifest["attachments"][0]["content_hash"] = None
    legacy = tmp_path / "old.zip"
    with zipfile.ZipFile(legacy, "w") as out:
        out.writestr("manifest.json", json.dumps(manifest))
        out.writestr(manifest["attachments"][0]["bundle_path"], raw)
    folder, marker = open_package(legacy, tmp_path / "received")
    assert marker["legacy"]
    engine = create_engine(URL.create("sqlite", database=str(folder / "laserclaw.db")))
    second = tmp_path / "second.zip"
    try:
        with Session(engine) as peer:
            assert peer.get(Attachment, file_id).content_hash == hashlib.sha256(raw).hexdigest()
            write_package(peer, peer.get(ExperimentCase, case), second, Principal("anonymous", "user"))
    finally:
        engine.dispose()
    _, forwarded = open_package(second, tmp_path / "forwarded")
    assert forwarded["legacy"]

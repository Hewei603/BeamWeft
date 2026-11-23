import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from PIL import Image
from sqlalchemy import create_engine, insert, select, update
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from app.api import attachments
from app.attachment_evidence import OriginalRemoved, retained_by
from app.database import Base
from app.models import AgentTask, AgentToolCall, Attachment, AuditLog, ExperimentCase, GeneratedContent, KnowledgeSource
from app.open_case import open_package


@pytest.fixture
def files(client, tmp_path, monkeypatch):
    monkeypatch.setattr(attachments.settings, "upload_dir", str(tmp_path))
    case = client.post("/api/cases", json={"title": "Evidence lifecycle"}).json()["id"]
    def upload(name, data):
        result = client.post(f"/api/cases/{case}/attachments", files={"file": (name, data)})
        assert result.status_code == 201, result.text
        return result.json()["id"]
    return case, upload


def test_prediction_evidence_opens_only_the_selected_original_in_its_case(client, db, files):
    case, upload = files
    original = upload("observation.csv", b"width_um\n171\n")
    extra = upload("notes.txt", b"other acquisition")
    endpoint = f"/api/cases/{case}/attachments"
    assert {row["id"] for row in client.get(endpoint).json()} == {original, extra}
    item, = client.get(endpoint, params={"attachment_id": original}).json()
    assert item["id"] == original and item["content_hash"]
    other = client.post("/api/cases", json={"title": "Another rig"}).json()["id"]
    assert client.get(f"/api/cases/{other}/attachments", params={"attachment_id": original}).json() == []
    db.add(GeneratedContent(case_id=case, content_type="prediction_check", content={"source": {
        "attachment_id": original, "sha256": item["content_hash"]}}))
    db.commit()
    assert client.delete(f"/api/attachments/{original}").status_code == 409


def test_saved_image_keeps_original_and_retired_number_survives_transfer(client, db, files, tmp_path):
    case, upload = files
    buffer = io.BytesIO()
    Image.new("L", (4, 4), 4).save(buffer, format="PNG")
    frame = upload("frame.png", buffer.getvalue())
    source = f"/api/attachments/{frame}"
    result = client.post(source + "/analyze").json()
    refused = client.delete(source)
    assert refused.status_code == 409 and f"结果 #{result['generated_content_id']}" in refused.json()["detail"]
    assert client.get(source).content == buffer.getvalue()
    free = upload("mistake.csv", b"aaa,bbb")
    upload_path = Path(db.get(Attachment, free).filepath)
    assert client.delete(f"/api/attachments/{free}").status_code == 204 and not upload_path.exists()
    assert client.get(f"/api/attachments/{free}").status_code == 404
    package = tmp_path / "saved.lclaw"
    package.write_bytes(client.get(f"/api/cases/{case}/project-file").content)
    workspace, _ = open_package(package, tmp_path / "restored")
    engine = create_engine(URL.create("sqlite", database=str(workspace / "laserclaw.db")))
    try:
        with Session(engine) as restored:
            original = restored.get(Attachment, frame)
            assert retained_by(restored, original) == f"已保存的结果 #{result['generated_content_id']}"
            assert Path(original.filepath).read_bytes() == buffer.getvalue()
            next_file = Attachment(case_id=case, filename="new", filepath="unused")
            restored.add(next_file)
            restored.commit()
            assert next_file.id > free
    finally:
        engine.dispose()
    assert upload("another.csv", b"a,b") > free


def test_restore_preexisting_source_links_and_ignore_unrelated_numbers(client, db, files):
    case, upload = files
    used = upload("source.dat", b"x")
    incidental = upload("not-a-source.dat", b"y")
    table = upload("values.tsv", b"x\ty\n1\t2\n")
    # Core insertion reproduces old or imported rows without today's save hooks.
    db.execute(insert(GeneratedContent).values(case_id=case, content_type="scientific_study", content={
        "originals": [{"attachment_id": used}], "connections": {"w": {"table": {"attachment_id": table}}},
        "output": {"attachment_id": incidental, "values": [incidental] * 500},
        "inputs": {"other_id": incidental}}))
    db.commit()
    for identifier in (used, table):
        assert db.get(Attachment, identifier).evidence_use is None
        assert client.delete(f"/api/attachments/{identifier}").status_code == 409
    assert client.delete(f"/api/attachments/{incidental}").status_code == 204
    # A result that finished computing after its file was deleted must not be saved.
    db.add(GeneratedContent(case_id=case, content_type="scientific_study", content={"originals": [{"attachment_id": incidental}]}))
    with pytest.raises(OriginalRemoved):
        db.commit()
    db.rollback()


def test_legacy_receipts_and_citations_agent_read_and_browse(client, db, files):
    case, upload = files
    db.add(AuditLog(action="attachment.delete", resource_type="attachment", resource_id="82"))
    db.commit()
    chosen = upload("selected.csv", b"a,b\n1,2")
    cited = upload("retrieved.txt", b"first optical observation")
    browsed = upload("seen.txt", b"only a directory listing")
    assert chosen == 83
    assert client.get(f"/api/attachments/{browsed}/preview").json()["status"] == "completed"
    task = AgentTask(case_id=case, goal="read a measurement", mode="workbench", status="running")
    db.add(task)
    db.flush()
    knowledge_id = db.scalar(select(KnowledgeSource.id).where(KnowledgeSource.attachment_id == cited))
    db.add_all([
        AgentToolCall(task_id=task.id, tool_name="workbench_request", status="completed", input_json={"attachment_ids": [chosen]}),
        AgentToolCall(task_id=task.id, tool_name="search_knowledge", status="completed", input_json={},
                      output_json={"citations": [{"source_id": knowledge_id}]}),
        AgentToolCall(task_id=task.id, tool_name="read_attachment", status="completed", input_json={},
                      output_json={"files": [{"id": browsed}]}),
    ])
    db.commit()
    for identifier in (chosen, cited):
        assert db.get(Attachment, identifier).evidence_use == f"Agent 任务 #{task.id}"
    db.execute(update(Attachment).values(evidence_use=None))
    db.commit()
    for identifier in (chosen, cited):
        assert client.delete(f"/api/attachments/{identifier}").status_code == 409
    assert client.delete(f"/api/attachments/{browsed}").status_code == 204


def test_failed_database_delete_preserves_original(client, db, files, monkeypatch):
    _, upload = files
    file = upload("uncommitted.bin", b"original")
    path = Path(db.get(Attachment, file).filepath)
    def fail_commit():
        raise RuntimeError("injected database error")
    with monkeypatch.context() as patch:
        patch.setattr(db, "commit", fail_commit)
        assert client.delete(f"/api/attachments/{file}").status_code == 500
    db.rollback()
    assert client.get(f"/api/attachments/{file}").content == path.read_bytes() == b"original"


def test_design_fact_sources_are_namespaced_and_portable_across_schema_generations(client, db, files):
    case, upload = files
    legacy, modern, selected, incidental = (upload(f"{index}.dat", b"x") for index in range(4))
    design = {"intake_files": [{"attachment_id": selected}], "facts": {
        "wavelength": {"value": 532, "source": f"image:{legacy}"},
        "radius": {"source_file": {"attachment_id": modern}},
        "inventory": {"source": f"inventory:{incidental}"},
        "raw": {"quote": f"image:{incidental}"},
    }}
    db.add(GeneratedContent(case_id=case, content_type="plan", content={"engineering_design": design}))
    db.commit()
    for identifier in (legacy, modern, selected):
        assert db.get(Attachment, identifier).evidence_use
    db.execute(update(Attachment).values(evidence_use=None))
    db.commit()
    for identifier in (legacy, modern, selected):
        assert client.delete(f"/api/attachments/{identifier}").status_code == 409
    assert client.delete(f"/api/attachments/{incidental}").status_code == 204


@pytest.mark.parametrize("save_first", [True, False])
def test_uncommitted_result_and_concurrent_delete_have_one_winner(tmp_path, save_first):
    engine = create_engine(URL.create("sqlite", database=str(tmp_path / "race.db")))
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            case = ExperimentCase(title="Concurrent edits", cavity_type="", goal="")
            db.add(case)
            db.flush()
            item = Attachment(case_id=case.id, filename="frame.dat", filepath="unused")
            db.add(item)
            db.commit()
            case_id, fid = case.id, item.id
        def save(db):
            db.add(GeneratedContent(case_id=case_id, content_type="scientific_study", content={"originals": [{"attachment_id": fid}]}))
            db.flush()
            return "saved"
        def remove(db):
            item = db.get(Attachment, fid)
            if retained_by(db, item):
                return "retained"
            db.delete(item)
            db.flush()
            return "removed"
        started = Event()
        def peer():
            with Session(engine) as db:
                started.set()
                try:
                    state = (remove if save_first else save)(db)
                    db.commit()
                    return state
                except OriginalRemoved:
                    db.rollback()
                    return "no original"
        with ThreadPoolExecutor(max_workers=1) as pool, Session(engine) as first:
            (save if save_first else remove)(first)
            second = pool.submit(peer)
            assert started.wait(timeout=5)
            first.commit()
            assert second.result(timeout=5) == ("retained" if save_first else "no original")
        with Session(engine) as db:
            assert (db.get(Attachment, fid) is not None) is save_first
            assert bool(db.scalar(select(GeneratedContent.id))) is save_first
    finally:
        engine.dispose()

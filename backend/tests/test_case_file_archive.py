import base64
import hashlib
import io
import json
import uuid
import zipfile
from pathlib import Path

import pytest
from openpyxl import Workbook
from PIL import Image

from app.api import attachments
from app.models import Attachment, GeneratedContent, KnowledgeSource


@pytest.fixture
def archive(client, tmp_path, monkeypatch):
    monkeypatch.setattr(attachments.settings, "upload_dir", str(tmp_path))
    case = client.post("/api/cases", json={"title": "Archive test"}).json()
    return f"/api/cases/{case['id']}"


def upload(client, archive, name, content, **form):
    return client.post(archive + "/attachments", files={"file": (name, content, "application/octet-stream")},
                       data=form)


def test_archive_round_trip_receipt_and_duplicate_names(client, archive, tmp_path):
    key = str(uuid.uuid4())
    payloads = [b"instrument-A", b"instrument-B", b"another run"]
    names = ["run/day1/trace.wfm", "run/day2/trace.wfm", "run/day1/trace.wfm"]
    items = []
    for i, (data, name) in enumerate(zip(payloads, names)):
        response = upload(client, archive, "trace.wfm", data, relative_path=name,
                          upload_id=key if i == 0 else str(uuid.uuid4()))
        assert response.status_code == 201
        items.append(response.json())
    replay = upload(client, archive, "trace.wfm", payloads[0], relative_path=names[0], upload_id=key)
    assert replay.json()["id"] == items[0]["id"]
    assert upload(client, archive, "trace.wfm", b"different", relative_path=names[0], upload_id=key).status_code == 409
    assert len(list(tmp_path.iterdir())) == len(client.get(archive + "/attachments").json()) == 3
    for item, data in zip(items, payloads):
        url = f"/api/attachments/{item['id']}"
        assert item["read_mode"] == "archive"
        assert client.get(url).content == data
        assert client.get(url + "/preview").json()["status"] == "unsupported"
    with zipfile.ZipFile(io.BytesIO(client.get(archive + "/bundle").content)) as bundle:
        rows = json.loads(bundle.read("manifest.json"))["attachments"]
        assert [row["relative_path"] for row in rows] == names
        assert len({row["bundle_path"] for row in rows}) == 3
        for row, data in zip(rows, payloads):
            assert bundle.read(row["bundle_path"]) == data
            assert row["content_hash"] == hashlib.sha256(data).hexdigest()


def test_workbook_preview_paginates_without_losing_sheet_row_or_value(client, archive):
    book = Workbook()
    book.active.title = "光谱"
    for n in range(80):
        book.active.append([n, f"sample-{n}"])
    book.create_sheet("功率").append(["P / W", 2.5])
    data = io.BytesIO()
    book.save(data)
    item = upload(client, archive, "measure.xlsx", data.getvalue()).json()
    assert item["read_mode"] == "workbook"
    url = f"/api/attachments/{item['id']}/preview"
    parts, offset = [], 0
    while offset is not None:
        result = client.get(url, params={"offset": offset, "limit": 100}).json()
        assert result["status"] == "completed"
        parts.append(result["text"])
        offset = result["next_offset"]
    full = "".join(parts)
    assert full == client.get(url).json()["text"]
    assert "80\t79\tsample-79" in full and "Sheet: 功率\n1\tP / W\t2.5" in full


def test_corrupt_source_remains_downloadable_without_successful_read(client, archive):
    bad_bytes = b"damaged instrument output"
    for name in ("scan.pdf", "scan.xlsx", "beam.png"):
        item = upload(client, archive, name, bad_bytes).json()
        if name.endswith(".pdf"):
            assert item["index_note"]
        url = f"/api/attachments/{item['id']}"
        assert client.get(url).content == bad_bytes
        assert client.get(url + "/preview").json()["status"] == "needs_input"
        if name.endswith(".png"):
            assert client.post(url + "/analyze").status_code == 422


def test_tiff_preview_is_separate_from_the_original_measurement(client, archive):
    original = io.BytesIO()
    frame = Image.new("I;16", (9, 7), 1500)
    frame.putpixel((4, 3), 3500)
    frame.save(original, format="TIFF")
    item = upload(client, archive, "beam.tiff", original.getvalue()).json()
    url = f"/api/attachments/{item['id']}"
    response = client.get(url + "/preview").json()
    assert response["status"] == "completed"
    assert response["image_url"].startswith("data:image/png;base64,")
    with Image.open(io.BytesIO(base64.b64decode(response["image_url"].split(",")[1]))) as preview:
        assert preview.getpixel((0, 0)) == 0
        assert preview.getpixel((4, 3)) == 255
    assert client.get(url).content == original.getvalue()
    assert client.post(url + "/analyze").status_code == 200


def test_invalid_folder_or_size_leaves_no_partial_file(client, archive, tmp_path, monkeypatch):
    monkeypatch.setattr(attachments.settings, "max_upload_size", 4)
    assert upload(client, archive, "trace", b"abc", relative_path="../trace").status_code == 422
    assert upload(client, archive, "trace", b"12345").status_code == 413
    assert list(tmp_path.iterdir()) == []
    assert client.get(archive + "/attachments").json() == []


def test_changed_original_cannot_be_previewed_downloaded_or_reindexed(client, archive, db):
    original = b"x,value\n1,2.75\n"
    item = upload(client, archive, "trace.csv", original).json()
    attachment = db.get(Attachment, item["id"])
    source = db.query(KnowledgeSource).filter_by(attachment_id=item["id"]).one()
    index_hash = source.content_hash
    url = f"/api/attachments/{item['id']}"
    Path(attachment.filepath).write_bytes(original.replace(b"2.75", b"9.85"))
    preview = client.get(url + "/preview").json()
    assert preview["failure_code"] == "original_mismatch"
    assert "text" not in preview and "content_hash" not in preview
    assert client.get(url, headers={"Range":"bytes=0-4"}).status_code == 409
    assert client.post(f"/api/knowledge/sources/{source.id}/reindex").status_code == 409
    db.refresh(source)
    assert source.content_hash == index_hash
    Path(attachment.filepath).write_bytes(original)
    assert client.get(url + "/preview").json()["content_hash"] == hashlib.sha256(original).hexdigest()
    partial = client.get(url, headers={"Range":"bytes=0-4"})
    assert partial.status_code == 206 and partial.content == original[:5]
    assert partial.headers["etag"] == '"' + hashlib.sha256(original).hexdigest() + '"'


def test_image_analysis_records_verified_display_and_never_uses_another_frame(client, archive, db):
    def camera(level):
        data = io.BytesIO()
        Image.new("I;16", (8, 6), level).save(data, format="TIFF")
        return data.getvalue()

    original = camera(1500)
    item = upload(client, archive, "frame.tiff", original).json()
    attachment = db.get(Attachment, item["id"])
    url = f"/api/attachments/{item['id']}"
    first = client.post(url + "/analyze").json()["content"]
    assert first["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert "display copy" in first["representation"]
    Path(attachment.filepath).write_bytes(camera(2500))
    assert client.post(url + "/analyze").status_code == 409
    assert db.query(GeneratedContent).filter_by(content_type="image_analysis").count() == 1


def test_restore_missing_original_requires_exact_bytes_and_keeps_id_and_index(client, archive, db):
    original = b"time_us,reading_mV\n3,8.125\n"
    item = upload(client, archive, "instrument.csv", original).json()
    attachment = db.get(Attachment, item["id"])
    path = Path(attachment.filepath)
    path.unlink()
    url = f"/api/attachments/{item['id']}"
    missing = client.get(url + "/preview").json()
    assert missing["failure_code"] == "missing_original" and missing["expected_sha256"] == item["content_hash"]
    def restore(name, payload):
        return client.put(url + "/original", files={"file":(name, payload, "text/csv")})
    assert restore("instrument.csv", original.replace(b"8.125", b"9.125")).status_code == 409
    assert not path.exists()
    assert restore("another-filename.txt", original).json()["id"] == item["id"]
    assert restore("another-filename.txt", original).status_code == 200
    assert list(path.parent.iterdir()) == [path]
    assert path.read_bytes() == original and client.get(url).content == original
    assert len(client.get(archive + "/attachments").json()) == 1
    assert db.query(KnowledgeSource).filter_by(attachment_id=item["id"]).count() == 1
    attachment.content_hash = None
    db.commit()
    assert client.get(url + "/preview").json()["integrity"] == "computed"
    assert restore("another-filename.txt", original).status_code == 409


def test_removed_source_during_image_analysis_reports_conflict(client, archive, db, monkeypatch):
    data = io.BytesIO()
    Image.new("L", (2, 2), 42).save(data, format="PNG")
    item = upload(client, archive, "frame.png", data.getvalue()).json()
    url = f"/api/attachments/{item['id']}"
    class Provider:
        async def analyze_image(self, *_):
            assert client.delete(url).status_code == 204
            return {"description":"A test exposure"}
    monkeypatch.setattr(attachments, "get_ai_provider", Provider)
    reply = client.post(url + "/analyze")
    assert reply.status_code == 409 and "原件" in reply.json()["detail"]
    assert db.query(GeneratedContent).filter_by(content_type="image_analysis").count() == 0


def test_reader_and_download_keep_the_verified_snapshot_during_archive_restore(client, archive, db, monkeypatch):
    from app.attachment_files import verified_original
    from starlette.responses import FileResponse

    original = b"setpoint,signal\n15,256\n"
    replacement = original.replace(b"256", b"768")
    item = upload(client, archive, "run.dat", original).json()
    attachment = db.get(Attachment, item["id"])
    path = Path(attachment.filepath)
    with verified_original(attachment) as (frozen, digest):
        path.write_bytes(replacement)
        assert frozen.read_bytes() == original
        assert digest == hashlib.sha256(original).hexdigest()
    assert not frozen.exists()
    path.write_bytes(original)
    send = FileResponse.__call__

    async def restore_during_download(response, scope, receive, deliver):
        path.write_bytes(replacement)
        return await send(response, scope, receive, deliver)

    monkeypatch.setattr(FileResponse, "__call__", restore_during_download)
    result = client.get(f"/api/attachments/{item['id']}")
    assert result.status_code == 200 and result.content == original and path.read_bytes() == replacement

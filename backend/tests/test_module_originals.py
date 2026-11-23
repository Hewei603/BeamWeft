import io
from pathlib import Path
from zipfile import ZipFile

from app.api import case_modules
from app.models import CaseModuleFile


def test_original_recovery_preserves_module_measurement_and_portable_history(client, db, tmp_path, monkeypatch):
    monkeypatch.setattr(case_modules.settings, "upload_dir", str(tmp_path))
    case_id = client.post("/api/cases", json={"title":"Spectrometer"}).json()["id"]
    url = f"/api/cases/{case_id}"
    module = client.post(url + "/modules", json={"module_type":"spectrum", "title":"Calibration"}).json()
    base = f"/api/cases/modules/{module['id']}"
    raw = b"wavelength_nm,intensity\n550,3\n560,7\n"
    def upload(data):
        return {"file":("trace.csv", data, "text/csv")}
    row = client.post(base + "/files", files=upload(raw)).json()
    file_url = f"/api/cases/module-files/{row['id']}"
    original = Path(db.get(CaseModuleFile, row["id"]).filepath)
    result = client.post(base + "/run", json={}).json()["result_json"]
    assert result["input_file_id"] == row["id"] and result["peak_intensity"] == 7

    changed = raw.replace(b"560,7", b"560,9000")
    original.write_bytes(changed)
    assert client.get(file_url).status_code == client.post(base + "/run", json={}).status_code == 409
    failed_export = client.get(url + "/project-file")
    assert failed_export.status_code == 409 and "Calibration" in failed_export.json()["detail"]
    assert client.get(url + "/modules").json()[0]["result_json"] == result
    assert client.put(file_url + "/original", files=upload(changed)).status_code == 409
    assert original.read_bytes() == changed
    original.unlink()
    assert client.get(file_url).status_code == 404
    assert client.put(file_url + "/original", files=upload(raw)).json() == row
    assert client.put(file_url + "/original", files=upload(raw)).json() == row
    downloaded = client.get(file_url)
    assert downloaded.content == raw and downloaded.headers["etag"] == '"' + row["content_hash"] + '"'
    package = client.get(url + "/project-file")
    assert package.status_code == 200
    with ZipFile(io.BytesIO(package.content)) as archive:
        assert archive.read(f"modules/{module['id']}/{row['id']}/trace.csv") == raw
    assert client.get(url + "/modules").json()[0]["result_json"] == result


def test_legacy_module_file_without_recorded_hash_keeps_its_uncertain_identity(client, db, tmp_path, monkeypatch):
    monkeypatch.setattr(case_modules.settings, "upload_dir", str(tmp_path))
    case = client.post("/api/cases", json={"title":"Legacy detector"}).json()["id"]
    module = client.post(f"/api/cases/{case}/modules", json={"module_type":"beam_profile"}).json()["id"]
    backup = {"file":("notes.txt", b"detector A", "text/plain")}
    file_id = client.post(f"/api/cases/modules/{module}/files", files=backup).json()["id"]
    item = db.get(CaseModuleFile, file_id)
    item.content_hash = None
    db.commit()
    file_url = f"/api/cases/module-files/{file_id}"
    assert client.get(file_url).content == b"detector A"
    Path(item.filepath).unlink()
    failure = client.put(file_url + "/original", files=backup)
    assert failure.status_code == 409 and "没有归档指纹" in failure.json()["detail"]
    assert "完整工程备份" in client.get(f"/api/cases/{case}/project-file").json()["detail"]
    assert not Path(item.filepath).exists()

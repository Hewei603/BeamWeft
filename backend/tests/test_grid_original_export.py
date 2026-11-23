import hashlib
import json
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from app.study_api import StrictInput, Study
from app.models import GeneratedContent
from app.workbench import tools
from app.workbench.grid_export import GridViewport, InspectNativeGrid, grid_digest, grid_view
from app.workbench.grid_original import GridOriginal, read_grid
from app.workbench.studies import inspect_native_grid


def npy_bytes(array):
    file = BytesIO()
    np.save(file, array)
    return file.getvalue()


def bound(data):
    original = GridOriginal(attachment_id=1)
    original._stream = BytesIO(data)
    return original


def test_raw_grid_preserves_instrument_order_and_coded_samples():
    values = np.asfortranarray(np.arange(12).reshape(4, 3).astype(">c16") * (1 + 2j))
    source = bound(npy_bytes(values))
    with pytest.raises(ValueError, match="复数|实数"):
        read_grid(source)
    np.testing.assert_array_equal(read_grid(source, allow_complex=True), values)
    values = np.arange(12, dtype=">u2").reshape(3, 4) * 7
    pgm = b"P5\n4 3\n1023\n" + values.astype(">u2").tobytes()
    np.testing.assert_array_equal(read_grid(bound(pgm)), values)
    np.testing.assert_array_equal(read_grid(bound(pgm + b"\n")), values)
    with pytest.raises(ValueError, match="单个"):
        read_grid(bound(pgm * 2))
    target = BytesIO()
    Image.fromarray(values.astype("uint16")).save(target, format="PNG")
    np.testing.assert_array_equal(read_grid(bound(target.getvalue())), values)
    for array, reason in [
        (np.ones((2, 3, 4)), "二维"),
        (np.array([[float("nan")]]), "有限"),
        (np.array([["signal"]]), "数值"),
    ]:
        with pytest.raises(ValueError, match=reason):
            read_grid(bound(npy_bytes(array)))
    with pytest.raises(ValueError, match="字节长度"):
        read_grid(bound(npy_bytes(values)[:-2]))


def test_original_grid_export_checks_the_frozen_study_before_delivery(client, monkeypatch, db):
    class Inputs(StrictInput):
        counts: GridOriginal

    unit = "积分计数"

    def native(args):
        grid = read_grid(args.counts)
        return SimpleNamespace(
            x_edges_mm=np.arange(grid.shape[1] + 1) * 0.25,
            y_edges_mm=np.arange(grid.shape[0] + 1) * 0.4,
            cell_integrals=grid,
            signal_unit=unit,
        )

    def measure(args):
        result = native(args)
        return {"native_grid_sha256": grid_digest(result.x_edges_mm, result.y_edges_mm, result.cell_integrals, unit)}

    study = Study("read_detector_area", "探测器计数", "1", "各像素原始计数", Inputs, measure, native_grid=native)
    monkeypatch.setitem(tools.STUDIES, study.name, (study, "detector lab"))
    case = client.post("/api/cases", json={"title": "探测器标定"}).json()["id"]
    base = f"/api/cases/{case}"
    values = np.array([[1, 0, 12], [-4, 5, 6]], dtype=">i2")
    raw = npy_bytes(values)
    original = client.post(base + "/attachments", files={"file": ("camera.npy", raw)}).json()
    saved = client.post(
        base + "/studies/run/" + study.name,
        json={"version": "1", "provider": "detector lab", "inputs": {"counts": {"attachment_id": original["id"]}}},
    )
    assert saved.is_success, saved.text
    receipt = saved.json()
    path = f"/studies/runs/{receipt['study_record']['id']}/native-grid"
    access = receipt["study_record"]["native_grid_access"]
    assert access["action"] == "inspect_native_grid" and access["download_path"] == base + path
    report = inspect_native_grid(
        db.get_bind(), case, InspectNativeGrid(run_id=access["run_id"], window=(1, 1, 2, 1)), tools.STUDIES
    )
    assert report["verified_against_saved_grid"] and report["cell_integrals"] == [[5, 6]] and report["integral"] == 11
    stamp = receipt["study_record"]["solver_receipt"]
    assert report["saved_solver_receipt"] == report["verification_solver_receipt"] == stamp
    zoom = client.get(
        base + path + "/view",
        params={"x_min_mm": 0.25, "x_max_mm": 0.75, "y_min_mm": 0.4, "y_max_mm": 0.8, "max_side": 1},
    )
    assert zoom.is_success, zoom.text
    detail = zoom.json()
    assert detail["native_window"] == [1, 1, 2, 1] and detail["original_shape_yx"] == [2, 3]
    np.testing.assert_allclose(detail["density"], [[55]])
    np.testing.assert_allclose(detail["density_range"], [-40, 120])
    assert client.get(base + path + "/view", params={"x_min_mm": 0}).status_code == 422
    assert client.get(base + path + "/view", params={"y_min_mm": 5, "y_max_mm": 6}).json()["status"] == "outside_grid"
    response = client.get(base + path)
    assert response.is_success, response.text[:200] if not response.is_success else ""
    assert int(response.headers["Content-Length"]) == len(response.content)
    with np.load(BytesIO(response.content), allow_pickle=False) as result:
        np.testing.assert_array_equal(result["cell_integrals"], values)
        assert result["signal_unit"].item() == unit
        assert result["grid_sha256"].item() == receipt["native_grid_sha256"]
        evidence = json.loads(result["provenance_json"].item())
        assert evidence["saved_solver_receipt"] == evidence["verification_solver_receipt"] == stamp
        assert evidence["originals"][0]["sha256"] == hashlib.sha256(raw).hexdigest()
        assert evidence["originals"][0]["input_paths"] == [["counts"]]
        assert evidence["inputs"]["counts"]["sha256"] == evidence["originals"][0]["sha256"]
    other = client.post("/api/cases", json={"title": "另一工程"}).json()["id"]
    assert client.get(f"/api/cases/{other}" + path).status_code == 404
    assert client.get(f"/api/cases/{other}" + path + "/view").status_code == 404

    # A successor can reproduce an old grid when it accepts the old optical inputs and the numbers still agree.
    successor = replace(study, version="2", accepts_inputs_from=(("detector lab", "1"),))
    monkeypatch.setitem(tools.STUDIES, study.name, (successor, "detector lab"))
    verified = client.get(base + path)
    assert verified.status_code == 200
    with np.load(BytesIO(verified.content), allow_pickle=False) as result:
        evidence = json.loads(result["provenance_json"].item())
        assert evidence["verified_against_saved_grid"] and evidence["saved_solver_receipt"] == stamp
        assert evidence["verification_solver_receipt"]["version"] == "2" and evidence["version"] == "1"
        assert result["grid_sha256"].item() == receipt["native_grid_sha256"]

    historical = db.get(GeneratedContent, access["run_id"])
    historical.content = {key:value for key, value in historical.content.items() if key != "solver_receipt"}
    db.commit()
    report = inspect_native_grid(db.get_bind(), case, InspectNativeGrid(run_id=access["run_id"]), tools.STUDIES)
    assert report["saved_solver_receipt"] is None and report["verified_against_saved_grid"]
    assert report["verification_solver_receipt"]["version"] == "2"
    incompatible = replace(study, version="3")
    monkeypatch.setitem(tools.STUDIES, study.name, (incompatible, "detector lab"))
    assert client.get(base + path).status_code == 422
    monkeypatch.setitem(tools.STUDIES, study.name, (successor, "detector lab"))
    unit = "毫瓦"
    mismatch = client.get(base + path)
    assert mismatch.status_code == 422 and "校验值" in mismatch.text
    monkeypatch.delitem(tools.STUDIES, study.name)
    missing = client.get(base + path)
    assert missing.status_code == 422 and "安装" in missing.text


def test_detail_view_uses_native_cell_area_and_shared_scale():
    grid = SimpleNamespace(
        x_edges_mm=np.array([1.0, 2.0, 4.0, 7.0]),
        y_edges_mm=np.array([3.0, 6.0, 7.0]),
        cell_integrals=np.array([[3.0, 12.0, 0.0], [-2.0, 2.0, 9.0]]),
        signal_unit="counts",
    )
    overview = grid_view(grid, GridViewport(max_side=1))
    np.testing.assert_allclose(overview["density"], [[24 / 24]])
    view = grid_view(grid, GridViewport(x_min_mm=2.01, x_max_mm=6.3, y_min_mm=6.0, y_max_mm=8.0, max_side=5))
    assert view["density_range"] == overview["density_range"] == [-2, 3]
    assert view["native_window"] == [1, 1, 2, 1]
    np.testing.assert_allclose(view["density"], [[1, 3]])

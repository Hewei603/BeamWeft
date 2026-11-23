"""Science records identify the software that ran, independently of the requested geometry."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from app import solver_receipt as identity
from app.models import GeneratedContent
from app.workbench import optics, tools
from app.workbench.authoring import EditableLayout, layout_receipt
from app.workbench.device_laws import device_catalog


def path(length=50):
    return {"mode":"single_pass", "wavelength_nm":808, "input_radius_mm":.3,
            "elements":[{"name":"Air", "uid":"air", "kind":"space", "length_mm":length},
                        {"name":"Lens", "uid":"lens", "kind":"lens", "focal_mm":100}]}


def test_build_sources_and_code_are_relocatable_but_sensitive_to_physical_tables(tmp_path):
    digests = []
    for name, ending in [("first", b"\n"), ("second", b"\r\n")]:
        root = tmp_path / name
        root.mkdir()
        (root / "study_api.py").write_bytes(b"# study" + ending)
        (root / "materials.json").write_bytes(b'{"n":1.8}')
        digests.append(identity._application_source(root))
    assert digests[0] == digests[1] and digests[0]
    (root / "materials.json").write_bytes(b'{"n":1.9}')
    assert identity._application_source(root) != digests[0]
    def compile_at(source_path):
        namespace = {"__name__":"lab.optics"}
        exec(compile("def coeff(samples): return samples[2:4]", source_path, "exec"), namespace)
        return identity._entry_identity(namespace["coeff"])
    assert compile_at("/a/optics.py") == compile_at("D:/b/optics.py")


def test_installed_provider_manifest_rejects_editable_loader_only(tmp_path, monkeypatch):
    from pathlib import PurePosixPath
    source = tmp_path / "relay.py"
    source.write_text("transfer = 2")
    origin = {}
    dist = SimpleNamespace(version="9", files=[PurePosixPath("relay.py")],
        read_text=lambda _:json.dumps(origin), locate_file=lambda file:tmp_path / str(file))
    monkeypatch.setattr(identity.metadata, "distribution", lambda name:dist)
    read = identity._provider_files.__wrapped__
    first = read("lab 9")
    assert first
    source.write_text("transfer = 3")
    assert read("lab 9") != first
    origin["dir_info"] = {"editable":True}
    assert read("lab 9") is None and read("lab 10") is None


def test_single_and_grid_scans_capture_once_outside_sample_loop(monkeypatch):
    calls = []
    def capture(method, *args, **kwargs):
        calls.append(method)
        return identity.solver_receipt(method, *args, **kwargs)
    monkeypatch.setattr(optics, "solver_receipt", capture)
    result = optics.analyze_system(path())
    other = optics.analyze_system(path(70))
    assert result["solver_receipt"] == other["solver_receipt"]
    assert result["solver_receipt"]["complete"] and len(calls) == 2
    calls.clear()
    axis = {"element_index":0, "parameter":"length_mm", "start":1, "stop":10, "count":4}
    linear = optics.scan_system({"system":path(), **axis})
    mirror = {"kind":"mirror", "name":"M", "radius_mm":200}
    cavity = {"mode":"standing_wave", "wavelength_nm":808, "elements":[mirror, *path()["elements"], mirror]}
    grid = optics.scan_system_2d({"system":cavity, "x":{**axis, "element_index":1},
        "y":{"element_index":2,"parameter":"focal_mm","start":80,"stop":100,"count":3}})
    assert calls == [linear["solver_receipt"]["method"], grid["solver_receipt"]["method"]]
    assert len(linear["points"]) == 4 and len(grid["x"]) == 4 and len(grid["y"]) == 3
    assert all("solver_receipt" not in point for point in linear["points"])


def test_same_version_device_replacement_changes_math_and_opaque_calibration_bypasses_cache(monkeypatch):
    recipe = device_catalog()[0]["aligned_lens_power"]["prescription"]
    element = {"name":"Measured lens", "kind":"registered", "uid":"measured", "prescription":recipe}
    system = path()
    system["elements"] = [element, *system["elements"][:1]]
    model = EditableLayout.model_validate({"system":system})
    name = recipe["study"]
    study, provider = tools.STUDIES[name]
    reference = layout_receipt(model)
    assert reference["complete"] and reference["devices"][0]["complete"]
    baseline = optics.analyze_system(system)
    output = study.element_law.output
    def factory(coefficient):
        def transfer(_):
            return {output:{"matrix_t":[1,0,coefficient,1]}}
        return transfer
    for coefficient in (-.02,-.04):
        monkeypatch.setitem(tools.STUDIES, name, (replace(study, compute=factory(coefficient)), provider))
        receipt = layout_receipt(model)
        fresh = optics.analyze_system(system)
        assert receipt["complete"] and receipt["sha256"] != reference["sha256"]
        assert fresh["planes"]["tangential"]["output"] != baseline["planes"]["tangential"]["output"]
        baseline, reference = fresh, receipt
    calibration = [-.05]
    def adjustable(_):
        return {output:{"matrix_t":[1,0,calibration[0],1]}}
    monkeypatch.setitem(tools.STUDIES, name, (replace(study, compute=adjustable), provider))
    receipt = layout_receipt(model)
    assert not receipt["complete"] and receipt["opaque_state"] and receipt["devices"][0]["opaque_state"]
    assert identity.solver_cache_key(receipt) != identity.solver_cache_key(receipt)
    first = optics.analyze_system(system)["planes"]["tangential"]["output"]["radius_mm"]
    calibration[0] = -.07
    assert optics.analyze_system(system)["planes"]["tangential"]["output"]["radius_mm"] != pytest.approx(first)


def test_semantic_variant_reuse_depends_on_build_and_legacy_revision_stays_unstamped(client, db, monkeypatch):
    case = client.post("/api/cases", json={"title":"Installed solver"}).json()["id"]
    url = f"/api/cases/{case}/optical-project"
    payload = {"title":"Untitled", "system":path()}
    def save_variant():
        res = client.post(url+"/variants", json={"project":payload})
        assert res.is_success, res.text
        return res.json()
    original = save_variant()
    historical = original["analysis"]["solver_receipt"]
    assert historical["complete"] and save_variant()["revision_id"] == original["revision_id"]
    monkeypatch.setattr(identity, "_APPLICATION", {**identity._APPLICATION, "source_sha256":"b"*64})
    next_version = save_variant()
    assert next_version["revision_id"] != original["revision_id"]
    assert next_version["analysis"]["solver_receipt"]["sha256"] != historical["sha256"]
    assert client.get(url, params={"revision_id":original["revision_id"]}).json()["current"]["analysis"]["solver_receipt"] == historical
    old_row = db.get(GeneratedContent, original["revision_id"])
    old_row.content = {**old_row.content, "analysis":{k:v for k,v in old_row.content["analysis"].items() if k != "solver_receipt"}}
    db.commit()
    old = client.get(url, params={"revision_id":old_row.id}).json()["current"]
    assert "solver_receipt" not in old["analysis"]

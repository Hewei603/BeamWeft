import hashlib
import json

import numpy as np
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from app.models import GeneratedContent
from app.open_case import open_package
from app.study_api import ElementLaw, MissingStudyInput, StrictInput, Study
from app.workbench import tools
from app.workbench.studies import run_study
from app.workbench.wave_study import WavePropagation, propagate_field


def space(uid, length, **parameters):
    return dict(name=uid, uid=uid, kind="space", length_mm=length, **parameters)


def layout(elements, radius=None, wavelength=850, **extra):
    return dict(system=dict(mode="single_pass", input_radius_mm=radius, elements=elements,
                            wavelength_nm=wavelength), **extra)


def field(project, **options):
    return propagate_field(WavePropagation(layout=project, **{"samples": 128, **options}))


def test_two_hard_stops_and_three_segments_replay_on_the_consumers_grid():
    stop_a = space("第一孔面", 18, n=1.46, clear_aperture=dict(
        shape="rectangle", width_mm=.49, height_mm=.82, center_t_mm=.075, angle_deg=22))
    lens = dict(name="透镜", uid="L", kind="lens", focal_mm=115)
    stop_b = space("第二孔面", 67, clear_aperture=dict(shape="ellipse", width_mm=.48, center_s_mm=-.07))
    station = dict(uid="inspection", name="玻璃内部", target_uid=stop_a["uid"], depth_mm=11)
    first = field(layout([stop_a, lens], .34, observation_planes=[station]), plane_uid="inspection", window_mm=14)
    saved = first["exit_field"]
    assert saved["plane"] == dict(beam_uid="primary", plane_uid="inspection", name="玻璃内部", z_mm=11)
    assert saved["operations"][0]["sections"] == [dict(kind="medium", length_mm=11., index=1.46)]
    assert len(json.dumps(saved)) < 1300
    # The rest of the same uniform slab has no new entrance stop at the cut plane.
    tail = space("余下玻璃", 7, n=1.46)
    middle = field(layout([tail, lens]), input_field=saved, window_mm=3)
    last = field(layout([stop_b]), input_field=middle["exit_field"], samples=256, window_mm=2.7)
    direct = field(layout([stop_a, lens, stop_b], .34), samples=256, window_mm=2.7)
    assert last["exit_field"]["distance_mm"] == 85
    assert last["exit_field"]["plane"]["z_mm"] == 67
    assert last["exit_field"]["plane"]["name"] == "第二孔面出射面"
    assert [row["z_mm"] for row in last["rows"]] == [-18, 0]
    assert last["rows"][1]["after"] < last["rows"][0]["after"] < 1
    # Splitting a uniform-medium Fresnel transfer changes roundoff only.
    np.testing.assert_allclose(last["grid"]["density"], direct["grid"]["density"], rtol=1e-12, atol=2e-14)
    for metric in ("throughput", "window_power", "shape_change"):
        assert last[metric] == pytest.approx(direct[metric], abs=3e-15)
    assert last["warnings"] == direct["warnings"]
    settings = last["numerics"]
    assert settings["fine_cells_per_padded_side"] == 512
    assert settings["coarse_cells_per_padded_side"] == 256
    assert settings["fine_spacing_mm"] == pytest.approx(2.7 / 256)
    assert settings["padded_width_mm"] == 5.4
    assert settings["quadrature_offsets_ts_cells"] == [[-.25, -.25], [.25, -.25], [-.25, .25], [.25, .25]]


def test_equal_intensities_with_opposite_phase_are_distinguished_downstream():
    flat = space("launch", 0)
    omitted = field(layout([flat], .34))
    assert omitted["throughput"] == pytest.approx(1)
    assert omitted["numerics"]["quadrature_offsets_ts_cells"] == [[0, 0]]
    project = layout([flat], .34)
    starts = []
    for radius in (-75, 75):
        project["system"]["input_curvature_mm"] = radius
        starts.append(field(project))
    np.testing.assert_allclose(starts[0]["grid"]["density"], starts[1]["grid"]["density"], atol=1e-15)
    next_project = layout([space("sample", 64)])
    peaks = [np.max(field(next_project, input_field=start["exit_field"])["grid"]["density"]) for start in starts]
    assert peaks[0] > 20 * peaks[1]
    with pytest.raises(MissingStudyInput):
        field(next_project)
    with pytest.raises(ValidationError):
        field(next_project, input_field=starts[0]["grid"])
    with pytest.raises(ValidationError, match="schema_version"):
        field(next_project, input_field={**omitted["exit_field"], "schema_version": 2})
    with pytest.raises(ValueError, match="真空波长"):
        field(layout([flat], wavelength=532), input_field=omitted["exit_field"])
    with pytest.raises(ValidationError, match="累计距离"):
        field(next_project, input_field={**omitted["exit_field"], "distance_mm": 2})


def test_incident_path_takes_its_wavelength_from_a_registered_field_and_keeps_local_z():
    upstream = space("upstream", 9, clear_aperture=dict(shape="ellipse", width_mm=.7))
    source = field(layout([upstream], .35, wavelength=780))
    arm = space("arm", 18)
    host = space("cell", 38, n=1.6)
    plane = dict(uid="detector", name="样品切面", target_uid="cell", depth_mm=14)
    path = dict(uid="probe", name="探测支路", target_uid="cell", through_uid="cell", elements=[arm])
    station = layout([space("before", 80), host], wavelength=780,
                     incident_paths=[path], observation_planes=[plane])
    chained = field(station, input_field=source["exit_field"], beam_uid="probe", plane_uid="detector")
    direct = field(layout([upstream, arm, space("cut", 14, n=1.6)], .35, wavelength=780))
    assert chained["grid"] == direct["grid"]
    assert chained["exit_field"]["distance_mm"] == 9 + 18 + 14
    assert chained["z_mm"] == 80 + 14
    assert chained["rows"][0]["z_mm"] == 80 - 18 - 9
    # The other-wavelength host cannot supply an unverified material index.
    station["system"]["wavelength_nm"] = 1064
    with pytest.raises(ValueError, match="折射率"):
        field(station, input_field=source["exit_field"], beam_uid="probe", plane_uid="detector")


def test_historical_plugin_phase_remains_executable_after_plugin_removal(monkeypatch):
    class PlateInputs(StrictInput):
        wavelength_nm: float

    law = ElementLaw(representation="ray_matrix", wavelength_input="wavelength_nm",
                     output="phase", scalar_transfer="quadratic_phase")
    recipe = Study("wave_test_plate", "柱面相位板", "1", "在标定波长模拟柱面二次相位", PlateInputs,
        lambda p: {"phase": {"matrix_t": [1, 0, -1/110, 1], "matrix_s": [1, 0, 0, 1]},
                   "warnings": ["只包含标定的二次项"]}, element_law=law)
    monkeypatch.setitem(tools.STUDIES, recipe.name, (recipe, "wave-test"))
    plate = dict(kind="registered", name="柱面板", uid="plate-id", prescription=dict(
        provider="wave-test", study=recipe.name, version="1"))
    out = space("camera", 97)
    reference = field(layout([plate, out], .33), window_mm=3)
    snapshot = field(layout([plate], .33), window_mm=4)["exit_field"]
    assert snapshot["operations"][0]["sections"][0] == dict(kind="phase", c_t_per_mm=-1/110, c_s_per_mm=0)
    assert snapshot["operations"][0]["law"] == dict(study=recipe.name, provider="wave-test", version="1", scope=recipe.scope)
    monkeypatch.delitem(tools.STUDIES, recipe.name)
    downstream = field(layout([out]), input_field=snapshot, window_mm=3)
    np.testing.assert_array_equal(reference["grid"]["density"], downstream["grid"]["density"])
    assert downstream["warnings"] == reference["warnings"] == ["「柱面板」只包含标定的二次项"]
    with pytest.raises(ValueError, match="重算此元件需要"):
        field(layout([plate], .33))


def test_frozen_result_binding_and_legacy_intensity_cannot_masquerade_as_field(client):
    case_id = client.post("/api/cases", json={"title": "续算实验"}).json()["id"]
    root = f"/api/cases/{case_id}/studies"
    name = "propagate_coherent_field"
    study, provider = tools.STUDIES[name]

    def run(project, connections=None, **options):
        return client.post(f"{root}/run/{name}", json={
            "version": study.version, "provider": provider,
            "inputs": {"layout": project, "samples": 128, **options}, "connections": connections or {},
        })

    stop = space("slit", 0, clear_aperture=dict(shape="rectangle", width_mm=.22, height_mm=.66))
    first = run(layout([stop], .34), window_mm=10).json()
    assert first["status"] == "partial"
    record = first["study_record"]
    assert record["available_outputs"]["exit_field"]["quantity"] == "optics.coherent-fresnel-field.v1"
    target = layout([space("detector", 75)])
    source = {"run_id": record["id"], "port": "exit_field"}
    unregistered = run(target, {"input_field": source})
    assert unregistered.status_code == 422
    source["registration"] = "孔径出射面贴合下段入口。两段沿同一展开光轴，T/S 横轴同向。"
    bound = run(target, {"input_field": source}, window_mm=2)
    assert bound.status_code == 200, bound.text
    after = bound.json()
    archived = client.get(f"{root}/runs/{after['study_record']['id']}").json()
    assert archived["inputs"]["input_field"] == first["exit_field"]
    assert archived["connections"]["input_field"] == source
    one_pass = run(layout([stop, *target["system"]["elements"]], .34), window_mm=2).json()
    assert after["grid"] == one_pass["grid"]
    assert after["throughput"] == one_pass["throughput"]
    assert after["warnings"] == one_pass["warnings"]
    assert len(json.dumps(archived["inputs"]["input_field"])) < 1300
    assert run(target, {"input_field": {**source, "port": "grid.density"}}).status_code == 422


def test_legacy_operation_key_is_immutable_and_explicit_recalculation_is_idempotent(client, db):
    case_id = client.post("/api/cases", json={"title": "早期衍射记录"}).json()["id"]
    study, provider = tools.STUDIES["propagate_coherent_field"]
    inputs = WavePropagation(layout=layout([space("relay", 15)], .45), samples=128).model_dump(mode="json")
    del inputs["input_field"]
    old_version = "1"
    identity = json.dumps([study.name, provider, old_version, inputs], sort_keys=True, ensure_ascii=False, allow_nan=False)
    old_key = "study:" + hashlib.sha256(identity.encode()).hexdigest()
    old_definition = study.describe(provider)
    old_definition["version"] = old_version
    old_definition.pop("accepts_inputs_from")
    old_definition["input_ports"].pop("input_field")
    old_definition["output_ports"].pop("exit_field")
    old_definition["inputs"]["properties"].pop("input_field")
    old_output = field(inputs["layout"])
    del old_output["exit_field"]
    del old_output["numerics"]
    old_record = dict(summary=study.title, definition=old_definition, inputs=inputs, output=old_output,
                      connections={}, available_outputs={})
    old = GeneratedContent(case_id=case_id, content_type="scientific_study", operation_key=old_key, content=old_record)
    db.add(old)
    db.commit()
    original_bytes = json.dumps(old.content, sort_keys=True, ensure_ascii=False)
    old_request = client.post(f"/api/cases/{case_id}/studies/run/{study.name}",
                              json={"version": old_version, "provider": provider, "inputs": inputs})
    assert old_request.status_code == 409 and "版本" in old_request.json()["detail"]
    request = {"version": study.version, "provider": provider, "inputs": {}, "reuse_inputs_from": old.id}
    current_url = f"/api/cases/{case_id}/studies/run/{study.name}"
    result = client.post(current_url, json=request)
    assert result.status_code == 200, result.text
    fresh = result.json()
    assert fresh["study_record"]["id"] != old.id
    assert fresh["study_record"]["version"] == study.version == "2"
    assert fresh["study_record"]["input_origin"] == dict(run_id=old.id, name=study.name, provider=provider, version="1")
    assert "exit_field" in fresh["study_record"]["available_outputs"]
    assert client.post(current_url, json=request).json()["study_record"]["id"] == fresh["study_record"]["id"]
    db.refresh(old)
    assert old.operation_key == old_key and json.dumps(old.content, sort_keys=True, ensure_ascii=False) == original_bytes
    assert db.query(GeneratedContent).filter_by(case_id=case_id, content_type="scientific_study").count() == 2


def test_native_project_can_resume_frozen_diffraction_in_an_independent_receiver_db(client, db, tmp_path):
    case_id = client.post("/api/cases", json={"title": "两台工作台交接"}).json()["id"]
    study, provider = tools.STUDIES["propagate_coherent_field"]
    first_part = [space("diaphragm", 32, clear_aperture=dict(
        shape="rectangle", width_mm=.44, height_mm=.6, center_t_mm=.06, angle_deg=-23))]
    second_part = [dict(kind="lens", uid="L", name="relay", focal_mm=92), space("sample", 67)]
    result = run_study(db.get_bind(), case_id, study, provider,
                       {"layout": layout(first_part, .32), "samples": 128, "window_mm": 3.5})
    saved = result["study_record"]["id"]
    package = client.get(f"/api/cases/{case_id}/project-file")
    assert package.status_code == 200 and ".beamweft" in package.headers["content-disposition"]
    file = tmp_path / "衍射工程.beamweft"
    file.write_bytes(package.content)
    folder, marker = open_package(file, tmp_path / "independent")
    assert marker["case_id"] == case_id
    receiver = create_engine(URL.create("sqlite", database=str(folder / "beamweft.db")))
    try:
        with Session(receiver) as peer:
            archived = peer.get(GeneratedContent, saved)
            assert archived.content["output"]["exit_field"] == result["exit_field"]
        source = {"input_field": {"run_id": saved, "port": "exit_field", "registration": "贴合出射面且展开 T/S 同向"}}
        continued = run_study(receiver, case_id, study, provider,
                              {"layout": layout(second_part), "samples": 256, "window_mm": 3}, connections=source)
        direct = field(layout(first_part + second_part, .32), samples=256, window_mm=3)
        assert continued["grid"] == direct["grid"]
        assert continued["throughput"] == direct["throughput"]
        assert continued["study_record"]["connections"] == source
        with Session(receiver) as peer:
            assert peer.get(GeneratedContent, continued["study_record"]["id"]) is not None
        assert db.get(GeneratedContent, continued["study_record"]["id"]) is None
    finally:
        receiver.dispose()

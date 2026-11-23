"""Use the original detector reading to test, without rerunning, two already-frozen models."""
from copy import deepcopy
import math
from pathlib import Path

import pytest

from app.models import Attachment, GeneratedContent
from app.workbench import prediction_comparisons
from app.workbench.prediction_checks import PredictionCheck, check_prediction
from .test_prediction_comparisons import expected, models, project, request


def scenario(client, db, *, station=80, positions=None, bound=5, definition="fwhm_diameter",
             camera=None, first=None, second=None, **changes):
    first = first or project()
    url, rid, other_id = models(client, db, first, second or project(115))
    args = request(rid, positions_mm=positions or [80, 110], definition=definition, **changes)
    if bound is not None:
        args.update(absolute_width_bound=bound, bound_source="相机校准规格")
    response = client.post(f"{url}/{other_id}/prediction-comparisons", json=args)
    assert response.status_code == 200, response.text
    frozen = response.json()
    third = deepcopy(camera or first)
    third.setdefault("observation_planes", [{"uid":"detector", "name":"实际相机位置",
                                             "position_reference":"fixed_z", "z_mm":station}])
    response = client.post(url, json={"project": third, "base_revision_id": rid, "change_note": "登记仪器"})
    assert response.status_code == 200, response.text
    case = int(url.split("/")[3])
    params = {"revision_id":response.json()["revision_id"], "plane_uid":"detector", "beam_uid":"primary",
              "axes_aligned":True, "wavelength_nm":808, "unit":"mm",
              "definition":definition, "locator":"frame 2", "conditions":"相机轴与台架零点已标定。"}

    def observe(value=.31, **change):
        raw = {**params, "tangential":value, **change}
        keys = ("tangential", "sagittal", "unit", "definition", "wavelength_nm")
        payload = (",".join(keys) + "\n" + ",".join(str(raw.get(k, "")) for k in keys) + "\n").encode()
        attach = client.post(f"/api/cases/{case}/attachments", files={"file": ("camera.csv", payload, "text/csv")})
        assert attach.status_code in (200, 201), attach.text
        r = client.post(url + "/beam-readings", json={**raw, "attachment_id":attach.json()["id"]})
        assert r.status_code == 200, r.text
        return r.json()

    def check(observation, **overrides):
        return check_prediction(db, case, PredictionCheck.model_validate({
            "prediction_id":frozen["id"], "reading_id":observation["id"], "registration_note":"同一台架、光束和探头。",
            **overrides}))
    return frozen, observe, check


def test_closed_loop_uses_predeclared_intervals_and_individual_roles_from_a_third_revision(client, db, monkeypatch):
    # Freeze touching intervals. A laboratory result exactly on their common edge supports both.
    factor = 1000 * math.sqrt(2 * math.log(2))
    a, b = [expected(f, 80) * factor for f in (100, 115)]
    edge = (a + b) / 2
    frozen, observe, check = scenario(client, db, bound=abs(a-b)/2)
    monkeypatch.setattr(prediction_comparisons, "_sample_model", lambda *_: pytest.fail("reran a frozen model"))
    measured = observe(edge / 1000)
    a_id, b_id = frozen["reference_revision_id"], frozen["revision_id"]
    roles = {a_id:"model_input", b_id:"held_out"}
    result = check(measured, model_roles=roles)
    assert result["status"] == "completed"
    assert [r["state"] for r in result["outcomes"]] == ["inside","inside"]
    assert [r["predicted"] for r in result["outcomes"]] == pytest.approx([a, b])
    assert [r["residual"] for r in result["outcomes"]] == pytest.approx([edge-a, edge-b])
    assert [r["evidence"]["data_role"] for r in result["outcomes"]] == ["model_input", "held_out"]
    assert result["reading"]["revision_id"] not in {a_id, b_id} and result["measured"] == pytest.approx(edge)
    assert result["source"]["sha256"] == measured["source_sha256"]
    assert result["reading"]["transcribed_by"] == measured["transcribed_by"]
    assert result["prediction_conditions"]["solver_sha256"] == frozen["solver_receipt"]["sha256"]
    assert result["chronology"]["acquired_at"] is None and result["chronology"]["reading_registered_at"]
    assert check(measured, model_roles=roles) == result
    assert db.get(GeneratedContent, frozen["id"]).content["points"] == frozen["points"]
    assert [r["state"] for r in check(observe(a/1000))["outcomes"]] == ["inside", "outside"]
    assert [r["state"] for r in check(observe(b/1000))["outcomes"]] == ["outside", "inside"]
    assert [r["state"] for r in check(observe(1))["outcomes"]] == ["outside", "outside"]
    assert check(measured, model_roles={b_id:"different_configuration"})["outcomes"][1]["residual"] is None
    with pytest.raises(ValueError, match="两个工程版本"):
        check(measured, model_roles={measured["revision_id"]:"held_out"})


def test_public_check_history_keeps_each_decision_with_its_own_prediction(client, db):
    frozen, observe, _ = scenario(client, db)
    case = db.get(GeneratedContent, frozen["id"]).case_id
    endpoint = f"/api/cases/{case}/optical-project/prediction-checks"
    request_data = {"prediction_id": frozen["id"], "reading_id": observe()["id"], "registration_note": "同一探头工位"}
    first = client.post(endpoint, json=request_data).json()
    assert client.post(endpoint, json=request_data).json()["id"] == first["id"]
    second = client.post(endpoint, json={**request_data, "model_roles": {str(frozen["revision_id"]): "model_input"}}).json()
    assert second["id"] != first["id"]
    params = {"prediction_id": frozen["id"]}
    assert [row["id"] for row in client.get(endpoint, params=params).json()["items"]] == [second["id"], first["id"]]
    assert client.get(endpoint, params={**params, "before_id": second["id"]}).json()["items"] == [first]
    unrelated = client.post("/api/cases", json={"title": "Other bench"}).json()["id"]
    assert client.get(f"/api/cases/{unrelated}/optical-project/prediction-checks", params=params).status_code == 404


def test_converts_geometric_radius_and_diameter_but_preserves_field_definition(client, db):
    frozen, observe, check = scenario(client, db, definition="diameter_1e2", bound=None)
    reading = observe(.31, definition="radius_1e2")
    result = check(reading)
    assert result["measured"] == pytest.approx(620)
    assert result["reading"]["tangential"] == .31 and result["reading"]["unit"] == "mm"
    assert result["definition"] == "diameter_1e2" and result["reading"]["definition"] == "radius_1e2"
    for index, outcome in enumerate(result["outcomes"]):
        f = [100,115][index]
        assert outcome["state"] == "unknown" and outcome["interval"] is None
        assert outcome["residual"] == pytest.approx(620 - 2000 * expected(f,80))
    for changes, reason in (({"definition":"fwhm_diameter"}, "宽度定义"),
                            ({"axes_aligned":False}, "配准"), ({"wavelength_nm":1064}, "波长"),
                            ({"tangential":None,"sagittal":.3}, "方向")):
        report = check(observe(**changes))
        assert report["status"] == "needs_input"
        assert all(o["residual"] is None and reason in o["reason"] for o in report["outcomes"])


def test_frozen_position_and_instrument_bounds_are_applied_once(client, db):
    _, observe, check = scenario(client, db, bound=1., position_bound={"plus_minus_mm":5, "source":"导轨定位检定"})
    factor = 1000 * math.sqrt(2 * math.log(2))
    # This caustic decreases over the accessible interval [75,85] mm.
    low, high = expected(100,85)*factor - 1, expected(100,75)*factor + 1
    inside = check(observe(high / 1000))
    outside = check(observe((high + .1) / 1000))
    assert inside["outcomes"][0]["interval"] == pytest.approx([low,high])
    assert inside["outcomes"][0]["state"] == "inside" and outside["outcomes"][0]["state"] == "outside"
    assert inside["prediction_conditions"]["position_bound"]["source"] == "导轨定位检定"


def test_unmatched_or_ambiguous_station_does_not_interpolate_or_select_the_nearest(client, db):
    for positions in ([79,81], [80, math.nextafter(80., 100)]):
        _, observe, check = scenario(client, db, positions=positions)
        result = check(observe())
        assert all(o["residual"] is None and o["state"] == "unknown" and "冻结的预测" in o["reason"]
                   for o in result["outcomes"])
        assert result["source"]["path"] == "camera.csv"


def test_fixed_plane_verifies_sample_side_and_medium_for_each_model(client, db):
    other = project(115)
    other["system"]["elements"][-1]["n"] = 1.5
    _, observe, check = scenario(client, db, second=other)
    report = check(observe())
    assert report["outcomes"][0]["residual"] is not None
    assert report["outcomes"][1]["residual"] is None and "介质" in report["outcomes"][1]["reason"]
    at_face = project()
    at_face["observation_planes"] = [{"uid":"detector", "name":"透镜前方", "target_uid":"in", "depth_mm":30}]
    _, observe, check = scenario(client, db, camera=at_face, positions=[30], boundary_side="downstream")
    assert all("界面" in row["reason"] and row["residual"] is None for row in check(observe())["outcomes"])


def test_changed_original_and_cross_case_evidence_are_rejected_even_after_a_saved_check(client, db):
    frozen, observe, check = scenario(client, db)
    reading = observe()
    check(reading)
    attachment = db.get(Attachment, reading["attachment_id"])
    with pytest.raises(ValueError, match="本工程"):
        check_prediction(db, attachment.case_id + 100, PredictionCheck(prediction_id=frozen["id"], reading_id=reading["id"],
                                                                       registration_note="other workspace"))
    Path(attachment.filepath).write_bytes(b"changed evidence")
    with pytest.raises(ValueError):
        check(reading)


def test_reading_only_needs_registered_geometry_in_the_third_revision(client, db):
    camera = project()
    del camera["system"]["input_radius_mm"]
    _, observe, check = scenario(client, db, camera=camera)
    reading = observe()
    registration = db.get(GeneratedContent, reading["revision_id"])
    # Old portable projects can store valid instruments without section analysis.
    registration.content = {**registration.content, "analysis": {"planes": {}}}
    db.commit()
    assert all(row["residual"] is not None for row in check(reading)["outcomes"])


def test_branch_coordinates_and_index_are_specific_to_the_acquired_beam(client, db):
    host = project()
    host["system"]["wavelength_nm"] = 1064
    host["incident_paths"] = [{"uid":"arm","name":"支路","target_uid":"out","through_uid":"out",
                               "target_n":1.4, "wavelength_nm":808, "elements":project()["system"]["elements"],
                               "tangential":{"m2":1,"wavefront":"waist","radius_mm":.45}}]
    camera = deepcopy(host)
    del camera["incident_paths"][0]["target_n"]
    _, observe, check = scenario(client, db, first=host, second=host, camera=camera, beam_uid="arm")
    reading = observe(beam_uid="arm")
    registration = db.get(GeneratedContent, reading["revision_id"])
    registration.content = {**registration.content, "analysis": {"planes": {}}}
    db.commit()
    result = check(reading)
    assert all("介质" in row["reason"] and row["residual"] is None for row in result["outcomes"])

    # The depth 50 mm exists in two different segments of the same beam.
    # One lies in the incident train, the other after it has entered the main train.
    _, observe, check = scenario(client, db, first=host, second=host, camera=host, positions=[50],
                                 beam_uid="arm", region="incident_train", position_reference="element_depth",
                                 element_uid="out")
    result = check(observe(beam_uid="arm"))
    assert all("接入前支路" in row["reason"] and row["residual"] is None for row in result["outcomes"])


def test_incomplete_historical_fit_provenance_changes_evidence_role_and_keeps_frozen_numbers(client, db):
    from app.workbench.beam_studies import fit_evidence_key

    frozen, observe, check = scenario(client, db)
    reading = observe()
    record = db.get(GeneratedContent, frozen["revision_id"])
    fit = GeneratedContent(case_id=record.case_id, content_type="beam_caustic_study", content={})
    db.add(fit)
    db.flush()
    source_file = {"attachment_id":reading["attachment_id"], "sha256":reading["source_sha256"]}
    point = {"reading_id":reading["id"], "attachment_id":reading["attachment_id"], "locator":"frame 2"}
    source = {"kind":"registered", "reading_ids":[reading["id"]]}
    source_axis = {"wavefront":"waist","radius_mm":.45,"m2":1.}
    original = deepcopy(record.content)

    def verdict(*, files, points, recipe=source, keyed=True):
        content = {"source":recipe, "metadata":{"files":files}, "points":points}
        fit.content = content
        origin = {"study_id":fit.id, "measured_axis":"T", "coordinate_mm":0., "registration":"旧档案",
                  "width_basis":"gaussian_equivalent", "wavelength_nm":808, "radius_mm":.45,
                  "curvature_mm":None, "m2":1.}
        if keyed:
            origin["study_key"] = fit_evidence_key(content)
        changed = deepcopy(original)
        system = changed["project"]["system"]
        system.pop("input_radius_mm", None)
        system["source"] = {"tangential":{**source_axis, "fit_origin":origin}}
        record.content = changed
        db.commit()
        report = check(reading, data_role="held_out")
        candidate = report["outcomes"][1]
        assert candidate["predicted"] == frozen["points"][0]["target"]["width"]
        assert candidate["residual"] is not None and report["outcomes"][0]["evidence"]["data_role"] == "held_out"
        return candidate["evidence"]

    fresh_hash = "a" * 64
    other = {**point, "reading_id":999, "attachment_id":22}
    other_file = {"attachment_id":22, "sha256":fresh_hash}
    expected = {"kind":"registered","reading_ids":[999,1000]}
    for evidence in (
        verdict(files=[source_file], points=[point], keyed=False),
        verdict(files=[], points=[point]),
        verdict(files=None, points=[point]),
        verdict(files=[{}], points=[point]),
        verdict(files=[other_file], points=[other], recipe=expected),
        verdict(files=[other_file], points=[other, {"reading_id":1000}], recipe=expected),
        verdict(files=[source_file], points=[{**point, "locator":"第 2 帧"}]),
    ):
        assert evidence["data_role"] == "unspecified" and evidence["unverified_fit_ids"] == [fit.id]
    reused = verdict(files=[source_file], points=[point, {}], recipe={**source, "reading_ids":[reading["id"], 1000]})
    assert reused["data_role"] == "model_input" and reused["fit_input_ids"] == [fit.id]
    independent = verdict(files=[other_file], points=[other], recipe={"kind":"registered","reading_ids":[999]})
    assert independent["data_role"] == "held_out" and not independent["unverified_fit_ids"]

"""Coating tool: dual-mode contract and the canonical 1123/1064 archetype case."""
import math
import pytest

from app.physics.coating_tool import evaluate_coating


def test_needs_input_when_empty():
    assert evaluate_coating({})["status"] == "needs_input"


def test_replaying_a_v1_receipt_keeps_its_original_result_and_requests_missing_media(client, db):
    from app.models import GeneratedContent

    case = client.post("/api/cases", json={"title":"薄膜边界复算"}).json()["id"]
    inputs = {"config":{"query_wavelengths_nm":[633], "stack":{"incident":"air","layers":[{"n":2.1,"thickness_nm":100}]}}}
    original = {"status":"completed","mode_used":"exact_stack","confidence":"high","query_results":[{"wavelength_nm":633,"R":.21012}]}
    historical = GeneratedContent(case_id=case, content_type="scientific_study", content={
        "definition":{"name":"compute_coating","provider":"LaserClaw","version":"1"},
        "inputs":inputs, "connections":{}, "output":original,
    })
    db.add(historical)
    db.commit()
    base = f"/api/cases/{case}/studies"
    response = client.post(base + "/run/compute_coating", json={
        "version":"2","provider":"LaserClaw","inputs":{},"reuse_inputs_from":historical.id,
    })
    assert response.status_code == 200, response.text
    replay = response.json()
    assert replay["status"] == "needs_input" and "基底" in replay["message"]
    assert replay["study_record"]["provider"] == replay["study_record"]["solver_receipt"]["provider"] == "BeamWeft"
    assert replay["study_record"]["input_origin"]["run_id"] == historical.id
    assert client.get(f"{base}/runs/{historical.id}").json()["output"] == original


def test_exact_stack_mode_is_high_confidence():
    ns_ar = math.sqrt(1.4496)
    d = 1064.0 / (4 * ns_ar)
    req = {
        "mode": "auto",
        "query_wavelengths_nm": [1064.0],
        "stack": {"incident": "air", "substrate": "fused_silica",
                  "layers": [{"thickness_nm": d, "n": ns_ar}]},
    }
    res = evaluate_coating(req)
    assert res["status"] == "completed"
    assert res["mode_used"] == "exact_stack"
    assert res["confidence"] == "high"
    assert res["query_results"][0]["R"] < 1e-6
    assert res["query_results"][0]["is_representative_only"] is False


@pytest.mark.parametrize(("stack", "issue"), [
    ({"layers":[],"incident":"air"}, "基底介质"),
    ({"layers":[],"substrate":"fused_silica"}, "入射介质"),
    ({"incident":"air","substrate":"fused_silica"}, "裸界面"),
    ({"incident":"air","substrate":"private-glass","layers":[]}, "private-glass"),
    ({"incident":1,"substrate":1.8,"layers":[{"thickness_nm":98}]}, "第 1 层"),
])
def test_incomplete_stack_names_the_missing_physical_input(stack, issue):
    result = evaluate_coating({"stack":stack, "query_wavelengths_nm":[633]})
    assert result["status"] == "needs_input" and issue in result["message"]
    assert not result.get("query_results")


def test_user_indices_and_a_measured_curve_do_not_require_catalog_materials():
    config = {"query_wavelengths_nm":[500], "stack":{"incident":1.4, "substrate":{"n":1.6,"k":0.12}, "layers":[]}}
    row = evaluate_coating(config)["query_results"][0]
    assert row["R"] == pytest.approx(abs((1.4-(1.6-.12j))/(1.4+(1.6-.12j)))**2, abs=5.1e-7)
    config["stack"]["substrate"] = None
    config["vendor_curve"] = {"wl_nm":[490,510],"R":[.2,.4]}
    measured = evaluate_coating(config)
    assert measured["mode_used"] == "vendor_curve" and measured["query_results"][0]["R"] == .3
    assert measured["warnings"]
    assert evaluate_coating({**config,"mode":"exact_stack"})["status"] == "needs_input"


def test_dispersion_range_and_representative_material_control_the_result_per_wavelength():
    config = {"query_wavelengths_nm":[633,8000],"stack":{"incident":"air","substrate":"BK7",
              "layers":[{"thickness_nm":75,"material":"ta2o5_film"}]}}
    result = evaluate_coating(config)
    good, outside = result["query_results"]
    assert result["status"] == "partial" and result["confidence"] == "low"
    assert 0 < good["R"] < 1 and good["is_representative_only"]
    assert all(outside[key] is None for key in ("R","T","A"))
    assert "nbk7" in outside["reason"] and "ta2o5_film" in outside["reason"]
    assert result["material_sources"]["nbk7"]["source"] and result["material_sources"]["ta2o5_film"]["locations"] == ["第 1 层"]
    invalid = evaluate_coating({**config,"query_wavelengths_nm":[8000]})
    assert invalid["status"] == "needs_input" and invalid["confidence"] == "unknown" and invalid["message"]


def test_loss_of_a_custom_film_is_used_and_ambiguous_or_invalid_models_are_explained():
    def run(layer):
        return evaluate_coating({"query_wavelengths_nm":[633],"stack":{"incident":1,"substrate":1.46,"layers":[layer]}})
    geometry = {"thickness_nm":85}
    lossy = run({**geometry,"n":2.05,"k":.11})
    assert lossy["query_results"][0]["A"] > .05
    assert run({**geometry,"n":"2.05-0.11j"})["query_results"] == lossy["query_results"]
    assert run({**geometry,"n":2.05,"material":"nbk7"})["status"] == "failed"
    assert run({**geometry,"n":2.05,"k":-.1})["status"] == "failed"


def test_vendor_curve_interpolates_and_nulls_outside_range():
    req = {
        "query_wavelengths_nm": [1000.0, 1500.0],
        "vendor_curve": {"wl_nm": [900, 1100, 1300], "R": [0.99, 0.995, 0.98], "source": "datasheet"},
    }
    res = evaluate_coating(req)
    assert res["mode_used"] == "vendor_curve"
    assert res["status"] == "partial"
    assert res["query_results"][0]["R"] is not None      # 1000 in range
    assert res["query_results"][1]["R"] is None           # 1500 out of measured range
    outside = evaluate_coating({**req,"query_wavelengths_nm":[1500]})
    assert outside["status"] == "needs_input" and outside["confidence"] == "unknown"
    assert "900–1300 nm" in outside["query_results"][0]["reason"]


def test_archetype_1123_hr_covers_1064():
    res = evaluate_coating({
        "nominal": {"function": "HR", "design_wavelength_nm": 1123.0},
        "query_wavelengths_nm": [1064.0],
        "aoi_deg": 0.0,
    })
    assert res["status"] == "completed"
    assert res["mode_used"] == "archetype"
    assert res["confidence"] == "low"
    assert res["recommend_measurement"] is True
    sb = res["stopband"]
    assert sb["edges_nm"][0] < 1064.0 < sb["edges_nm"][1]   # 1064 inside the stopband
    q = res["query_results"][0]
    assert q["band_position"] == "inside"
    assert q["margin_to_edge_nm"] > 0
    assert q["is_representative_only"] is True
    # honest bracket present
    assert sb["width_bracket_nm"][0] < sb["width_nm"] < sb["width_bracket_nm"][1] or \
        sb["width_bracket_nm"][0] <= sb["width_nm"] <= sb["width_bracket_nm"][1]


def test_archetype_aoi_blueshifts_center():
    res = evaluate_coating({
        "nominal": {"function": "HR", "design_wavelength_nm": 1123.0},
        "query_wavelengths_nm": [1064.0],
        "aoi_deg": 45.0,
    })
    assert res["aoi"]["center_shift_nm"] < -20.0            # blue shift is negative and sizeable
    assert res["stopband"]["edges_s_nm"] is not None
    assert res["stopband"]["edges_p_nm"] is not None


def test_archetype_far_wavelength_is_outside():
    res = evaluate_coating({
        "nominal": {"function": "HR", "design_wavelength_nm": 1064.0},
        "query_wavelengths_nm": [532.0],
    })
    q = res["query_results"][0]
    assert q["band_position"] == "outside"
    assert q["margin_to_edge_nm"] < 0                       # negative = outside the band

"""Independent field integration, optical invariants and case-scoped workbench entry points."""

import asyncio
import math
from copy import deepcopy

import numpy as np
import pytest

from app.models import AgentTask, ExperimentCase, GeneratedContent
from app.workbench.mode_overlap import ModeOverlap, evaluate_overlap
from app.workbench.observation import OpticalLayout
from app.workbench.projects import SaveProject, save_project
from app.workbench.study_inputs import WorkingDraft, capture_snapshot, basis
from app.workbench.tools import execute

WAVELENGTH = 0.001064
AXES = ("tangential", "sagittal")


def layout():
    return {
        "system": {
            "mode": "single_pass",
            "input_radius_mm": 0.25,
            "wavelength_nm": 1064,
            "elements": [{"uid": "cell", "name": "介质", "kind": "space", "length_mm": 120, "n": 1.65}],
        },
        "incident_paths": [
            {
                "uid": "probe",
                "name": "参考束",
                "target_uid": "cell",
                "target_n": 1.65,
                "wavelength_nm": 1064,
                "tangential": {"radius_mm": 0.25, "m2": 1, "wavefront": "waist"},
                "elements": [],
            }
        ],
        "observation_planes": [
            {"uid": "front", "name": "入口", "target_uid": "cell", "depth_mm": 0},
            {"uid": "inside", "name": "内截面", "target_uid": "cell", "depth_mm": 75},
        ],
    }


def solve(raw, plane="front", first="primary", second="probe"):
    query = ModeOverlap(plane_uid=plane, first_beam_uid=first, second_beam_uid=second)
    return evaluate_overlap(OpticalLayout.model_validate(raw), None, query)


def test_saved_section_pair_preserves_registration_physics_and_partial_outputs(db, client):
    from app.workbench.mode_overlap import InspectModeOverlap

    case = ExperimentCase(title="Shared station", cavity_type="custom", goal="Compare both modes")
    db.add(case)
    db.commit()
    source = layout()
    source["incident_paths"][0]["launch_ray"] = {"t_offset_mm": 0.0}
    scope = "The input beam is a design assumption."
    model = save_project(db, case.id, SaveProject(project={"title": case.title, **source,
                         "component_context":{"scope_warnings":[scope]}}, change_note="Saved axes"))
    root = f"/api/cases/{case.id}/studies"
    definition = client.get(root + "/catalog/compare_section_modes").json()
    selection = {"plane_uid": "front", "first_beam_uid": "primary", "second_beam_uid": "probe"}
    ref = {"revision_id": model["revision_id"], "port": "v1/layout",
           "registration": "采用工程中登记的同一观察面和两束光的共向 T/S 坐标。"}
    recipe = {"inputs": selection, "connections": {"layout": ref}, "version": definition["version"],
              "provider": definition["provider"]}
    saved = client.post(root + "/run/compare_section_modes", json=recipe)
    assert saved.status_code == 200, saved.text
    assert scope in saved.json()["warnings"]
    record = saved.json()["study_record"]
    task = AgentTask(case_id=case.id, mode="workbench", status="running", goal="保存同一截面的比较")
    db.add(task)
    db.commit()
    result = asyncio.run(execute("analyze_mode_overlap", InspectModeOverlap(**selection, save_result=True),
        db=db, case=case, task=task, provider=None, session_id=None, attachment_ids=[], working=basis(model)))
    assert result["study_record"]["id"] == record["id"]
    assert record["connections"] == {"layout": ref}
    assert record["available_outputs"]["intensity.normalized_product_per_mm2"]["scale"] == 1e6
    ids = record["available_outputs"]["coherent.power_fraction"]["condition_values"]
    assert ids["optics.observation-plane.uid"] == "front" and ids["optics.observation-mount"] == "element:cell"
    assert ids["optics.reference-beam.uid"] == "primary" and ids["optics.compared-beam.uid"] == "probe"
    search = client.post(root + "/search", json={"run_id": record["id"], "registration": "Same station and two physical beams",
        "axes": [{"paths": [["layout", "incident_paths", 0, "wavelength_nm"]], "samples": [[1064], [808]]}],
        "measures": [{"output": "intensity.cosine", "direction": "max"},
                     {"output": "coherent.power_fraction", "direction": "max"}]})
    assert search.status_code == 200, search.text
    grid = search.json()
    assert grid["rows"][0]["state"] == "feasible" and grid["rows"][1]["state"] == "unavailable"
    assert "波长不同" in grid["rows"][1]["measure_issues"]["coherent.power_fraction"]
    assert grid["rows"][1]["values"]["intensity.cosine"] == pytest.approx(1)
    revised = client.post(root + "/reassess", json={"search_id":grid["id"],
        "measures":[{"output":"intensity.cosine", "direction":"max"}]}).json()
    assert revised["preferred_indices"] == [0, 1]
    assert [r["run_id"] for r in revised["rows"]] == [r["run_id"] for r in grid["rows"]]

    unphased = deepcopy(source)
    unphased["incident_paths"][0]["tangential"]["wavefront"] = "unknown"
    result = client.post(root + "/run/compare_section_modes", json={**recipe, "connections": {},
                         "inputs": {**selection, "layout": unphased}}).json()
    assert result["status"] == "partial"
    assert set(result["study_record"]["available_outputs"]) == {"intensity.cosine", "intensity.normalized_product_per_mm2"}
    assert "波前" in result["output_assessments"]["coherent.power_fraction"]["message"]


def test_equal_spots_can_match_half_or_all_of_the_spatial_mode():
    data = layout()
    assert solve(data)["coherent"]["power_fraction"] == pytest.approx(1)
    source = data["incident_paths"][0]["tangential"]
    radius = math.pi * 0.25**2 / (2 * WAVELENGTH)
    source.update(wavefront="curved", curvature_mm=radius)
    value = solve(data)
    assert value["intensity"]["cosine"] == pytest.approx(1)
    assert value["intensity"]["normalized_product_per_mm2"] == pytest.approx(1 / (math.pi * 0.25**2))
    assert value["coherent"]["power_fraction"] == pytest.approx(0.5)
    assert solve(data, "inside")["coherent"]["power_fraction"] == pytest.approx(0.5)
    assert solve(data, "inside")["intensity"]["cosine"] < 1


def test_displaced_tilted_astigmatic_modes_match_independent_complex_field_quadrature():
    data = layout()
    p = data["incident_paths"][0]
    p["tangential"].update(radius_mm=0.19, wavefront="curved", curvature_mm=170)
    p["sagittal"] = {"radius_mm": 0.33, "wavefront": "curved", "curvature_mm": -230, "m2": 1}
    p["launch_ray"] = {"t_offset_mm": 0.12, "s_offset_mm": -0.07, "t_angle_mrad": 0.9, "s_angle_mrad": -0.6}
    out = solve(data)
    integrals = []
    for w, r, d, angle in ((0.19, 170, 0.12, 0.0009), (0.33, -230, -0.07, -0.0006)):
        x = np.linspace(-3, 3, 48001)
        a = np.exp(-((x / 0.25) ** 2))
        b = np.exp(-(((x - d) / w) ** 2) + 1j * (2 * math.pi / WAVELENGTH) * (angle * (x - d) + (x - d) ** 2 / (2 * r)))
        # Integrate the fields and intensities independently of the product-Gaussian implementation.
        dx = x[1] - x[0]

        def integrate(y):
            return np.sum(y) * dx

        power1, power2 = integrate(abs(a) ** 2), integrate(abs(b) ** 2)
        eta = abs(integrate(a.conjugate() * b)) ** 2 / (power1 * power2)
        product = integrate(abs(a) ** 2 * abs(b) ** 2)
        cosine = product / math.sqrt(integrate(abs(a) ** 4) * integrate(abs(b) ** 4))
        integrals.append((eta, cosine, product / (power1 * power2)))
    eta, cosine, density = np.prod(integrals, axis=0)
    assert out["coherent"]["power_fraction"] == pytest.approx(eta, rel=2e-11)
    assert out["intensity"]["cosine"] == pytest.approx(cosine, rel=2e-11)
    assert out["intensity"]["normalized_product_per_mm2"] == pytest.approx(density, rel=2e-11)
    inside = solve(data, "inside")
    assert inside["coherent"]["power_fraction"] == pytest.approx(eta, rel=2e-11)
    assert inside["beams"][1]["field_axes"]["tangential"]["center_mm"] == pytest.approx(0.12 + 75 * 0.0009 / 1.65)
    assert solve(data, first="probe", second="primary")["coherent"]["power_fraction"] == pytest.approx(eta)


def test_cavity_and_two_independent_incident_colours_share_one_registered_segment():
    data = layout()
    data["system"].update(
        mode="standing_wave",
        elements=[
            {"name": "M1", "uid": "m1", "kind": "mirror"},
            *data["system"]["elements"],
            {"name": "M2", "uid": "m2", "kind": "mirror", "radius_mm": 300},
        ],
    )
    other = deepcopy(data["incident_paths"][0])
    other.update(uid="pump", wavelength_nm=808, target_n=1.7)
    other["launch_ray"] = {"t_offset_mm": 0.20, "t_angle_mrad": 1.0}
    other["elements"] = [
        {"name": "空气", "uid": "g", "kind": "space", "length_mm": 20},
        {"name": "透镜", "uid": "l", "kind": "lens", "focal_mm": 50},
    ]
    data["incident_paths"].append(other)
    same = solve(data, "inside")
    assert 0 < same["coherent"]["power_fraction"] < 1
    different = solve(data, "inside", first="probe", second="pump")
    assert different["status"] == "completed" and different["coherent"]["status"] == "not_applicable"
    assert "power_fraction" not in different["coherent"] and different["intensity"]["cosine"] > 0
    ray = different["beams"][1]["field_axes"]["tangential"]
    p = 0.001 - (0.2 + 20 * 0.001) / 50
    assert ray["reduced_angle"] == pytest.approx(p)
    assert ray["center_mm"] == pytest.approx(0.22 + 75 * p / 1.7)


@pytest.mark.parametrize(
    "case",
    [
        "unknown_phase",
        "unknown_angle",
        "unknown_position",
        "missing_index",
        "different_index",
        "multimode",
        "disconnected",
    ],
)
def test_partial_and_unavailable_results_do_not_become_coupling_claims(case):
    data = layout()
    p = data["incident_paths"][0]
    if case == "unknown_phase":
        p["tangential"]["wavefront"] = "unknown"
    if case == "unknown_angle":
        p["launch_ray"] = {"t_angle_mrad": None}
    if case == "unknown_position":
        p["launch_ray"] = {"s_offset_mm": None}
    if case == "missing_index":
        p["target_n"] = None
    if case == "different_index":
        p["target_n"] = 1.4
    if case == "multimode":
        p["tangential"]["m2"] = 1.6
    if case == "disconnected":
        p["target_uid"] = "absent"
    value = solve(data)
    assert "power_fraction" not in value["coherent"]
    if case == "unknown_phase":
        assert "参考束的 T/S 波前曲率" in value["coherent"]["note"] and "指向" not in value["coherent"]["note"]
    if case == "unknown_angle":
        assert "参考束的 T 指向" in value["coherent"]["note"] and "曲率" not in value["coherent"]["note"]
    if case in {"unknown_phase", "unknown_angle", "missing_index", "different_index"}:
        assert value["intensity"]["cosine"] == pytest.approx(1)
    else:
        assert "cosine" not in value["intensity"]


def test_exact_entrance_with_thin_lens_preserves_width_but_changes_phase():
    data = layout()
    f = math.pi * 0.25**2 / (2 * WAVELENGTH)
    data["incident_paths"][0]["elements"] = [{"name": "薄透镜", "kind": "lens", "focal_mm": f}]
    result = solve(data)
    assert result["intensity"]["cosine"] == pytest.approx(1)
    assert result["coherent"]["power_fraction"] == pytest.approx(0.5)
    assert result["beams"][1]["field_axes"]["tangential"]["reduced_vergence_per_mm"] == pytest.approx(-1 / f)


def test_overlap_surfaces_selected_model_calibration_and_large_angle_approximation():
    data = layout()
    p = data["incident_paths"][0]
    p["launch_ray"] = {"t_angle_mrad": 500}
    p["elements"] = [
        {
            "name": "参考透镜",
            "kind": "lens",
            "focal_mm": 100,
            "component_origin": {
                "model_revision_id": 7,
                "version": 1,
                "name": "标定透镜",
                "instance_uid": "lens",
                "ordinal": 1,
                "wavelength_nm": 532,
            },
        }
    ]
    warnings = solve(data)["scope_warnings"]
    assert any("532 nm" in line and "1064 nm" in line for line in warnings)
    assert any("近轴" in line and "mrad" in line for line in warnings)


def test_draft_history_and_manual_agent_entry_points_preserve_project_and_agree(client, db):
    case = ExperimentCase(title="比较截面", goal="几何验证", cavity_type="other")
    other = ExperimentCase(title="其他案例", goal="验证工程隔离", cavity_type="other")
    db.add_all([case, other])
    db.commit()
    raw = {"title": "参考工程", **layout()}
    first = save_project(db, case.id, SaveProject(project=raw, change_note="原版本"))
    # Simulate a pre-upgrade record without local wavefront and ray states.
    legacy = db.get(GeneratedContent, first["revision_id"])
    content = deepcopy(legacy.content)
    for p in content["analysis"]["sections"]["planes"]:
        for beam in p["beams"]:
            for key in ("index", "field_axes", "launch_basis"):
                beam.pop(key, None)
    legacy.content = content
    db.commit()
    api = f"/api/cases/{case.id}/optical-project/mode-overlap"
    query = {"plane_uid": "inside", "second_beam_uid": "probe"}
    old = client.post(api, json={**query, "revision_id": first["revision_id"]})
    assert old.status_code == 200, old.text
    assert old.json()["coherent"]["power_fraction"] == pytest.approx(1)
    raw["incident_paths"][0]["launch_ray"] = {"t_offset_mm": 0.25}
    trial = capture_snapshot(
        db,
        case.id,
        WorkingDraft(project=raw, base_revision_id=first["revision_id"], source_revision_id=first["revision_id"]),
    )
    task = AgentTask(case_id=case.id, status="running", mode="workbench", goal="评估草稿")
    db.add(task)
    db.commit()
    direct = client.post(api, json={**query, "revision_id": trial["revision_id"]}).json()
    computed = asyncio.run(
        execute(
            "analyze_mode_overlap",
            ModeOverlap(**query),
            db=db,
            case=case,
            task=task,
            session_id=None,
            attachment_ids=[],
            provider=None,
            working=basis(trial),
        )
    )
    assert computed == direct and direct["coherent"]["power_fraction"] == pytest.approx(math.exp(-1))
    assert direct["revision_id"] == trial["revision_id"]
    assert (
        client.post(
            f"/api/cases/{other.id}/optical-project/mode-overlap", json={**query, "revision_id": trial["revision_id"]}
        ).status_code
        == 422
    )
    assert client.post(api, json={**query, "second_beam_uid": "missing"}).status_code == 422
    assert db.query(GeneratedContent).filter_by(case_id=case.id, content_type="optical_project").count() == 1

import pytest

from app.design.solver import solve_design
from app.models import GeneratedContent
from app.workbench.authoring import analyze_editable
from app.workbench.design_import import saved_geometry
from app.workbench.projects import OpticalProject, SaveProject, read_project, save_project


def test_saved_scan_row_opens_independently_and_can_be_retried(client, db):
    cid = client.post("/api/cases", json={"title": "独立光路"}).json()["id"]
    official = save_project(db, cid, SaveProject(change_note="原有单程工作", project=OpticalProject(
        title="已有光路", system={"mode": "single_pass", "elements": [], "wavelength_nm": 780})))
    inputs = dict(cavity_type="linear", laser_wavelength_nm=940, input_mirror_flat=True, output_roc_mm=200,
                  crystal_index=1.62, crystal_length_mm=7, crystal_front_mm=3.5, min_output_gap_mm=21,
                  max_output_gap_mm=31)
    design = solve_design({k: {"value": v} for k, v in inputs.items()}, [], assumption_policy="evidence_only")
    content = GeneratedContent(case_id=cid, content_type="plan", content={"engineering_design": design})
    db.add(content)
    db.commit()
    endpoint = f"/api/cases/{cid}/design/{content.id}/import-project"
    rank = 8
    opened = client.post(endpoint, json={"candidate": rank})
    assert opened.status_code == 200, opened.text
    result = opened.json()
    row = design["evidence_assessment"]["cold_cavity"]["rows"][rank - 1]
    assert result["project"]["system"]["elements"][-2]["length_mm"] == row["output_gap_mm"]
    assert result["project"]["system"]["elements"][2]["medium_role"] is None
    assert all(p["ownership"] != "owned" for p in result["project"]["parts"])
    assert result["analysis"]["planes"]["tangential"]["m"] == pytest.approx(row["stability_m"])
    assert result["project"]["component_context"]["design_origin"] == {
        "content_id": content.id, "selection": rank, "collection": "cold_cavity.rows", "assumption_policy": "evidence_only"}
    assert result["model_kind"] == "draft" and result["base_revision_id"] == official["revision_id"]
    assert client.post(endpoint, json={"candidate": rank}).json()["revision_id"] == result["revision_id"]
    assert read_project(db, cid)["project"] == official["project"]
    assert client.post(endpoint, json={"candidate": 1000}).status_code == 422
    other = client.post("/api/cases", json={"title": "另一工程"}).json()["id"]
    assert client.post(f"/api/cases/{other}/design/{content.id}/import-project", json={"candidate": 1}).status_code == 404


COUPLED = dict(cavity_type="linear", input_mirror_flat=True, output_roc_mm=210, laser_wavelength_nm=1030,
               crystal_index=1.64, crystal_front_mm=15, crystal_length_mm=12, front_cap_mm=2, active_length_mm=7,
               rear_cap_mm=3, output_gap_mm=60, pump_wavelength_nm=940, pump_index=1.71, collimator_f_mm=45,
               focus_f_mm=32, source_z_mm=-150, collimator_z_mm=-101, focus_lens_z_mm=-34,
               pump_exit_waist_radius_um=140, pump_farfield_half_angle_mrad=5.5)


def design_with(inputs, policy="evidence_only"):
    return solve_design({k: {"value": v} for k, v in inputs.items()}, [], assumption_policy=policy)


@pytest.mark.parametrize("focus_z", [-34, 0, 5.25])
def test_shared_crystal_keeps_source_station_and_both_indices_after_moving_crystal(focus_z):
    design = design_with({**COUPLED, "focus_lens_z_mm": focus_z})
    model = saved_geometry(design, 4, 1, target="coupled_layout")
    pump, = model.incident_paths
    host = {e.uid: e for e in model.system.elements}
    assert [(host[s.target_uid].medium_role, s.n) for s in pump.downstream] == [
        ("undoped", 1.71), ("gain", 1.71), ("undoped", 1.71)]
    for step in (0, 2.75):
        host[pump.target_uid].length_mm += step
        result = analyze_editable(model)
        path, = result["sections"]["paths"]
        assert path["source_z_mm"] == pytest.approx(COUPLED["source_z_mm"])
        center = result["sections"]["planes"][0]
        assert center["z_mm"] == pytest.approx(21 + step)
        primary, incident = center["beams"]
        assert [b["index"] for b in (primary, incident)] == [1.64, 1.71]
        assert [b["wavelength_nm"] for b in (primary, incident)] == [1030, 940]
        assert incident["field_axes"]["tangential"]["center_mm"] is None
        comparison = design_with({**COUPLED, "focus_lens_z_mm": focus_z,
                                  "crystal_front_mm": COUPLED["crystal_front_mm"] + step})["evidence_assessment"]
        assert primary["radii_mm"]["tangential"] == pytest.approx(comparison["cold_cavity"]["rows"][0]["mode_radius_mm"])
        expected = next(p["w_mm"] for p in comparison["pump_imaging"]["beam"]["points"] if p["z_mm"] == center["z_mm"])
        assert incident["radii_mm"]["tangential"] == pytest.approx(expected, rel=1e-12)


def test_independent_polars_at_same_wavelength_preserve_unknown_index():
    inputs = {**COUPLED, "pump_wavelength_nm": COUPLED["laser_wavelength_nm"]}
    del inputs["pump_index"]
    design = design_with(inputs)
    assert design["evidence_assessment"]["pump_imaging"]["editable_geometry"] == "geometry"
    model = saved_geometry(design, 7, 1, target="coupled_layout")
    primary, incident = analyze_editable(model)["sections"]["planes"][0]["beams"]
    assert primary["radii_mm"] and not incident["radii_mm"] and incident["index"] is None
    model.incident_paths[0].inherit_index_at_same_wavelength = True
    _, linked = analyze_editable(model)["sections"]["planes"][0]["beams"]
    assert linked["index"] == primary["index"] and linked["radii_mm"]


def test_candidate_rank_and_nominal_fiber_assumptions_survive_opening_each_geometry(client, db):
    values = {**COUPLED, "gain_material": "Nd:YAG", "pump_source": "fiber", "fiber_core_um": 80,
              "fiber_na": .12, "crystal_width_mm": 3, "crystal_height_mm": 3}
    for key in ("source_z_mm", "collimator_z_mm", "focus_lens_z_mm", "output_gap_mm",
                "pump_exit_waist_radius_um", "pump_farfield_half_angle_mrad"):
        del values[key]
    cid = client.post("/api/cases", json={"title": "名义芯像与腔模"}).json()["id"]
    plan = design_with(values, "exploratory")
    original = plan["candidates"][1]
    record = GeneratedContent(case_id=cid, content_type="plan", content={"engineering_design": plan})
    db.add(record)
    db.commit()
    endpoint = f"/api/cases/{cid}/design/{record.id}/import-project"
    revisions = set()
    for target, collection in (("pump_path", "candidates.pump"), ("coupled_layout", "candidates+pump"), ("cold_cavity", "candidates")):
        response = client.post(endpoint, json={"candidate": 2, "target": target})
        assert response.status_code == 200, response.text
        saved = response.json()
        revisions.add(saved["revision_id"])
        source = saved["project"]["component_context"]["design_origin"]
        assert (source["collection"], source["selection"]) == (collection, 2)
        assert client.post(endpoint, json={"candidate": 2, "target": target}).json()["revision_id"] == saved["revision_id"]
        if target != "cold_cavity":
            assert any("标称纤芯" in text for text in saved["project"]["assumptions"])
        if target == "coupled_layout":
            assert saved["analysis"]["sections"]["paths"][0]["source_z_mm"] == pytest.approx(original["pump"]["positions"]["source"])
    assert len(revisions) == 3 and read_project(db, cid) is None

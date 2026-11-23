from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.workbench.authoring import EditableLayout, analyze_editable
from app.workbench.forward_path import forward_path
from app.workbench.optics import air_gap, element_catalog, material_interval
from app.workbench.projects import OpticalProject


def apparatus():
    return {"system": {"mode": "single_pass", "wavelength_nm": 1064, "input_radius_mm": .2,
                       "elements": [{"uid": "sample", "name": "样品", "kind": "space", "length_mm": 12,
                                     "n": None, "index_reference": {"model": "nbk7"}},
                                    {**air_gap("空气间隔", 16), "uid": "gap"}]},
            "incident_paths": [{"uid": "beam", "name": "测量束", "wavelength_nm": 532, "target_uid": "sample",
                                "through_uid": "gap", "target_n": 1.6, "downstream": [{"target_uid": "gap", "n": 1.02}],
                                "tangential": {"radius_mm": .1, "wavefront": "waist"}}]}


def test_legacy_editor_preview_and_save_use_the_same_calibration(client):
    case_id = client.post("/api/cases", json={"title": "支路标定波长"}).json()["id"]
    endpoint = f"/api/cases/{case_id}/optical-project"
    first = client.post(endpoint, json={"project": {"title": "对照", **apparatus()}, "change_note": "两段均在 532 nm 标定"})
    assert first.status_code == 200, first.text
    saved = first.json()
    old = saved["project"]
    assert old["incident_paths"][0]["shared_index_wavelength_nm"] == 532
    proposal = deepcopy(old)
    arm = proposal["incident_paths"][0]
    arm.pop("shared_index_wavelength_nm")
    arm.update(wavelength_nm=780)
    arm["downstream"][0]["n"] = 1.03  # This old client supplied one new value, and echoed the other.
    preview = client.post(endpoint+"/preview", json={k: proposal[k] for k in ("system", "incident_paths")},
                          headers={"X-BeamWeft-Source-Revision": str(saved["revision_id"])})
    assert preview.status_code == 200, preview.text
    trial = preview.json()["sections"]["paths"][0]["shared_route"]
    assert [row["n"] for row in trial] == [None, 1.03]
    assert [row["reference_axis_opl_mm"] for row in trial] == [None, pytest.approx(16.48)]
    revised = client.post(endpoint, json={"base_revision_id": saved["revision_id"], "project": proposal, "change_note": "重定波长"})
    assert revised.status_code == 200, revised.text
    assert revised.json()["analysis"]["sections"]["paths"][0]["shared_route"] == trial
    actual = revised.json()["project"]["incident_paths"][0]
    assert actual["independent_index_uids"] == ["sample"] and actual["shared_index_wavelength_nm"] == 780
    assert client.get(endpoint, params={"revision_id":saved["revision_id"]}).json()["current"]["project"] == old
    # An explicit calibration at the new wavelength may legitimately have the same numeric value.
    confirmed = deepcopy(revised.json()["project"])
    confirmed["incident_paths"][0]["target_n"] = 1.6
    response = client.post(endpoint,json={"base_revision_id":revised.json()["revision_id"],
                           "project":confirmed,"change_note":"在 780 nm 独立复核"})
    assert response.status_code == 200 and response.json()["analysis"]["sections"]["paths"][0]["shared_route"][0]["n"] == 1.6


def test_tagged_overrides_expire_in_a_standalone_study_without_touching_its_input():
    source = EditableLayout.model_validate(apparatus()).model_dump()
    arm = source["incident_paths"][0]
    arm["wavelength_nm"] = 405
    result = EditableLayout.model_validate(source)
    assert [result.incident_paths[0].target_n, result.incident_paths[0].downstream[0].n] == [None, None]
    assert set(result.incident_paths[0].independent_index_uids) == {"sample", "gap"}
    assert (arm["target_n"], arm["downstream"][0]["n"], arm["shared_index_wavelength_nm"]) == (1.6, 1.02, 532)


def test_catalogue_material_condition_and_geometric_interval_are_independent():
    catalogue = {m["key"]:m for m in element_catalog()["material_models"]}
    assert catalogue["nbk7"]["valid_range_nm"] == [300, 2500]
    assert catalogue["nbk7"]["index_basis"] == "scalar" and catalogue["bbo_e"]["index_basis"] == "extraordinary"
    assert catalogue["air"]["source"] and catalogue["sio2_film"]["confidence"] == "representative"
    data = apparatus()
    for wavelength, manufacturer_index in [(404.7, 1.53024), (1060., 1.50669)]:
        system = {**data["system"], "wavelength_nm": wavelength}
        result = analyze_editable(EditableLayout.model_validate({"system": system}))
        # Rounded line wavelengths and indices from the SCHOTT N-BK7 datasheet.
        assert result["material_indices"][0]["n"] == pytest.approx(manufacturer_index, abs=1e-5)
    layout = EditableLayout.model_validate(data)
    gap = layout.system.elements[-1].model_dump()
    assert not material_interval(gap, {})
    assert material_interval(gap, {"role": "medium"})
    assert material_interval({**gap, "n": 1.8}, {})
    readings = analyze_editable(layout)["material_indices"]
    assert [row["element_uid"] for row in readings] == ["sample", "gap"]
    assert readings[0]["reference_axis_opl_mm"] == pytest.approx(12 * 1.506634802, rel=1e-9)
    assert readings[1]["reference_axis_opl_mm"] == 16
    data["system"]["wavelength_nm"] = None
    missing = EditableLayout.model_validate(data)
    assert missing.system.elements[0].n is None and missing.system.elements[-1].n == 1
    # A connected field can supply the only known wavelength, for either the main path or its private arm.
    path = forward_path(missing, "primary", source_wavelength=532)
    assert path.elements[0][1] == pytest.approx(1.51947, abs=1e-5)
    data["incident_paths"][0].update(wavelength_nm=None, elements=[data["system"]["elements"][0]])
    path = forward_path(EditableLayout.model_validate(data), "beam", source_wavelength=532)
    assert path.elements[0][1] == pytest.approx(1.51947, abs=1e-5)


def test_out_of_range_catalog_input_and_old_documented_sample_value():
    data = apparatus()
    data["system"]["wavelength_nm"] = 280
    part = data["system"]["elements"][0]
    part.update(n=1.62, index_reference={"model":"nbk7", "wavelength_nm":280})
    with pytest.raises(ValidationError, match="单点折射率"):
        EditableLayout.model_validate(data)
    project = {"title": "旧版材料标定", **data, "component_context":{"components":{"element:sample":{
        "medium_projection":{"element_uids":["sample"], "wavelength_nm":280, "sections":[{"index_model":"nbk7",
        "model_index":None, "project_index":1.62, "index_source":"旧工程的单点记录"}]}}}}}
    migrated = OpticalProject.model_validate(project).system.elements[0]
    assert migrated.n == 1.62 and migrated.index_reference.model is None
    assert part["index_reference"]["model"] == "nbk7"

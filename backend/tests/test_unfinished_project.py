from copy import deepcopy
import math

import pytest

from app.workbench.authoring import EditableElement, EditableLayout, analyze_editable
from app.workbench.observation import OpticalLayout, analyze_layout
from app.workbench.optics import OpticalElement, PropagationStudySystem


def test_save_incomplete_system_then_supply_only_geometry_then_wavelength(client):
    cid = client.post("/api/cases", json={"title":"待搭光路"}).json()["id"]
    url = f"/api/cases/{cid}/optical-project"
    project = {"title":"工程装配", "system":{"mode":"standing_wave", "elements":[{"kind":"mirror","name":"M1","uid":"m1"}]}}
    def save(previous=None):
        result = client.post(url,json={"base_revision_id":previous,"project":project,"change_note":"实验记录"})
        assert result.status_code == 200, result.text
        return result.json()
    first = save()
    assert first["analysis"]["status"] == "needs_input"
    assert not first["analysis"]["planes"] and "stable_both_planes" not in first["analysis"]
    assert first["project"]["system"]["wavelength_nm"] is None
    assert "端镜" in str(first["analysis"]["input_issues"])
    project["system"]["elements"] += [{"kind":"space","name":"空气","length_mm":100},
                                        {"kind":"mirror","name":"M2","radius_mm":200}]
    geometric = save(first["revision_id"])
    assert geometric["analysis"]["status"] == "partial" and geometric["analysis"]["stable_both_planes"]
    assert "output" not in geometric["analysis"]["planes"]["tangential"]
    assert geometric["analysis"]["planes"]["tangential"]["m"] == pytest.approx(0.)
    project["system"]["wavelength_nm"] = 1064
    complete = save(geometric["revision_id"])
    assert complete["analysis"]["status"] == "completed"
    waist = complete["analysis"]["planes"]["tangential"]["input"]["radius_mm"]
    assert waist == pytest.approx(math.sqrt(.001064 * 100 / math.pi))
    old = client.get(url,params={"revision_id":first["revision_id"]}).json()["current"]
    assert old["project"] == first["project"] and old["analysis"] == first["analysis"]


def test_unknown_index_never_becomes_air_and_other_beams_keep_their_results():
    main = {"mode":"single_pass","wavelength_nm":635,"input_radius_mm":.2,
            "elements":[{"uid":"host","kind":"space","name":"空气","length_mm":10}]}
    path = {"uid":"side","name":"待装支路","target_uid":"host","wavelength_nm":532,
            "elements":[{"uid":"glass","kind":"thick_lens","name":"厚镜","length_mm":3,"front_radius_mm":50}]}
    layout = EditableLayout(system=main, incident_paths=[path])
    assert layout.incident_paths[0].elements[0].n is None
    result = analyze_editable(layout)
    assert result["status"] == "partial" and result["planes"]["tangential"]["output"]["radius_mm"] > .2
    assert all(i["owner"] == "side" for i in result["input_issues"])
    assert result["input_issues"][0]["element_uid"] == "glass"
    assert result["input_issues"][0]["path"] == ["elements",0,"n"]
    for key in ("thick_lens", "grin"):
        unknown = {**main,"elements":[{"kind":key,"name":"未测介质","length_mm":3}]}
        assert not analyze_editable(EditableLayout(system=unknown))["planes"]
        with pytest.raises(ValueError,match="折射率"):
            PropagationStudySystem.model_validate(unknown)
    repaired = deepcopy(path)
    repaired["elements"][0]["n"] = 1.46
    assert "input_issues" not in analyze_editable(EditableLayout(system=main,incident_paths=[repaired]))


def test_public_element_schema_and_editable_values_agree_about_unknown_material():
    for model in (EditableElement, OpticalElement):
        schema = model.model_json_schema()["properties"]["n"]
        assert "default" not in schema and "GRIN" in schema["description"]
        assert model(kind="space", name="空气", length_mm=5).n == 1
        for kind in ("thick_lens", "grin"):
            assert model(kind=kind, name="未标号", length_mm=5, n=1).n == 1
    for kind in ("thick_lens", "grin"):
        values = EditableElement(kind=kind, name="未标号", length_mm=5).model_dump()
        assert values["n"] is None
        assert EditableElement.model_validate(values).n is None
        with pytest.raises(ValueError, match="折射率"):
            OpticalElement.model_validate(values)


def test_independent_port_network_never_waits_for_an_unused_ordered_wavelength():
    network = {"components":[{"uid":"bs","name":"分束器","model":{"kind":"coupler","through_fraction":.5}}],
               "links":[], "drives":[{"at":{"component":"bs","port":"1"},"power_w":2}]}
    system = {"mode":"single_pass","elements":[]}
    assert "尚无元件" in analyze_editable(EditableLayout(system=system))["model_warnings"][0]
    pending = analyze_editable(EditableLayout(system=system,network=network))
    assert pending["status"] == "needs_input" and pending["mode"] == "port_network"
    assert [issue["owner"] for issue in pending["input_issues"]] == ["network"]
    network["wavelength_nm"] = 800
    solved = analyze_editable(EditableLayout(system=system,network=network))
    assert solved["status"] == "completed" and "input_issues" not in solved
    frozen = OpticalLayout(system=system,network=network)
    assert frozen.system.wavelength_nm is None and analyze_layout(frozen)["network"]["wavelength_nm"] == 800
    with pytest.raises(ValueError,match="尚无元件"):
        frozen.system.for_propagation()
    system.update(mode="standing_wave",elements=[{"name":"M","kind":"mirror"}])
    mixed = analyze_editable(EditableLayout(system=system,network=network))
    assert mixed["status"] == "partial" and not mixed["planes"]
    powers = [port["outgoing_w"] for port in mixed["network"]["ports"] if port["port"] in ("3","4")]
    assert powers == pytest.approx([1,1])

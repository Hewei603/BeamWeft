"""Refractive-index evidence shared by independent beams and by a portable path slice."""
from copy import deepcopy
from math import hypot, pi, sqrt

import pytest

from app.workbench.authoring import EditableLayout, analyze_editable
from app.workbench.forward_path import forward_path
from app.workbench.layout_prefix import LayoutPrefixes, prefix_key, saved_prefixes
from app.workbench.scoped_parameters import scan_bindings
from app.models import GeneratedContent
from app.physics.materials import n_real


def bk7(wavelength_nm):
    squared = (wavelength_nm / 1000)**2
    b = [1.03961212, .231792344, 1.01046945]
    c = [.00600069867, .0200179144, 103.560653]
    return sqrt(1 + sum(v * squared/(squared - pole) for v, pole in zip(b, c, strict=True)))


def bench(wavelength=405, *, host_wave=1064, model="nbk7"):
    elements = [{"uid":"sample","name":"shared sample","kind":"space","length_mm":22,"n":None,
                 "index_reference":{"model":model,"wavelength_nm":host_wave}},
                {"uid":"exit","name":"output rail","kind":"space","length_mm":11,"n":1}]
    return {"system":{"mode":"single_pass","wavelength_nm":host_wave,"input_radius_mm":.12,"elements":elements},
            "observation_planes":[{"name":"in sample","uid":"inside","target_uid":"sample","depth_mm":15},
                                  {"name":"camera","uid":"meter","target_uid":"exit","depth_mm":11}],
            "incident_paths":[{"uid":"arm","name":"visible","target_uid":"sample","through_uid":"exit",
                               "wavelength_nm":wavelength,"tangential":{"radius_mm":.08,"wavefront":"waist","m2":1},
                               "downstream":[{"target_uid":"exit","n":1}]}]}


def evaluate(raw):
    layout = EditableLayout.model_validate(raw)
    return layout, analyze_editable(layout, include_sections=True)


def test_shared_medium_depends_on_branch_wavelength_and_exports_the_same_dispersion_law():
    raw = bench()
    layout, result = evaluate(raw)
    sections = result["sections"]
    shared, air = sections["paths"][0]["shared_route"]
    assert shared["index_source"] == "material_dispersion" and shared["index_model"] == "nbk7"
    assert "SCHOTT" in shared["catalog_source"] and shared["valid_range_nm"] == [300, 2500]
    assert shared["wavelength_nm"] == 405 and shared["n"] == pytest.approx(bk7(405))
    assert layout.system.elements[0].n == pytest.approx(bk7(1064))
    assert air["index_source"] == "branch_value"
    assert layout.incident_paths[0].target_n is None
    arm = next(b for b in sections["planes"][1]["beams"] if b["uid"] == "arm")
    expected = hypot(.08, (22/bk7(405)+11)*.000405/(pi*.08))
    assert arm["radii_mm"]["tangential"] == pytest.approx(expected, rel=1e-12)
    route = forward_path(layout, "arm", "meter").elements
    assert [n for _, n, _ in route] == pytest.approx([bk7(405), 1])
    prefix, where = LayoutPrefixes(layout).project("meter", "arm")
    projected = prefix["system"]["elements"][0]
    assert projected["index_reference"] == {"model":"nbk7","wavelength_nm":405}
    assert projected["n"] == pytest.approx(bk7(405))
    assert where["element_sources"][0]["n_path"] is None
    assert ("system","elements",0,"n") not in scan_bindings(raw, where, prefix)
    prefix["system"]["wavelength_nm"] = 550
    assert EditableLayout.model_validate(prefix).system.elements[0].n == pytest.approx(bk7(550))

    assert prefix_key("meter","arm") in {key for key, _, _ in saved_prefixes(raw, result)}
    historic = deepcopy(result)
    historic["sections"]["paths"][0]["shared_route"][0] = {"element_uid":"sample","z_mm":0,"n":None}
    assert prefix_key("meter","arm") not in {key for key, _, _ in saved_prefixes(raw, historic)}


@pytest.mark.parametrize(("updates","source"), [
    ({"independent_index_uids":["sample"]},"independent"),
    ({"inherit_index_at_same_wavelength":False},"independent"),
    ({"through_uid":None},"legacy_explicit"), ({"wavelength_nm":None},"wavelength_missing"),
    ({"wavelength_nm":250},"out_of_range")])
def test_independent_unknowns_are_preserved_and_out_of_range_is_local(updates, source):
    raw = bench()
    raw["incident_paths"][0].update(updates)
    _, result = evaluate(raw)
    step = result["sections"]["paths"][0]["shared_route"][0]
    assert step["n"] is None and step["index_source"] == source
    inside = result["sections"]["planes"][0]["beams"]
    assert inside[0]["radii_mm"] and inside[1]["radii_mm"] == {}


@pytest.mark.parametrize("wavelength", [532, 1064])
def test_directional_model_requires_evidence_on_the_second_beam(wavelength):
    raw = bench(wavelength, model="yvo4_o")
    _, result = evaluate(raw)
    sample = result["sections"]["paths"][0]["shared_route"][0]
    assert sample["n"] is None and sample["index_source"] == "directional_model"
    assert sample["index_basis"] == "ordinary"
    raw["incident_paths"][0].update(target_n=1.75)
    _, result = evaluate(raw)
    sample = result["sections"]["paths"][0]["shared_route"][0]
    assert sample["n"] == 1.75 and sample["index_source"] == "branch_value"


def test_in_range_branch_can_use_model_even_when_main_wavelength_has_no_index():
    raw = bench(host_wave=250)
    layout, result = evaluate(raw)
    assert layout.system.elements[0].n is None
    primary, arm = result["sections"]["planes"][0]["beams"]
    assert not primary["radii_mm"] and arm["radii_mm"]["tangential"] > 0
    assert prefix_key("inside","arm") in {key for key, _, _ in saved_prefixes(raw, result)}


def test_host_numeric_value_without_a_model_only_transfers_at_the_same_wavelength():
    raw = bench()
    sample = raw["system"]["elements"][0]
    sample["n"] = 1
    sample["index_reference"] = {"model":None,"wavelength_nm":1064}
    for wavelength, source, expected in [(1064,"host_same_wavelength",1),(405,"missing",None)]:
        raw["incident_paths"][0]["wavelength_nm"] = wavelength
        _, result = evaluate(raw)
        step = result["sections"]["paths"][0]["shared_route"][0]
        assert (step["index_source"], step["n"]) == (source, expected)


def install_gain_sample(client, material):
    cid = client.post("/api/cases",json={"title":"双波长增益样品"}).json()["id"]
    endpoint = f"/api/cases/{cid}/optical-project"
    project = bench()
    project["system"]["elements"][0].pop("index_reference")
    project["system"]["elements"][0]["n"] = 1.45
    project.update(title="测试安装位", parts=[{"element_index":0,"role":"crystal","source":"安装位置"}])
    initial = client.post(endpoint, json={"project":project,"change_note":"保存安装位"}).json()
    item = client.post("/api/inventory/items",json={"name":material,"category":"gain_crystal",
                       "crystal_spec":{"segments":[{"material":material,"length_mm":22,"role":"gain"}]}}).json()
    installed = client.post(endpoint+"/replace-component",json={"target_role":"crystal","inventory_id":item["id"],
                            "change_note":"换入实物","base_revision_id":initial["revision_id"]})
    assert installed.status_code == 200, installed.text
    return endpoint, installed.json()


@pytest.mark.parametrize(("material","model","source"), [
    ("Nd:YAG","yag","material_dispersion"), ("yvo4_o","yvo4_o","directional_model"), ("Nd:YVO4",None,"missing")])
def test_gain_mount_has_a_spectral_basis_and_requires_identified_polarization(client, material, model, source):
    endpoint, installed = install_gain_sample(client, material)
    project = installed["project"]
    sample = project["system"]["elements"][0]
    assert sample["index_reference"] == {"model":model,"wavelength_nm":1064}
    step = installed["analysis"]["sections"]["paths"][0]["shared_route"][0]
    assert step["index_source"] == source
    if model == "yag":
        assert step["n"] == pytest.approx(n_real("yag",405))
    if model is None:
        assert sample["n"] is None and step["n"] is None
    project["system"]["wavelength_nm"] = 750
    result = client.post(endpoint,json={"project":project,"base_revision_id":installed["revision_id"],"change_note":"改主波长"})
    assert result.status_code == 200,result.text
    updated = result.json()["project"]
    expected = n_real(model,750) if model else None
    assert updated["system"]["elements"][0]["n"] == expected
    assert updated["component_context"]["crystal_sections"][0]["model_index"] == expected


def test_recover_verified_gain_fit_from_legacy_receipt_on_next_revision_only(client, db):
    endpoint, installed = install_gain_sample(client, "Nd:YAG")
    record = db.get(GeneratedContent, installed["revision_id"])
    old = deepcopy(record.content)
    project = old["project"]
    del project["system"]["elements"][0]["index_reference"]
    record.content = old
    db.commit()
    draft = deepcopy(project)
    draft["system"]["wavelength_nm"] = 850
    result = client.post(endpoint,json={"project":draft,"base_revision_id":installed["revision_id"],"change_note":"改主波长"})
    assert result.status_code == 200, result.text
    changed = result.json()["project"]
    assert changed["system"]["elements"][0]["n"] == n_real("yag",850)
    assert changed["system"]["elements"][0]["index_reference"] == {"model":"yag","wavelength_nm":850}
    assert client.get(endpoint,params={"revision_id":installed["revision_id"]}).json()["current"]["project"] == project


def test_archive_with_assumed_extraordinary_vanadate_keeps_orientation_unknown(client, db):
    endpoint, installed = install_gain_sample(client, "Nd:YVO4")
    record = db.get(GeneratedContent, installed["revision_id"])
    old = deepcopy(record.content)
    project = old["project"]
    element = project["system"]["elements"][0]
    del element["index_reference"]
    element["n"] = n_real("yvo4_e",1064)
    project["component_context"]["crystal_sections"][0].update(model_index=element["n"], index_model="yvo4_e")
    record.content = old
    db.commit()
    result = client.post(endpoint,json={"project":project, "base_revision_id":installed["revision_id"],"change_note":"核对安装段"})
    assert result.status_code == 200, result.text
    sample = result.json()["project"]["system"]["elements"][0]
    assert sample["n"] is None and sample["index_reference"]["model"] is None
    assert client.get(endpoint,params={"revision_id":installed["revision_id"]}).json()["current"]["project"] == project

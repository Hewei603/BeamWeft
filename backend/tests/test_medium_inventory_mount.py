from copy import deepcopy

import pytest

from app.models import ExperimentCase
from app.physics.materials import n_real
from app.workbench.authoring import EditableLayout, analyze_editable
from app.workbench.component_selection import locate_mount
from app.workbench.layout_prefix import LayoutPrefixes
from app.workbench.optics import OpticalSystem, PropagationStudySystem
from app.workbench.projects import SaveProject, read_project, save_project


def optical_bench(db):
    case = ExperimentCase(title="两条光路的介质", cavity_type="custom", goal="核对介质折射率", parameters={})
    db.add(case)
    db.commit()
    sample = {"uid":"plate", "kind":"space", "n":1.5, "length_mm":5, "name":"同名样品"}
    project = {"title":"介质装配", "system":{"mode":"single_pass", "wavelength_nm":1064, "input_radius_mm":.2,
               "elements":[sample, {"kind":"space", "uid":"gap", "name":"自由空间", "length_mm":10}]},
               "incident_paths":[{"uid":"branch", "name":"另一光路", "target_uid":"gap", "target_n":1,
                                  "wavelength_nm":802, "elements":[sample], "tangential":{"radius_mm":.2,"wavefront":"waist"}}]}
    return case, save_project(db, case.id, SaveProject(project=project, change_note="开始"))


def inventory(client, **fields):
    response = client.post("/api/inventory/items", json=fields)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_material_mount_candidates_and_segment_identity(client, db):
    case, before = optical_bench(db)
    root = f"/api/cases/{case.id}/optical-project"
    multilayer = inventory(client, category="nonlinear_crystal", name="两段样品",
                  crystal_spec={"segments":[{"material":"fused_silica","length_mm":2},{"material":"YAG","length_mm":3}]})
    plate = inventory(client, category="window", name="BK7 平板", material="BK7", thickness_mm=5)
    curved = inventory(client, category="window", name="曲面窗", thickness_mm=5, roc_mm=60)
    partial = inventory(client, category="window", name="厚度待补")
    other = inventory(client, category="other", name="座", thickness_mm=3)
    target = {"target_path_uid":"branch", "target_element_uid":"plate"}
    candidates = client.get(root+"/replacement-candidates", params={**target, "revision_id":before["revision_id"]}).json()
    assert candidates["category"] == "medium" and candidates["default_category"] == "window"
    assert set(candidates["categories"]) == {"window", "nonlinear_crystal", "crystal_other", "gain_crystal", "other"}
    options = {o["id"]:o for o in candidates["items"]}
    assert options[plate]["eligible"] and options[multilayer]["eligible"]
    assert "曲率" in options[curved]["reason"] and "厚度" in options[partial]["reason"] and not options[other]["eligible"]
    request = {**target, "base_revision_id":before["revision_id"], "inventory_id":multilayer, "orientation":"reversed","change_note":"按反向安装"}
    preview = client.post(root+"/replace-component/preview", json=request)
    assert preview.status_code == 200, preview.text
    response = client.post(root+"/replace-component", json={**request,"preview_token":preview.json()["preview_token"],"change_note":"按反向安装"})
    assert response.status_code == 200, response.text
    installed = response.json()
    project = installed["project"]
    assert before["project"]["system"] == project["system"]
    arm = project["incident_paths"][0]
    assert [e["length_mm"] for e in arm["elements"]] == [3, 2]
    assert [e["n"] for e in arm["elements"]] == pytest.approx([n_real("yag",802),n_real("fused_silica",802)])
    assert len(project["assemblies"]) == 1
    uid1, uid2 = [e["uid"] for e in arm["elements"]]
    branch_context = project["component_context"]["path_contexts"]["branch"]
    assert "crystal_spec" not in branch_context
    frozen = branch_context["components"][f"element:{uid1}"]
    assert [s["material"] for s in frozen["crystal_spec"]["segments"]] == ["fused_silica","YAG"]
    assert [s["material"] for s in frozen["medium_projection"]["sections"]] == ["YAG","fused_silica"]
    assert frozen["medium_projection"]["sections"][0]["index_source"]
    assert preview.json()["project"] == project
    occupied = client.get(root+"/replacement-candidates", params={"revision_id":installed["revision_id"], "target_element_uid":"plate",
                                                                 "search":f"#{multilayer}"}).json()["items"][0]
    assert not occupied["eligible"] and occupied["occupied_quantity"] == 1
    assert client.patch(f"/api/inventory/items/{multilayer}",json={"name":"已重新标记的样品"}).status_code == 200
    saved_copy = save_project(db,case.id,SaveProject(project=project,base_revision_id=installed["revision_id"],change_note="复算先前实物"))
    assert saved_copy["project"]["component_context"]["path_contexts"]["branch"]["components"][f"element:{uid1}"] == frozen
    retry = client.post(root+"/replace-component", json={**target, "target_element_uid":uid2,
                        "base_revision_id":saved_copy["revision_id"], "inventory_id":plate, "change_note":"以第二段定位同一实物"})
    assert retry.status_code == 200, retry.text
    replaced = retry.json()["project"]
    current = replaced["component_context"]["path_contexts"]["branch"]
    assert len(current["components"]) == 1 and f"element:{uid1}" not in current["warnings_by_role"]
    assert replaced["incident_paths"][0]["elements"][0]["n"] == pytest.approx(n_real("nbk7",802))
    assert read_project(db,case.id,installed["revision_id"])["project"] == project


@pytest.mark.parametrize(("material","model"), [("BK7","nbk7"), ("YVO4",None), ("Unidentified KQ-17",None)])
def test_wavelength_edit_uses_dispersion_or_requires_a_new_measurement(client, db, material, model):
    case, before = optical_bench(db)
    stock = inventory(client, category="window", name="窗", material=material, thickness_mm=5)
    root = f"/api/cases/{case.id}/optical-project"
    installed = client.post(root+"/replace-component", json={"target_element_uid":"plate", "base_revision_id":before["revision_id"],
                            "inventory_id":stock, "change_note":"按记录装配"}).json()
    project = deepcopy(installed["project"])
    medium = project["system"]["elements"][0]
    original_uid = medium["uid"]
    revision = installed["revision_id"]
    assert medium["index_reference"]["model"] == model
    if model is None:
        assert medium["n"] is None and installed["analysis"]["status"] == "needs_input"
        medium["n"] = 1.55
        measured = save_project(db, case.id, SaveProject(project=project, base_revision_id=revision, change_note="填入此波长的实测值"))
        project = deepcopy(measured["project"])
        revision = measured["revision_id"]
        assert "工程填写" in project["component_context"]["components"][f"element:{original_uid}"]["medium_projection"]["sections"][0]["index_source"]
    project["system"]["wavelength_nm"] = 633
    layout = EditableLayout.model_validate({k:project[k] for k in ("system","incident_paths")})
    expected = n_real(model,633) if model else None
    assert layout.system.elements[0].n == expected
    if model is None:
        assert analyze_editable(layout)["status"] == "needs_input"
        for solver in (OpticalSystem, PropagationStudySystem):
            with pytest.raises(ValueError, match="折射率"):
                solver.model_validate(project["system"])
    else:
        for solver in (OpticalSystem, PropagationStudySystem):
            assert solver.model_validate(project["system"]).elements[0].n == expected
    result = save_project(db,case.id,SaveProject(project=project,base_revision_id=revision,change_note="改工作波长"))
    assert result["project"]["system"]["elements"][0]["n"] == expected
    assert result["project"]["component_context"]["components"][f"element:{original_uid}"]["medium_projection"]["wavelength_nm"] == 633
    if model:
        invalid = deepcopy(result["project"])
        invalid["system"]["elements"][0]["n"] = 1.74
        with pytest.raises(ValueError, match="折射率"):
            EditableLayout.model_validate({k:invalid[k] for k in ("system","incident_paths")})
        with pytest.raises(ValueError, match="折射率"):
            save_project(db,case.id,SaveProject(project=invalid,base_revision_id=result["revision_id"],change_note="检查不同来源相互矛盾"))


def test_ordinary_air_apertures_and_mixed_assemblies_do_not_become_solid_medium(db):
    _, before = optical_bench(db)
    project = before["project"]
    gap = project["system"]["elements"][1]
    with pytest.raises(ValueError, match="空气间隔"):
        locate_mount(project,"primary","gap")
    project["assemblies"] = [{"uid":"group","name":"样品及间隙","path_uid":"primary","element_uids":["plate","gap"]}]
    with pytest.raises(ValueError, match="组合"):
        locate_mount(project,"primary","plate")
    project["assemblies"] = []
    gap["index_reference"] = {"wavelength_nm":1064, "model":None}
    assert locate_mount(project,"primary","gap").category == "medium"
    gap["clear_aperture"] = {"shape":"ellipse","width_mm":1}
    with pytest.raises(ValueError, match="光阑"):
        locate_mount(project,"primary","gap")


def test_incomplete_or_misaddressed_medium_snapshot_is_recoverable(client, db):
    case, before = optical_bench(db)
    root = f"/api/cases/{case.id}/optical-project"
    item = inventory(client, category="window", name="窗", material="BK7", thickness_mm=5)
    installed = client.post(root+"/replace-component", json={"target_element_uid":"plate","base_revision_id":before["revision_id"],
                            "inventory_id":item,"change_note":"安装"}).json()
    uid = installed["project"]["system"]["elements"][0]["uid"]
    source = installed["project"]["component_context"]["components"][f"element:{uid}"]["medium_projection"]
    for projection in (None, {}, {**source,"element_uids":[]}, {**source,"element_uids":["other"]},
                       {**source,"element_uids":[uid,uid]}, {**source,"spec":{"segments":"broken"}}):
        draft = deepcopy(installed["project"])
        draft["component_context"]["components"][f"element:{uid}"]["medium_projection"] = projection
        denied = client.post(root,json={"project":draft,"base_revision_id":installed["revision_id"],"change_note":"恢复导入文件"})
        assert denied.status_code == 422 and "重新选用" in denied.text
    assert read_project(db,case.id)["project"] == installed["project"]


def test_saved_prefix_uses_independent_shared_segment_index(db):
    _, saved = optical_bench(db)
    project = saved["project"]
    host = project["system"]["elements"][0]
    host.update(n=n_real("nbk7",1064),index_reference={"wavelength_nm":1064,"model":"nbk7"})
    branch = project["incident_paths"][0]
    branch.update(target_uid=host["uid"], target_n=1.57)
    project["observation_planes"] = [{"uid":"station","target_uid":host["uid"],"name":"样品内","depth_mm":2}]
    prefix, details = LayoutPrefixes.from_document(project).project("station","branch")
    section = EditableLayout.model_validate(prefix).system.elements[-1]
    assert details["medium_n"] == section.n == 1.57
    assert section.index_reference.wavelength_nm == branch["wavelength_nm"]
    assert section.index_reference.model is None

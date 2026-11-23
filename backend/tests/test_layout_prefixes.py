from copy import deepcopy

import pytest

from app.workbench.layout_prefix import LayoutPrefixes, prefix_key, saved_prefixes
from app.workbench.observation import OpticalLayout


def test_saved_known_prefix_and_live_preview_are_the_same_physical_problem(client):
    case = client.post("/api/cases",json={"title":"先计算已测光路"}).json()["id"]
    root = f"/api/cases/{case}"
    plane = "测点/%"
    project = {"title":"源面到中间面","system":{"mode":"single_pass","input_radius_mm":.3,"wavelength_nm":1000,
               "elements":[{"kind":"space","uid":"known","name":"首段","length_mm":30},
                           {"kind":"thick_lens","uid":"unmeasured","name":"待测镜","length_mm":7}]},
               "observation_planes":[{"name":"中间测点","uid":plane,"target_uid":"known","depth_mm":12}]}
    saved = client.post(root+"/optical-project",json={"project":project,"change_note":"测量未完"}).json()
    assert saved["analysis"]["input_issues"][0]["path"] == ["elements",1,"n"]
    beam = saved["analysis"]["sections"]["planes"][0]["beams"][0]
    key = beam["layout_prefix_port"]
    assert key == prefix_key(plane,"primary")
    revision = {"revision_id":saved["revision_id"]}
    outputs = root+"/studies/project-outputs"
    assert client.get(outputs,params={**revision,"port":"v1/layout"}).status_code == 404
    exact = client.get(outputs,params={**revision,"port":key}).json()["outputs"][key]
    assert exact["value"]["system"]["elements"][0]["length_mm"] == 12
    assert exact["excluded_issue_indices"] == [0] and exact["element_sources"][0]["length_path"] == ["observation_planes",0,"depth_mm"]
    assert exact["element_uid"]=="known" and exact["path_uid"]=="primary" and exact["position_reference"]=="element_depth"
    assert saved["project"]["system"]["elements"][0]["length_mm"] == 30 and saved["project"]["system"]["elements"][1]["n"] is None
    path = "propagate_coherent_field"
    definition = client.get(root+"/studies/catalog/"+path).json()
    assert definition["layout_prefix_inputs"] == {"plane":"plane_uid","path":"beam_uid"}
    req = {"provider":definition["provider"],"version":definition["version"],"inputs":{"samples":128,"window_mm":2}}
    draft = {k:project[k] for k in ("system","observation_planes")}
    preview = root+"/studies/preview/"+path
    params = {"prefix_plane_uid":plane,"prefix_path_uid":"primary"}
    direct = client.post(preview,params=params,json={**req,"inputs":{**req["inputs"],"layout":draft}})
    assert direct.status_code==200,direct.text
    assert client.post(preview,json={**req,"inputs":{**req["inputs"],"layout":draft}}).status_code==422
    connection = {"layout":{**revision,"port":key,"registration":"沿归档的源面正向到已登记的截面。"}}
    received = client.post(root+"/studies/run/"+path,json={**req,"connections":connection})
    assert received.status_code==200,received.text
    result = received.json()
    assert result["exit_field"] == direct.json()["exit_field"]
    assert result["exit_field"]["distance_mm"] == 12 and result["exit_field"]["plane"]["plane_uid"]==plane
    assert result["throughput"] == pytest.approx(1.,abs=1e-6)
    record = client.get(root+f"/studies/runs/{result['study_record']['id']}").json()
    assert record["project_evidence"]["layout"]["element_sources"]==exact["element_sources"]
    assert record["connections"]["layout"]["revision_id"]==saved["revision_id"]

    # Manually selected ray width or an undeclared phase cannot stand in for a complex field.
    draft["system"]["beam_quality_m2"]=1.2
    assert "复振幅" in client.post(preview,params=params,json={**req,"inputs":{"layout":draft}}).text
    draft["system"]["wavelength_nm"]=None
    assert client.post(preview,params=params,json={**req,"inputs":{"layout":draft}}).status_code==422


def test_branch_source_and_registered_sides_preserve_physical_order_and_apertures():
    layout = {"system":{"mode":"single_pass","wavelength_nm":1064,"input_radius_mm":.2,"elements":[
        {"uid":"before","name":"待测基底","kind":"thick_lens","length_mm":25},
        {"uid":"g","name":"窗口","kind":"space","length_mm":10,"n":1.5},
        {"uid":"thin","name":"已知焦距","kind":"lens","focal_mm":35,"n":None,"clear_aperture":{"shape":"ellipse","width_mm":.42}},
        {"uid":"mirror","name":"零厚度反射","kind":"mirror","radius_mm":90,"n":None},
        {"uid":"air","name":"屏前","kind":"space","length_mm":10}]},
        "incident_paths":[{"uid":"arm/%","name":"独立探针","target_uid":"g","through_uid":"air","target_n":1.41,
            "downstream":[{"target_uid":"air","n":1}],"wavelength_nm":800,
            "tangential":{"m2":1,"wavefront":"waist","radius_mm":.2},
            "elements":[{"uid":"g","name":"私有空气","kind":"space","length_mm":3}]}],
        "observation_planes":[{"position_reference":"fixed_z","uid":face,"name":face,"z_mm":35,"boundary_side":face}
                              for face in ("upstream","downstream")]}
    slices = LayoutPrefixes.from_document(layout)
    for face in ("upstream","downstream"):
        with pytest.raises(ValueError,match="折射率"):
            slices.project(face,"primary")
    up, up_info = slices.project("upstream","arm/%")
    down, info = slices.project("downstream","arm/%")
    assert [el["name"] for el in up["system"]["elements"]] == ["私有空气","窗口"]
    assert [el["name"] for el in down["system"]["elements"]] == ["私有空气","窗口","已知焦距","零厚度反射","屏前"]
    assert len({el["uid"] for el in down["system"]["elements"]}) == 5
    assert down["system"]["elements"][1]["n"]==1.41 and down["system"]["wavelength_nm"]==800
    assert [el["n"] for el in down["system"]["elements"][2:4]] == [None,None]
    assert down["system"]["elements"][-1]["length_mm"]==0
    assert info["element_sources"][1]["n_path"]==["incident_paths",0,"target_n"]
    assert info["element_sources"][2]["n_path"] is None and info["element_sources"][-1]["length_path"] is None
    assert up_info["source_z_mm"]==22 and info["z_mm"]==35 and info["position_reference"]=="fixed_z"
    from app.workbench.wave_study import WavePropagation, prepare_field
    def operations(data,plane):
        _,_,_,path,end,_=prepare_field(WavePropagation(layout=OpticalLayout.model_validate(data),plane_uid=plane))
        return [el for el,_,_ in path],end
    front,z0=operations(up,"upstream")
    after,z1=operations(down,"downstream")
    assert z0==z1==13 and not any(el.clear_aperture for el in front)
    assert sum(bool(el.clear_aperture) for el in after)==1
    assert after[2].sections[0].c_t_per_mm == pytest.approx(-1/35)
    assert after[3].sections[0].c_t_per_mm == pytest.approx(-2/90)
    missing=deepcopy(layout)
    missing["incident_paths"][0]["target_n"]=None
    with pytest.raises(ValueError,match="折射率"):
        LayoutPrefixes.from_document(missing).project("downstream","arm/%")


def test_legacy_branch_only_publishes_a_connected_prefix_and_checks_frozen_blockers():
    project={"system":{"mode":"single_pass","wavelength_nm":938,"elements":[
        {"uid":k,"name":k,"kind":"space","length_mm":10} for k in ("a","b","c")]},
        "incident_paths":[{"uid":"branch","name":"旧处方","wavelength_nm":805,"target_uid":"a","target_n":1,
                           "downstream":[{"target_uid":"c","n":1}]}],
        "observation_planes":[{"uid":k,"name":k,"target_uid":k,"depth_mm":5} for k in ("a","c")]}
    slices=LayoutPrefixes.from_document(project)
    _, info=slices.project("a","branch")
    assert info["wavelength_nm"]==805
    with pytest.raises(ValueError,match="连续"):
        slices.project("c","branch")
    analysis={"sections":{"planes":[{"uid":"a","target_uid":"a","z_mm":5,"depth_mm":5,
              "beams":[{"uid":"primary","wavelength_nm":938,"index":1},{"uid":"branch","wavelength_nm":805,"index":1}]}]},
              "input_issues":[{"owner":"primary","path":["elements",0,"n"],"element_uid":"a","message":"历史方法显式登记 n 未知"}]}
    assert [key for key,_,_ in saved_prefixes(project,analysis)] == [prefix_key("a","branch")]

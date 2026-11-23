import math

import pytest


@pytest.mark.parametrize("primary_basis,branch_basis", [("equivalent", "second_moment"), ("second_moment", "equivalent")])
def test_unit_m2_width_basis_survives_shared_station_and_connected_studies(client, primary_basis, branch_basis):
    from app.workbench.beam_source import SourceAxis

    cid = client.post("/api/cases", json={"title": "不同口径的同轴输入光束"}).json()["id"]
    root = f"/api/cases/{cid}"

    def axis(basis, radius):
        return {"wavefront": "waist", "m2": 1, "radius_mm": radius, basis + "_radius": True}

    def study(name, inputs, connections):
        definition = client.get(root + "/studies/catalog/" + name).json()
        return client.post(root + "/studies/run/" + name, json={"version": definition["version"], "provider": definition["provider"],
                                                               "inputs": inputs, "connections": connections})

    project = {"title": "窗口内两束光", "system": {"mode": "single_pass", "wavelength_nm": 940,
               "source": {"tangential": axis(primary_basis, .28)},
               "elements": [{"kind": "space", "uid": "glass", "name": "窗口", "length_mm": 22, "n": 1.55}]},
               "incident_paths": [{"uid": "arm", "name": "入射光束", "wavelength_nm": 812, "target_uid": "glass",
                                   "target_n": 1.57, "tangential": axis(branch_basis, .16)}],
               "observation_planes": [{"uid": "test", "name": "窗口内", "target_uid": "glass", "depth_mm": 7}]}
    saved = client.post(root + "/optical-project", json={"project": project, "change_note": "保留半径口径"})
    assert saved.status_code == 200, saved.text
    revision = saved.json()
    assert revision["analysis"]["intensity_model"] == "width_only"
    assert revision["analysis"]["planes"]["tangential"]["width_basis"] == primary_basis
    beams = revision["analysis"]["sections"]["planes"][0]["beams"]
    for row in beams:
        basis, radius, wavelength, index = ((primary_basis, .28, .000940, 1.55) if row["uid"] == "primary" else
                                             (branch_basis, .16, .000812, 1.57))
        assert row["width_basis"] == dict(tangential=basis, sagittal=basis)
        assert row["radii_mm"]["tangential"] == pytest.approx(math.hypot(radius, 7 * wavelength / (math.pi * index * radius)))
        ref = {"revision_id": revision["revision_id"], "port": row["continuation_port"]}
        station = client.get(root + "/studies/project-outputs", params=ref).json()["outputs"][ref["port"]]["value"]
        material = study("propagate_paraxial_sections", {"sections": [{"length_mm": 15, "next_medium_n": 1}]},
                         {"input_beam": {**ref, "registration": "同一窗口内的截面，原点与两横轴一致"}})
        assert material.status_code == 200, material.text
        air = study("analyze_optical_system", {"mode": "single_pass", "elements": [{"name": "末段", "kind": "space", "length_mm": 20}]},
                    {"input_beam": {"run_id": material.json()["study_record"]["id"], "port": "exit_air",
                                    "registration": "从同一窗口的出射面沿原横轴继续"}})
        assert air.status_code == 200, air.text
        for state in (station, material.json()["exit_beam"], material.json()["exit_air"], air.json()["exit_beam"]):
            for direction in ("tangential", "sagittal"):
                returned = SourceAxis.model_validate(state[direction])
                assert returned.m2 == 1 and returned.width_basis() == basis
        assert air.json()["intensity_model"] == "width_only" and "exit_field" not in air.json()
        coherent = study("propagate_coherent_field", {"samples": 128}, {"layout": {
            "revision_id": revision["revision_id"], "port": row["layout_prefix_port"],
            "registration": "使用同一光路从源面传播到记录截面"}})
        assert coherent.status_code == 422, coherent.text
        assert "复振幅" in coherent.text
    assert {row["uid"] for row in beams} == {"primary", "arm"}


def test_continue_from_frozen_plane_and_replay_after_source_change(client):
    cid = client.post("/api/cases", json={"title": "中间面"}).json()["id"]
    root = f"/api/cases/{cid}"
    source = {"tangential": {"radius_mm": .32, "m2": 1.4, "wavefront": "waist", "second_moment_radius": True},
              "sagittal": {"radius_mm": .43, "m2": 1, "wavefront": "unknown"},
              "launch_ray": {"t_offset_mm": .12, "t_angle_mrad": 1, "s_offset_mm": -.31, "s_angle_mrad": None}}
    plane = {"uid": "focus/百分%", "name": "接收面", "target_uid": "air", "depth_mm": 20}
    system = {"mode": "single_pass", "wavelength_nm": 1000, "source": source,
              "elements": [{"uid": "air", "name": "第一段", "kind": "space", "length_mm": 20}]}
    project = {"title": "两横轴", "system": system, "observation_planes": [plane]}

    def save(previous=None):
        response = client.post(root+"/optical-project", json={"project": project, "base_revision_id": previous, "change_note": "测量条件"})
        assert response.status_code == 200, response.text
        doc = response.json()
        return doc["revision_id"], doc["analysis"]["sections"]["planes"][0]["beams"][0]["continuation_port"]

    first, key = save()
    offers = client.get(root+"/studies/project-outputs", params={"revision_id": first}).json()["outputs"]
    assert offers[key]["plane_uid"] == plane["uid"] and offers[key]["path_uid"] == "primary"
    assert "value" not in offers[key] and "%2F" in key
    params = {"revision_id": first, "port": key}
    focused = client.get(root+"/studies/project-outputs", params=params).json()["outputs"]
    assert list(focused) == [key] and focused[key]["value"]["sagittal"]["radius_mm"] is None
    assert client.get(root+"/studies/project-outputs", params={**params, "port": key+"x"}).status_code == 404
    other = client.post("/api/cases", json={"title": "其他工程"}).json()["id"]
    assert client.get(f"/api/cases/{other}/studies/project-outputs", params=params).status_code == 404
    definition = client.get(root+"/studies/catalog/analyze_optical_system").json()
    ref = {"revision_id": first, "port": key, "registration": "同向展开空气参考面，T/S 原点重合"}
    inputs = {"mode": "single_pass", "elements": [{"kind": "lens", "focal_mm": 10, "name": "L"},
                                                {"kind": "space", "length_mm": 20, "name": "屏前空气"}]}
    request = {"version": definition["version"], "provider": definition["provider"], "inputs": inputs, "connections": {"input_beam": ref}}
    run = client.post(root+"/studies/run/analyze_optical_system", json=request)
    assert run.status_code == 200, run.text
    measured = run.json()["exit_beam"]
    # This symmetric 2f–2f system images the input with magnification −1.
    for axis, radius in (("tangential", .32), ("sagittal", .43)):
        assert measured[axis]["radius_mm"] == pytest.approx(radius)
    assert measured["sagittal"]["wavefront"] == "unknown"
    assert measured["launch_ray"]["s_offset_mm"] == pytest.approx(.31)
    assert measured["launch_ray"]["s_angle_mrad"] is None
    rid = run.json()["study_record"]["id"]
    frozen = client.get(root+f"/studies/runs/{rid}").json()
    incident = frozen["inputs"]["input_beam"]
    expected = .32 * math.hypot(1, 20*.0014/(math.pi*.32**2))
    assert incident["tangential"]["radius_mm"] == pytest.approx(expected)
    assert incident["tangential"]["second_moment_radius"] and incident["sagittal"]["radius_mm"] is None
    assert frozen["project_evidence"]["input_beam"]["plane_uid"] == plane["uid"]
    source["sagittal"]["radius_mm"] = .51
    second, second_key = save(first)
    assert key == second_key
    replay = client.post(root+"/studies/replay", json={"run_id": rid, "source_id": first, "replacement_id": second, "execute": True,
                         "registrations": {str(rid): {"input_beam": ref["registration"]}}}).json()
    assert replay["status"] == "partial" and replay["result_id"], replay["steps"]
    updated = client.get(root+f"/studies/runs/{replay['result_id']}").json()
    assert updated["output"]["exit_beam"]["sagittal"]["radius_mm"] == pytest.approx(.51)
    assert client.get(root+f"/studies/runs/{rid}").json() == frozen


def test_branch_conjugate_plane_and_air_contract(client):
    cid = client.post("/api/cases", json={"title": "共享处方"}).json()["id"]
    root = f"/api/cases/{cid}"
    optics = [{"uid":"a", "kind":"space", "name":"物侧", "length_mm":50.8},
              {"uid":"lens", "kind":"lens", "name":"中继", "focal_mm":25.4},
              {"uid":"b", "kind":"space", "name":"像侧", "length_mm":50.8},
              {"uid":"g", "kind":"space", "name":"玻璃", "n":1.5, "length_mm":10},
              {"uid":"stop", "kind":"space", "name":"光阑之后", "length_mm":10, "clear_aperture":{"shape":"ellipse","width_mm":1}}]
    geom = {"mode":"single_pass","wavelength_nm":976,"elements":optics}
    arm = {"uid":"arm", "name":"侧路", "target_uid":"a", "target_n":1, "through_uid":"stop", "wavelength_nm":808,
           "downstream":[{"target_uid":uid,"n":index} for uid,index in (("b",1),("g",1.6),("stop",1))],
           "tangential":{"radius_mm":.4,"m2":1.5}, "launch_ray":{"t_offset_mm":.17,"t_angle_mrad":None}}
    planes = [{"uid":el["uid"], "name":el["name"], "target_uid":el["uid"],"depth_mm":el["length_mm"]} for el in optics if el["kind"]=="space"]
    response = client.post(root+"/optical-project", json={"project":{"title":"分色","system":geom,"incident_paths":[arm],"observation_planes":planes},"change_note":"分析"})
    assert response.status_code == 200, response.text
    doc = response.json()
    beams = {s["uid"]:next(b for b in s["beams"] if b["uid"]=="arm") for s in doc["analysis"]["sections"]["planes"]}
    assert "tangential" not in beams["a"]["radii_mm"]
    assert beams["b"]["radii_mm"]["tangential"] == pytest.approx(.4)
    assert beams["b"]["field_axes"]["tangential"]["center_mm"] == pytest.approx(-.17)
    assert beams["g"]["continuation_port"].startswith("station-v3/")
    assert "continuation_port" not in beams["stop"]
    offers = client.get(root+"/studies/project-outputs", params={"revision_id":doc["revision_id"]}).json()["outputs"]
    continuation_offers = [value for port, value in offers.items() if port.startswith(("station-v2/", "station-v3/"))]
    stations = {p["plane_uid"] for p in continuation_offers if p.get("path_uid")=="arm"}
    assert stations == {"a","b","g"}
    material = client.get(root+"/studies/project-outputs", params={"revision_id":doc["revision_id"],
                         "port":beams["g"]["continuation_port"]}).json()["outputs"][beams["g"]["continuation_port"]]["value"]
    assert material["wavelength_nm"] == 808 and material["medium_n"] == 1.6
    assert material["origins"]["tangential"]["medium_n"] == 1
    assert not any(p.get("path_uid")=="primary" for p in continuation_offers)
    optics[2]["length_mm"] += .001
    next(p for p in planes if p["uid"]=="b")["depth_mm"] = optics[2]["length_mm"]
    altered = client.post(root+"/optical-project", json={"base_revision_id":doc["revision_id"],
                          "project":{"title":"探测面移动 1 μm","system":geom,"incident_paths":[arm],"observation_planes":planes},
                          "change_note":"离开共轭面"}).json()
    screen = next(p for p in altered["analysis"]["sections"]["planes"] if p["uid"]=="b")
    off_image = next(b for b in screen["beams"] if b["uid"]=="arm")
    assert "tangential" not in off_image["radii_mm"] and off_image["field_axes"]["tangential"]["center_mm"] is None


def test_freeze_only_a_bound_cavity_eigenmode_and_read_earlier_scalar_evidence():
    from app.workbench.station_outputs import station_beams, with_station_links
    from app.workbench.projects import OpticalProject
    from app.workbench.authoring import analyze_editable

    cavity = {"mode":"standing_wave","wavelength_nm":1000,"elements":[
        {"kind":"mirror","uid":"m1","name":"平面镜"},
        {"kind":"space","uid":"a","name":"空气","length_mm":40},
        {"kind":"mirror","uid":"m2","name":"凹面镜","radius_mm":100}]}
    model = {"title":"腔内参考面","system":cavity,"observation_planes":[{"uid":"s","name":"测试面","target_uid":"a","depth_mm":0}]}
    def run():
        layout = OpticalProject.model_validate(model)
        return list(station_beams(layout.model_dump(), analyze_editable(layout)))
    _, _, state, where = run()[0]
    z_r = math.sqrt(40 * 60)
    assert state["tangential"]["radius_mm"] == pytest.approx(math.sqrt(.001*z_r/math.pi))
    assert where["location"] == "测试面 · 腔内正向本征模"
    cavity["elements"][1]["length_mm"] = 130
    assert run() == []

    # A historical plane can supply its stored radius without re-running the current solver.
    project = {"system":{"mode":"single_pass","input_radius_mm":.25,"beam_quality_m2":1},"observation_planes":model["observation_planes"]}
    old = {"uid":"primary","index":1,"wavelength_nm":980,"launch_basis":"reference_axis","radii_mm":{"tangential":.25},
           "field_axes":{"tangential":{"center_mm":0,"reduced_angle":0}},"width_basis":{"tangential":"gaussian","sagittal":"gaussian"}}
    analysis = {"sections":{"planes":[{"uid":"s","target_uid":"a","beams":[old]}]}}
    restored = list(station_beams(project, analysis))[0][2]
    assert restored["tangential"]["wavefront"] == "unknown" and restored["sagittal"]["radius_mm"] is None
    assert with_station_links(project, analysis)["sections"]["planes"][0]["beams"][0]["continuation_port"] == "station-v2/s/primary/beam"
    assert "continuation_port" not in old
    del old["field_axes"]
    assert list(station_beams(project, analysis)) == []

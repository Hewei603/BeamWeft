import math

import pytest

from app.workbench.authoring import EditableLayout, analyze_editable
from app.workbench.optics import analyze_system


def gap(length, n=1):
    return {"kind":"space", "name":"medium", "length_mm":length, "n":n}


def model(elements, **ray):
    return {"mode":"single_pass", "wavelength_nm":800, "source":{"tangential":{}, "launch_ray":ray},
            "elements":elements}


def test_unknown_launch_angle_recovers_an_isolated_image_inside_a_space():
    system = model([gap(20), {"kind":"lens","name":"L","focal_mm":10}, gap(25)],
                   t_offset_mm=.8, t_angle_mrad=None, s_offset_mm=-.2, s_angle_mrad=5)
    planes = analyze_system(system)["planes"]
    t, s = planes["tangential"], planes["sagittal"]
    assert t["center_profile"] == [[0.,.8], [40.,-.8]]
    assert t["center_profile_breaks"] == [1]
    assert not t["samples"] and t["center_basis"] == "specified"
    assert s["center_profile"][-1][1] == pytest.approx(-.1 + 25 * (.005 + .1/10))
    assert s["center_profile_breaks"] == []


def test_refracting_faces_and_grin_follow_the_cross_section_transfer_laws():
    thick = {"kind":"thick_lens", "name":"L", "length_mm":12, "n":1.6, "front_radius_mm":50, "back_radius_mm":-75}
    grin = {"kind":"grin", "name":"rod", "n":1.4, "gradient_per_mm":.04, "length_mm":24}
    system = model([thick,grin], t_offset_mm=1.2,t_angle_mrad=2.)
    trace = analyze_system(system)["planes"]["tangential"]["center_profile"]
    p = .002 + (1-1.6)/50 * 1.2
    x = 1.2 + 12*p/1.6
    p += (1.6-1)/(-75) * x
    assert trace[1] == pytest.approx([12,x])
    assert len(trace)>10
    for z, offset in trace[2:]:
        phase = .04*(z-12)
        assert offset == pytest.approx(x*math.cos(phase) + p*math.sin(phase)/(1.4*.04))


def test_zero_thickness_relay_faces_are_independent_and_unstable_mode_is_empty():
    relay = {"kind":"matrix", "name":"relay", "matrix_t":[-2,0,0,-.5], "matrix_s":[1,0,0,1]}
    plane = analyze_system(model([gap(5),relay,gap(5)],t_offset_mm=.25))["planes"]["tangential"]
    assert plane["center_profile"] == [[0,.25],[5,.25],[5,-.5],[10,-.5]]
    assert plane["center_profile_breaks"] == [2]
    unstable = {"mode":"standing_wave", "wavelength_nm":1000,
                "elements":[{"kind":"mirror","name":"M1"},gap(100),{"kind":"mirror","name":"M2","radius_mm":50}]}
    plane = analyze_system(unstable)["planes"]["tangential"]
    assert plane["center_basis"] == "reference_axis" and plane["center_profile"] == []


def test_preview_and_saved_branch_agree_with_station_and_use_its_index(client):
    cid = client.post("/api/cases", json={"title":"registered centers"}).json()["id"]
    root = f"/api/cases/{cid}/optical-project"
    system = model([{**gap(10), "uid":"air"}, {**gap(30,1.5), "uid":"sample"}],t_offset_mm=-.1,t_angle_mrad=4)
    side = {"uid":"pump","name":"pump","target_uid":"sample","target_n":2,"wavelength_nm":500,
            "tangential":{},"launch_ray":{"t_offset_mm":.4,"t_angle_mrad":-10},"elements":[gap(15)]}
    data = {"system":system, "incident_paths":[side],
            "observation_planes":[{"uid":"eye","name":"exit","target_uid":"sample","depth_mm":30}]}
    preview = client.post(root+"/preview",json=data)
    assert preview.status_code == 200, preview.text
    saved = client.post(root,json={"project":{"title":"sample", **data}, "change_note":"ray registration"})
    assert saved.status_code == 200, saved.text
    for result in (saved.json()["analysis"], preview.json()):
        host = result["planes"]["tangential"]
        branch = result["sections"]["paths"][0]["axes"]["tangential"]
        assert host["center_profile"][-1] == pytest.approx([40,-.1+.004*(10+30/1.5)])
        assert branch["center_profile"][0] == pytest.approx([-5,.4])
        assert branch["center_profile"][-1] == pytest.approx([40,.4-.01*(15+30/2)])
        station = result["sections"]["planes"][0]["beams"]
        for trace, reading in zip((host,branch),station,strict=True):
            assert reading["field_axes"]["tangential"]["center_mm"] == pytest.approx(trace["center_profile"][-1][1])


def test_incomplete_index_only_transports_the_independently_known_ray_components():
    data = model([gap(5),gap(25,None),{**gap(10),"uid":"receiver"}],t_offset_mm=.4,t_angle_mrad=0,s_offset_mm=.7,s_angle_mrad=4)
    station = {"uid":"eye","name":"eye","target_uid":"receiver","depth_mm":7}
    output = analyze_editable(EditableLayout(system=data, observation_planes=[station]))
    assert not output["planes"] and output["status"] == "partial"
    partial = output["primary_center_axes"]
    assert partial["tangential"]["center_profile"] == [[0,.4],[5,.4],[30,.4],[40,.4]]
    assert partial["sagittal"]["center_profile"][-1] == pytest.approx([5,.72])
    reading = output["sections"]["planes"][0]["beams"][0]["field_axes"]
    assert reading["tangential"]["center_mm"] == .4
    assert reading["sagittal"]["center_mm"] is None and reading["sagittal"]["reduced_angle"] == .004
    branch = {"uid":"arm","name":"arm","target_uid":"receiver","target_n":1, "launch_ray":data["source"]["launch_ray"],
              "elements":[gap(5),gap(25,None)]}
    other = analyze_editable(EditableLayout(system=data, incident_paths=[branch], observation_planes=[station]))
    assert other["sections"]["status"] == "partial" and other["sections"]["paths"][0]["status"] == "partial"
    reading = other["sections"]["planes"][0]["beams"][1]["field_axes"]
    assert reading["tangential"]["center_mm"] == .4 and reading["sagittal"]["center_mm"] is None

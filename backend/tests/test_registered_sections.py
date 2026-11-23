import hashlib
import io
import json
import math
import zipfile
from copy import deepcopy

import pytest

from app.workbench.observation import OpticalLayout, analyze_sections
from app.workbench.optics import analyze_system


def layout():
    def gap(uid, length, n=1):
        return {"uid": uid, "kind": "space", "name": uid, "length_mm": length, "n": n}
    def mirror(uid, r=None):
        return {"uid": uid, "kind": "mirror", "name": uid, "radius_mm": r}
    return {"system": {"mode":"standing_wave","wavelength_nm":1064,"elements":[mirror("left"),gap("air",20),
            gap("sample",10,1.8),gap("end",40),mirror("right",200)]},
            "incident_paths":[{"uid":"pump","name":"808 pump","target_uid":"sample","wavelength_nm":808,"target_n":1.85,
                  "tangential":{"radius_mm":.3,"wavefront":"waist","m2":1},
                  "sagittal":{"radius_mm":.42,"wavefront":"curved","curvature_mm":-180,"m2":1.4},
                  "elements":[gap("a",30),{"kind":"lens","name":"L","focal_mm":50},gap("b",60)]}],
            "observation_planes":[{"uid":"obs","name":"inside","target_uid":"sample","depth_mm":4}]}


def solve(data):
    config = OpticalLayout.model_validate(data)
    return analyze_sections(config, analyze_system(config.system, profile=False))


@pytest.mark.parametrize("nested", [False, True])
def test_preview_reports_only_the_fault_in_the_submitted_layout(client, nested):
    case_id = client.post("/api/cases", json={"title": "编辑中的光路"}).json()["id"]
    system = {"mode": "single_pass", "wavelength_nm": 780, "input_radius_mm": .3,
              "elements": [{"kind": "space", "name": "输入", "length_mm": 10},
                           {"kind": "lens", "name": "可调镜片", "focal_mm": 0}]}
    response = client.post(f"/api/cases/{case_id}/optical-project/preview", json={"system": system} if nested else system)
    assert response.status_code == 422
    (issue,) = response.json()["detail"]
    assert issue["loc"][-2:] == ["elements", 1]
    assert "可调镜片" in issue["msg"] and "焦距" in issue["msg"]


def test_two_colour_section_matches_independent_ray_transfer_and_resonator():
    data = layout()
    result = solve(data)
    assert result["status"] == "completed"
    obs = result["planes"][0]
    assert obs["z_mm"] == 24
    signal, pump = obs["beams"]
    length = 20+10/1.8+40
    zr = math.sqrt(length*(200-length))
    w0 = math.sqrt(.001064*zr/math.pi)
    expected_signal = w0*math.hypot(1, (20+4/1.8)/zr)
    assert signal["radii_mm"]["tangential"] == pytest.approx(expected_signal, rel=1e-12)
    after = 60+4/1.85
    a, b = 1-after/50, 30+after*(1-30/50)
    for axis,w,r,m2 in [("tangential",.3,math.inf,1),("sagittal",.42,-180,1.4)]:
        assert pump["radii_mm"][axis] == pytest.approx(math.hypot(w*(a+b/r),b*m2*.000808/(math.pi*w)),rel=1e-12)
    trace=result["paths"][0]
    assert trace["source_z_mm"] == -70 and trace["entrance_z_mm"] == 20
    assert trace["axes"]["tangential"]["profile"][-1][0] == 30


@pytest.mark.parametrize("drift", [0, 13])
def test_incident_element_exits_disambiguate_coincident_surfaces_without_observers(drift):
    data = layout()
    data["observation_planes"] = []
    path = data["incident_paths"][0]
    path["sagittal"] = None
    elements = [{"name": "cylindrical telescope", "kind": "matrix",
                 "matrix_t": [2, 0, 0, .5], "matrix_s": [.5, 0, 0, 2]},
                {"name": "inverse telescope", "kind": "matrix",
                 "matrix_t": [.5, 0, 0, 2], "matrix_s": [2, 0, 0, .5]}]
    if drift:
        elements.append({"name": "air", "kind": "space", "length_mm": drift})
    path["elements"] = elements
    axes = solve(data)["paths"][0]["axes"]
    for axis, factor in [("tangential", 2), ("sagittal", .5)]:
        expected = [.3 * factor, .3]
        if drift:
            expected.append(.3 * math.hypot(1, drift * .000808 / (math.pi * .3**2)))
        assert axes[axis]["exit_radii_mm"] == pytest.approx(expected, rel=1e-12)
        coincident = [r for z, r in axes[axis]["profile"] if z == 20-drift]
        assert coincident[0] == pytest.approx(.3)
        assert any(math.isclose(r, .3*factor) for r in coincident)
        assert coincident[-1] == pytest.approx(.3)
    path["tangential"]["wavefront"] = "unknown"
    partial = solve(data)["paths"][0]["axes"]["tangential"]
    assert partial["profile"] == [[20-drift, .3]]
    assert partial["exit_radii_mm"] is None


def test_unknown_wavefront_and_dispersion_preserve_independent_results():
    data = layout()
    path = data["incident_paths"][0]
    path["tangential"]["wavefront"] = "unknown"
    path["target_n"] = None
    data["observation_planes"].append({"uid":"front","name":"front","target_uid":"sample","depth_mm":0})
    res = solve(data)
    assert res["status"] == "partial"
    inside, front = res["planes"]
    assert inside["beams"][0]["radii_mm"]
    assert inside["beams"][1]["radii_mm"] == {}
    assert "波前" in inside["beams"][1]["notes"]["tangential"]
    assert "折射率" in inside["beams"][1]["notes"]["sagittal"]
    assert "sagittal" in front["beams"][1]["radii_mm"]
    data["system"]["elements"][-1]["radius_mm"] = 25
    res = solve(data)
    assert res["planes"][1]["beams"][0]["radii_mm"] == {}
    assert "sagittal" in res["planes"][1]["beams"][1]["radii_mm"]


def test_invalid_anchor_depth_and_direct_source():
    data = layout()
    path = data["incident_paths"][0]
    path["elements"] = []
    path["sagittal"] = None
    data["observation_planes"][0]["depth_mm"] = 0
    assert solve(data)["planes"][0]["beams"][1]["radii_mm"] == pytest.approx({"tangential":.3,"sagittal":.3})
    data["observation_planes"][0]["depth_mm"] = 11
    assert "z_mm" not in solve(data)["planes"][0]
    data["system"]["elements"][2]["uid"] = "replacement"
    assert solve(data)["paths"][0]["message"] and not solve(data)["planes"][0]["beams"]


def test_source_plane_distinguishes_given_width_from_a_downstream_prediction():
    data = layout()
    path = data["incident_paths"][0]
    path.update(elements=[], sagittal=None)
    path["tangential"]["wavefront"] = "unknown"
    data["observation_planes"].insert(0, {"uid": "front", "name": "entrance", "target_uid": "sample", "depth_mm": 0})
    front, interior = solve(data)["planes"]
    assert front["beams"][1]["radii_mm"] == {"tangential": .3, "sagittal": .3}
    assert front["beams"][1]["radius_origin"] == dict.fromkeys(("tangential", "sagittal"), "source_input")
    assert not interior["beams"][1]["radii_mm"]
    assert "波前" in interior["beams"][1]["notes"]["sagittal"]


def test_unrepresentable_source_preserves_other_axis_and_cavity():
    data = layout()
    data["incident_paths"][0]["tangential"]["radius_mm"] = 1e300
    result = solve(data)
    assert result["status"] == "partial"
    primary, pump = result["planes"][0]["beams"]
    assert len(primary["radii_mm"]) == 2 and set(pump["radii_mm"]) == {"sagittal"}
    assert pump["notes"]["tangential"]
    json.dumps(result, allow_nan=False)


def test_registration_sources_comparability_and_replay(client, db):
    case = client.post("/api/cases",json={"title":"planes"}).json()["id"]
    other = client.post("/api/cases",json={"title":"foreign"}).json()["id"]
    base = f"/api/cases/{case}/optical-project"
    project = {"title":"bench",**layout()}
    project["incident_paths"][0]["sagittal"]["second_moment_radius"] = True
    saved = client.post(base,json={"project":project,"change_note":"measurement model"}).json()
    assert "revision_id" in saved, saved
    rid = saved["revision_id"]
    preview = client.post(base+"/preview",json={k:v for k,v in project.items() if k != "title"}).json()
    saved_planes = deepcopy(saved["analysis"]["sections"]["planes"])
    for persisted, draft in zip(saved_planes, preview["sections"]["planes"], strict=True):
        for recorded, pending in zip(persisted["beams"], draft["beams"], strict=True):
            for link in ("continuation_port","layout_prefix_port"):
                assert link not in pending
                key = recorded.pop(link, None)
                if key:
                    offer = client.get(f"/api/cases/{case}/studies/project-outputs",params={"revision_id":rid,"port":key}).json()["outputs"][key]
                    assert offer["plane_uid"] == persisted["uid"] and offer["path_uid"] == recorded["uid"]
    assert preview["sections"]["planes"] == saved_planes
    original = b"axis,diameter_um\nT,250\n"
    def put(case_id):
        return client.post(f"/api/cases/{case_id}/attachments",files={"file":("beam.csv",original,"text/csv")}).json()["id"]
    file_id = put(case)
    args = {"revision_id":rid,"plane_uid":"obs","beam_uid":"pump","attachment_id":file_id,"locator":"row 2","conditions":"test A",
            "wavelength_nm":808,"definition":"diameter_1e2","axes_aligned":True,"unit":"um","tangential":250}
    def record(**updates):
        return client.post(base+"/beam-readings",json={**args,**updates})
    accepted=record().json()
    assert accepted["radii_mm"] == {"tangential":.125}
    assert accepted["measured_minus_model_mm"]["tangential"] == pytest.approx(.125-accepted["prediction_mm"]["tangential"])
    assert accepted["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert record().json()["id"] == accepted["id"]
    assert record(attachment_id=put(other)).status_code == 422
    assert record(plane_uid="gone").status_code == 422
    assert not record(wavelength_nm=1064).json()["measured_minus_model_mm"]
    assert not record(definition="fwhm_diameter").json()["measured_minus_model_mm"]
    assert len(client.get(f"{base}/{rid}/beam-readings").json()) == 3
    unaligned = record(axes_aligned=False).json()
    assert unaligned["tangential"] == 250 and not unaligned["measured_minus_model_mm"]
    mixed = record(sagittal=314).json()
    assert set(mixed["measured_minus_model_mm"]) == {"tangential"}
    assert "sagittal" in mixed["axis_notes"]
    d4sigma = record(sagittal=314, definition="d4sigma_diameter").json()
    assert d4sigma["radii_mm"]["sagittal"] == .157
    assert set(d4sigma["measured_minus_model_mm"]) == {"tangential", "sagittal"}
    changed=deepcopy(project)
    changed["observation_planes"][0]["depth_mm"]=5
    new=client.post(base,json={"base_revision_id":rid,"project":changed,"change_note":"different depth"}).json()["revision_id"]
    assert client.get(f"{base}/{new}/beam-readings").json() == []
    assert client.get(f"{base}/{rid}/beam-readings").json()[0]["z_mm"]==24
    with zipfile.ZipFile(io.BytesIO(client.get(f"/api/cases/{case}/bundle").content)) as bundle:
        manifest=json.loads(bundle.read("manifest.json"))
        assert any(x["id"]==accepted["id"] for x in manifest["generated_contents"])
    from app.models import Attachment
    db.get(Attachment, file_id).content_hash = "0" * 64
    db.commit()
    damaged = record(conditions="another run")
    assert damaged.status_code == 422 and "校验" in damaged.text

import math

import pytest

from app.workbench.authoring import EditableLayout, analyze_editable
from app.workbench.beam_readings import registration_site


def test_register_before_prediction_and_compare_after_completing_model(client):
    cid = client.post("/api/cases",json={"title":"相机先到"}).json()["id"]
    root = f"/api/cases/{cid}"
    api = root+"/optical-project"
    original = client.post(root+"/attachments",files={"file":("camera.csv",b"FWHM_um\n180\n","text/csv")}).json()
    project = {"title":"相机工位","system":{"mode":"single_pass","elements":[{"kind":"space","uid":"air","name":"空气","length_mm":80}]},
               "observation_planes":[{"uid":"sensor","name":"相机","position_reference":"fixed_z","z_mm":60}]}
    old = client.post(api,json={"project":project,"change_note":"记录相机位置"}).json()
    raw = {"revision_id":old["revision_id"],"plane_uid":"sensor","beam_uid":"primary","attachment_id":original["id"],
           "locator":"第2行","conditions":"相机固定","wavelength_nm":635,"definition":"fwhm_diameter","unit":"um",
           "axes_aligned":True,"tangential":180}
    response = client.post(api+"/beam-readings",json=raw)
    assert response.status_code == 200, response.text
    reading = response.json()
    assert reading["z_mm"] == 60 and reading["native_widths"] == {"tangential":{"measured":180}}
    assert not reading["prediction_mm"] and not reading["radii_mm"]
    assert "635 nm" in reading["comparison_note"]
    assert client.post(api+"/beam-readings",json={**raw,"beam_uid":"missing"}).status_code == 422
    project["system"].update(wavelength_nm=635,input_radius_mm=.16)
    new = client.post(api,json={"project":project,"base_revision_id":old["revision_id"],"change_note":"补入光源"}).json()
    checked = client.post(f"{api}/{new['revision_id']}/beam-comparisons",json={"reading_id":reading["id"],
        "position_reference":"element_depth","registration_note":"同一相机、同一几何与光束","data_role":"held_out"})
    assert checked.status_code == 200, checked.text
    result = checked.json()
    assert result["reference_prediction"]["status"] == "needs_input"
    measured = result["target_prediction"]["widths"]["tangential"]
    w = .16 * math.hypot(1,60*.000635/(math.pi*.16**2))
    assert measured["predicted"] == pytest.approx(w*math.sqrt(2*math.log(2))*1000)
    assert measured["residual"] == pytest.approx(180-measured["predicted"])
    assert client.get(f"{api}/{old['revision_id']}/beam-readings").json()[0] == reading


def test_independent_incoming_beam_reaches_only_its_registered_path_through_unfinished_host():
    host = {"mode":"standing_wave","elements":[{"kind":"space","uid":"sample","name":"样品前","length_mm":50},
            {"kind":"lens","uid":"unknown","name":"焦距待测"},
            {"kind":"space","uid":"after","name":"透镜后","length_mm":20}]}
    path = {"uid":"probe","name":"探测光","wavelength_nm":532,"target_uid":"sample","through_uid":"after",
            "target_n":1,"downstream":[{"target_uid":"after","n":1}],"tangential":{"m2":1,"radius_mm":.25,"wavefront":"waist"}}
    planes = [{"uid":uid,"name":uid,"target_uid":uid,"depth_mm":10} for uid in ["sample","after"]]
    result = analyze_editable(EditableLayout(system=host,incident_paths=[path],observation_planes=planes))
    assert result["status"] == "partial" and not result["planes"]
    first,last = result["sections"]["planes"]
    assert not first["beams"][0]["radii_mm"]
    radius = .25*math.hypot(1,10*.000532/(math.pi*.25**2))
    assert first["beams"][1]["radii_mm"]["tangential"] == pytest.approx(radius)
    assert not last["beams"][1]["radii_mm"] and "焦距" in str(last["beams"][1]["notes"])


def test_legacy_revision_with_no_saved_sections_resolves_geometry_without_recomputing_a_prediction():
    project = {"system":{"mode":"single_pass","wavelength_nm":635,"input_radius_mm":.2,"elements":[
        {"kind":"space","uid":"one","name":"前段","length_mm":30},{"kind":"space","uid":"two","name":"后段","length_mm":40}]},
        "incident_paths":[{"uid":"arm","name":"探测光","target_uid":"one","wavelength_nm":532,"tangential":{}}],
        "observation_planes":[{"uid":"here","name":"探头","position_reference":"fixed_z","z_mm":55},
                              {"uid":"face","name":"界面","position_reference":"fixed_z","z_mm":30}]}
    revision = {"project":project,"analysis":{"planes":{}}}
    site, beam = registration_site(revision,"here","primary")
    assert site == {"target_uid":"two","depth_mm":25,"z_mm":55} and not beam["radii_mm"]
    with pytest.raises(ValueError,match="未经过"):
        registration_site(revision,"here","arm")
    with pytest.raises(ValueError,match="边界"):
        registration_site(revision,"face","primary")
    assert revision["analysis"] == {"planes":{}}

import pytest

from app.workbench.network import ElementReference
from app.workbench.observation import OpticalLayout
from app.workbench.wave_study import WavePropagation, prepare_field


def create(client, title, project):
    case_id = client.post("/api/cases", json={"title": title}).json()["id"]
    root = f"/api/cases/{case_id}"
    response = client.post(root + "/optical-project", json={"project": {"title": title, **project}, "change_note": "test"})
    assert response.is_success, response.text
    return root, response.json()["revision_id"]


def gap(uid, length):
    return {"kind": "space", "uid": uid, "name": "air", "length_mm": length}


@pytest.mark.parametrize("station", ["attached", "fixed", "exit", "face"])
def test_rehome_cross_domain_edges_in_new_coordinates(client, station):
    system = {"mode": "single_pass", "wavelength_nm": 780, "elements": [gap("pre", 21), gap("cell", 10)],
              "input_radius_mm": 0.4}
    plane = {"uid": "monitor", "name": "sample", "target_uid": "cell", "depth_mm": 7}
    if station == "fixed":
        plane = {"uid": "monitor", "name": "sample", "position_reference": "fixed_z", "z_mm": 28}
    profile = {"kind": "layout", "beam_uid": "primary"}
    if station in ("attached", "fixed"):
        profile["plane_uid"] = "monitor"
    if station == "face":
        profile["element_exit"] = {"scope": "host", "element_uid": "cell"}
    drive = {"at": {"component": "r", "port": "1"}, "path_output": {"path_uid": "primary", "scope": "host", "element_uid": "cell"},
             "profile": profile, "coherence_group": "bench phase lock", "phase_deg": 32}
    source = {"system": system, "observation_planes": [plane], "network": {
        "components": [{"uid": "r", "name": "R", "model": {"kind": "reflector", "reflectance": 0.8, "phase_deg": 0}}],
        "drives": [drive]}}
    root, rev = create(client, "sender", source)
    args = {"project_revision_id": rev, "first_uid": "cell", "last_uid": "cell", "network_component_uids": ["r"], "name": "optics"}
    if station == "fixed":
        assert client.post(root + "/component-models", json=args).json()["reference_planes"] == []
        args["reference_plane_uids"] = ["monitor"]
    captured = client.post(root + "/component-models", json=args)
    assert captured.is_success, captured.text
    model = captured.json()
    assert model["representation"] == "assembly" and model["internal_feeds"] == 1
    assert model["boundary_ports"] == [] and model["reference_planes"] == ["sample"]
    equivalent = client.post(root + "/component-models", json={**args, "reference_plane_uids": ["monitor"]})
    assert equivalent.json()["model_revision_id"] == model["model_revision_id"]
    refs, = model["input_references"]
    assert refs["power_reference"] == "air · 出射面"
    assert refs["field_reference"] == ("sample" if station in ("attached", "fixed") else "air · 出射面")
    dest, dest_rev = create(client, "other bench", {"system": {**system, "elements": [gap("first", 91), gap("tail", 48)]}})
    placed = client.post(dest + "/component-models/preview-placement", json={
        "model_revision_id": model["model_revision_id"], "project_revision_id": dest_rev, "before_uid": "tail"})
    assert placed.is_success, placed.text
    project = placed.json()["project"]
    piece = project["system"]["elements"][1]
    node = project["network"]["components"][0]
    assert piece["component_origin"]["instance_uid"] == node["component_origin"]["instance_uid"]
    assert piece["component_origin"]["ordinal"] != node["component_origin"]["ordinal"]
    new_drive, = project["network"]["drives"]
    assert new_drive["at"]["component"] == node["uid"] and new_drive["path_output"]["element_uid"] == piece["uid"]
    assert new_drive["coherence_group"] != drive["coherence_group"] and new_drive["phase_deg"] == 32
    assert new_drive["power_w"] is None
    copied, = project["observation_planes"]
    assert copied["target_uid"] == piece["uid"] and copied["depth_mm"] == 7 and copied["uid"] != plane["uid"]
    layout = OpticalLayout.model_validate({k: project[k] for k in ("system", "network", "observation_planes")})
    ref = layout.network.drives[0].profile
    if station in ("attached", "fixed"):
        assert ref.plane_uid == copied["uid"]
    else:
        assert ref.element_exit == ElementReference(scope="host", element_uid=piece["uid"])
    _, _, _, _, end, _ = prepare_field(WavePropagation(layout=layout, plane_uid=ref.plane_uid), element_exit=ref.element_exit)
    assert end == pytest.approx(98 if station in ("attached", "fixed") else 101)


def test_one_selection_can_keep_independent_wavelengths_and_external_sources_outside(client):
    system = {"mode": "single_pass", "wavelength_nm": 532, "elements": [gap("p", 7)]}
    network = {"wavelength_nm": 780, "components": [{"uid": "r", "name": "R", "model": {
        "kind": "reflector", "reflectance": 0.5, "phase_deg": 0}}], "drives": [{"at": {"component": "r", "port": "1"}, "power_w": 9}]}
    root, rev = create(client, "two colours", {"system": system, "network": network})
    model = client.post(root + "/component-models", json={"project_revision_id": rev, "first_uid": "p", "last_uid": "p",
                        "network_component_uids": ["r"], "name": "dual"}).json()
    dest, dest_rev = create(client, "recipient", {"system": {**system, "elements": []}})
    preview = client.post(dest + "/component-models/preview-placement", json={"project_revision_id": dest_rev, "model_revision_id": model["model_revision_id"]})
    assert preview.is_success, preview.text
    copy = preview.json()["project"]
    assert copy["network"]["drives"] == []
    assert copy["network"]["components"][0]["component_origin"]["wavelength_nm"] == 780
    assert copy["system"]["elements"][0]["component_origin"]["wavelength_nm"] == 532
    assert model["internal_feeds"] == 0 and len(model["boundary_ports"]) == 1


def test_exit_face_can_move_between_main_and_private_paths_without_binding_to_the_other_beam(client):
    system = {"mode": "single_pass", "wavelength_nm": 780, "elements": [gap("cell", 10)], "input_radius_mm": 0.3}
    node = {"uid": "r", "name": "R", "model": {"kind": "reflector", "reflectance": 0.9, "phase_deg": 0}}
    feed = {"at": {"component": "r", "port": "1"}, "path_output": {"path_uid": "primary", "scope": "host", "element_uid": "cell"},
            "profile": {"kind": "layout", "beam_uid": "primary"}}
    src, rev = create(client, "source", {"system": system, "network": {"components": [node], "drives": [feed]}})
    model = client.post(src + "/component-models", json={"project_revision_id": rev, "first_uid": "cell", "last_uid": "cell",
                        "network_component_uids": ["r"], "name": "module"}).json()
    # Reuse a user-defined ID deliberately in the host and in the arm.
    host = {**system, "wavelength_nm": 1064, "elements": [gap("tail", 8)]}
    arm = {"uid": "incoming", "name": "pump", "target_uid": "tail", "through_uid": "tail", "wavelength_nm": 780,
           "target_n": 1, "tangential": {"radius_mm": 0.4, "m2": 1, "wavefront": "waist"}, "elements": [gap("tail", 40)]}
    dest, dest_rev = create(client, "receiver", {"system": host, "incident_paths": [arm]})
    request = {"project_revision_id": dest_rev, "model_revision_id": model["model_revision_id"], "path_uid": "incoming", "before_uid": "tail"}
    preview = client.post(dest + "/component-models/preview-placement", json=request)
    assert preview.is_success, preview.text
    project = preview.json()["project"]
    layout = OpticalLayout.model_validate({k: project[k] for k in ("system", "network", "incident_paths")})
    drive, = layout.network.drives
    inserted = layout.incident_paths[0].elements[0]
    assert drive.path_output.element_uid == drive.profile.element_exit.element_uid == inserted.uid
    assert drive.path_output.scope == drive.profile.element_exit.scope == "arm"
    assert drive.profile.beam_uid == drive.path_output.path_uid == "incoming"
    wf = WavePropagation(layout=layout, beam_uid="incoming")
    wavelength, _, _, train, end, _ = prepare_field(wf, element_exit=drive.profile.element_exit)
    assert wavelength == 780 and end == -40 and len(train) == 1
    # Both overlapping source IDs resolve uniquely by path scope.
    assert prepare_field(wf, element_exit=ElementReference(scope="arm", element_uid="tail"))[4] == 0
    assert prepare_field(wf, element_exit=ElementReference(scope="host", element_uid="tail"))[4] == 8

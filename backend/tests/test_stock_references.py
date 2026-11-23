from copy import deepcopy

from app.workbench.observation import IncidentPath, LayoutSystem, host_positions, host_route
from app.workbench.stock_references import reconcile_stock_references


def medium(uid, size, n=1.5):
    return {"uid": uid, "kind": "space", "name": uid, "length_mm": size, "n": n}


def transmission(scope, uid):
    return {"scope": scope, "element_uid": uid, "law": {"mode": "net", "retained": {"low": .91}}}


def drive(path, scope, uid):
    ref = {"scope": scope, "element_uid": uid}
    return {"at": {"component": "coupler", "port": path}, "path_output": {"path_uid": path, **ref},
            "profile": {"kind": "layout", "beam_uid": path, "element_exit": ref.copy()}}


def test_replacement_preserves_geometric_ports_and_retires_independent_material_indices():
    before = [medium("core", 5), medium("cap", 1)]
    after = [medium("cap1", 1.5), medium("replacement", 2), medium("cap2", 1.5)]
    route = {"uid": "reference", "name": "reference", "target_uid": "air", "through_uid": "cap",
             "wavelength_nm": 780, "downstream": [{"target_uid": "core", "n": 1.52}],
             "power_budget": {"stages": [transmission("arm", "core"), transmission("host", "core")]}}
    legacy = {"uid": "legacy", "name": "alignment", "target_uid": "core", "target_n": 1.51,
              "downstream": [{"target_uid": "cap", "n": 1.52}, {"target_uid": "out", "n": 1.1}]}
    faces = [{"uid": "front", "name": "front", "target_uid": "core", "depth_mm": 0},
             {"uid": "bond", "name": "bond", "target_uid": "cap", "depth_mm": 0},
             {"uid": "camera", "name": "camera", "position_reference": "fixed_z", "z_mm": 1.2}]
    net = [drive("reference", "host", "cap"), drive("legacy", "host", "core"), drive("reference", "arm", "cap")]
    model = {"incident_paths": [route, legacy], "observation_planes": faces, "network": {"drives": net}}
    original = deepcopy(model)
    messages = reconcile_stock_references(model, "primary", before, after)

    assert route["through_uid"] == "cap2" and route["downstream"] == []
    assert route["power_budget"]["stages"] == [transmission("arm", "core")]
    assert route["independent_index_uids"] == [e["uid"] for e in after]
    system = LayoutSystem(mode="single_pass", wavelength_nm=780, elements=[medium("air", 1, 1), *after, medium("out", 3)])
    active = IncidentPath.model_validate(route)
    traced, issue = host_route(active, host_positions(system), 780)
    assert issue is None and [index for _, _, index in traced] == [1, None, None, None]
    confirmed = {**route, "downstream": [{"target_uid": "replacement", "n": 1.47}]}
    assert host_route(IncidentPath.model_validate(confirmed), host_positions(system), 780)[0][2][2] == 1.47
    assert legacy["target_uid"] == "cap1" and legacy["target_n"] is None
    assert legacy["downstream"] == [{"target_uid": "replacement", "n": None}, {"target_uid": "cap2", "n": None},
                                    {"target_uid": "out", "n": 1.1}]
    assert faces[0]["target_uid"] == "cap1" and faces[1:] == original["observation_planes"][1:]
    assert net[0]["path_output"]["element_uid"] == net[0]["profile"]["element_exit"]["element_uid"] == "cap2"
    assert net[1:] == original["network"]["drives"][1:]
    assert any("bond" in msg for msg in messages)
    assert any("coupler / legacy" in msg for msg in messages)


def test_private_swap_never_retargets_another_path_with_the_same_element_identifier():
    original = {"uid": "lens", "kind": "lens", "clear_aperture": {"width_mm": 4}}
    new = {"uid": "new-lens", "kind": "lens"}
    host, private = transmission("host", "lens"), transmission("arm", "lens")
    ports = [drive("primary", "host", "lens"), drive("a", "arm", "lens"), drive("b", "arm", "lens")]
    project = {"single_pass_power": {"stages": [host]}, "incident_paths": [
        {"uid": name, "name": name, "power_budget": {"stages": [host, private]}} for name in ("a", "b")],
        "network": {"drives": ports}}
    warnings = reconcile_stock_references(project, "a", [original], [new])
    assert project["incident_paths"][0]["power_budget"]["stages"] == [host]
    assert project["incident_paths"][1]["power_budget"]["stages"] == [host, private]
    assert project["single_pass_power"]["stages"] == [host]
    for key, uid in enumerate(["lens", "new-lens", "lens"]):
        assert ports[key]["path_output"]["element_uid"] == ports[key]["profile"]["element_exit"]["element_uid"] == uid
    assert any("净孔径" in msg for msg in warnings)

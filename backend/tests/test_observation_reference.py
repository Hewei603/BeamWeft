"""The same detector frame must reach the optics solver, power ledger and archived records."""

import asyncio
import math
from copy import deepcopy

import pytest

from app.workbench.mode_overlap import ModeOverlap, evaluate_overlap
from app.workbench.observation import OpticalLayout, PowerAnalysis, analyze_sections, power_for_layout
from app.workbench.optics import analyze_system
from app.workbench.power_measurements import meter_targets
from app.workbench.tools import execute


def station(uid, z, side=None):
    return {"uid": uid, "name": uid, "position_reference": "fixed_z", "z_mm": z, "boundary_side": side}


def system():
    return {
        "mode": "single_pass",
        "wavelength_nm": 1064,
        "input_radius_mm": 0.25,
        "elements": [
            {"uid": "air", "name": "入射空气", "kind": "space", "length_mm": 20},
            {"uid": "lens", "name": "透镜", "kind": "lens", "focal_mm": 50},
            {"uid": "sample", "name": "出射段", "kind": "space", "length_mm": 80},
        ],
    }


def test_same_resolved_station_drives_beam_field_and_both_power_entry_points():
    wave = 1064
    first = {"mode": "net", "retained": {"low": 1}}
    loss = {"mode": "bulk", "entry": {"low": 1}, "exit": {"low": 1}, "alpha_per_mm": {"low": 0.03}}
    budget = {
        "wavelength_nm": wave,
        "input_w": {"low": 2},
        "stages": [
            {"scope": "host", "element_uid": uid, "law": law}
            for uid, law in (("air", first), ("lens", first), ("sample", loss))
        ],
    }
    host = system()
    host["elements"][2]["n"] = 1.6
    optics = OpticalLayout(
        system=host,
        single_pass_power=budget,
        observation_planes=[
            station("camera", 30),
            {"uid": "mount", "name": "mount", "target_uid": "sample", "depth_mm": 10},
        ],
        incident_paths=[
            {
                "uid": "other",
                "name": "other",
                "target_uid": "sample",
                "target_n": 1.6,
                "wavelength_nm": wave,
                "tangential": {"m2": 1, "radius_mm": 0.25, "wavefront": "waist"},
                "power_budget": {**budget, "stages": budget["stages"][-1:]},
            }
        ],
    )
    optics.system.elements[0].length_mm = 25
    primary = analyze_system(optics.system, profile=False)
    sections = analyze_sections(optics, primary)
    record = {"revision_id": 2, "project": optics.model_dump(), "analysis": {**primary, "sections": sections}}
    cam, mount = sections["planes"]
    assert (cam["z_mm"], cam["target_uid"], cam["depth_mm"]) == (30, "sample", 5)
    assert (mount["z_mm"], mount["depth_mm"]) == (35, 10)
    assert [b["uid"] for b in cam["beams"]] == ["primary", "other"]
    assert cam["beams"][1]["radii_mm"]["sagittal"] == pytest.approx(
        0.25 * math.hypot(1, 5 * 0.001064 / (1.6 * math.pi * 0.25**2))
    )
    query = ModeOverlap(plane_uid="camera", second_beam_uid="other")
    result = evaluate_overlap(optics, record, query)
    assert result == evaluate_overlap(optics, None, query) | {"revision_id": 2}
    assert result["plane"]["depth_mm"] == 5 and result["coherent"]["status"] == "completed"
    assert sections["power_budgets"] == power_for_layout(optics)
    for path in meter_targets(record):
        target = next(t for t in path["targets"] if t["target"] == {"kind": "plane", "uid": "camera"})
        assert target["z_mm"] == 30 and target["model"]["watts"] == pytest.approx([2 * math.exp(-0.15)] * 2)


def test_interface_with_two_sides_requires_a_choice_and_thick_interior_has_no_invented_result():
    raw = system()
    raw["elements"].append({"uid": "glass", "name": "厚镜", "kind": "thick_lens", "n": 1.5, "length_mm": 4})
    layout = OpticalLayout(
        system=raw,
        observation_planes=[
            station("amb", 20),
            station("before", 20, "upstream"),
            station("after", 20, "downstream"),
            station("thick", 102),
            station("outside", 120),
            station("origin", 0, "upstream"),
        ],
    )
    primary = analyze_system(layout.system, profile=False)
    panels = analyze_sections(layout, primary)["planes"]
    for i in (0, 3, 4, 5):
        assert panels[i]["beams"] == [] and "z_mm" not in panels[i] and "message" in panels[i]
    assert panels[1]["target_uid"] == "air" and panels[2]["target_uid"] == "sample"
    left, right = [p["beams"][0] for p in panels[1:3]]
    assert left["radii_mm"] == pytest.approx(right["radii_mm"])
    key = "reduced_vergence_per_mm"
    assert right["field_axes"]["tangential"][key] - left["field_axes"]["tangential"][key] == pytest.approx(-1 / 50)


def test_invalid_stations_are_reported_consistently_without_discarding_valid_power():
    optics = system()
    valid = {"uid": "valid", "name": "valid", "target_uid": "air", "depth_mm": 5}
    planes = [valid, {**valid, "uid": "shortened", "depth_mm": 25},
              {**valid, "uid": "removed", "target_uid": "missing"},
              {**valid, "uid": "wrong_kind", "target_uid": "lens"}, station("ambiguous", 20)]
    budget = {"wavelength_nm": 1064, "input_w": {"low": 1}, "stages": [
        {"scope": "host", "element_uid": el["uid"], "law": {"mode": "net", "retained": {"low": 1}}}
        for el in optics["elements"]]}
    layout = OpticalLayout(system=optics, observation_planes=planes, single_pass_power=budget)
    sections = analyze_sections(layout, analyze_system(layout.system, profile=False))
    report = asyncio.run(execute("analyze_path_power", PowerAnalysis(layout=layout),
                                 db=None, case=None, task=None, provider=None, session_id=None, attachment_ids=[]))
    expected = {row["uid"]: row["message"] for row in sections["planes"] if "message" in row}
    assert set(expected) == {"shortened", "removed", "wrong_kind", "ambiguous"}
    assert {row["uid"]:row["message"] for row in report["unresolved_planes"]} == expected
    assert report["status"] == "partial"
    assert report["power_budgets"] == sections["power_budgets"]
    valid_power, = report["power_budgets"][0]["planes"]
    assert valid_power["plane_uid"] == "valid" and valid_power["forward"]["watts"] == [1, 1]


def test_fixed_observation_reading_retains_original_local_depth_after_model_edit(client):
    case_id = client.post("/api/cases", json={"title": "台架复验"}).json()["id"]
    url = f"/api/cases/{case_id}/optical-project"
    original = {"title": "基准", "system": system(), "observation_planes": [station("camera", 80)]}
    saved = client.post(url, json={"project": original, "change_note": "建模"}).json()
    rid = saved["revision_id"]
    attachment = client.post(
        f"/api/cases/{case_id}/attachments", files={"file": ("camera.csv", b"radius_um\n60\n", "text/csv")}
    ).json()
    read = client.post(
        url + "/beam-readings",
        json={
            "revision_id": rid,
            "attachment_id": attachment["id"],
            "beam_uid": "primary",
            "plane_uid": "camera",
            "definition": "radius_1e2",
            "unit": "um",
            "tangential": 60,
            "axes_aligned": True,
            "wavelength_nm": 1064,
            "locator": "camera.csv row 2",
            "conditions": "A",
        },
    )
    assert read.status_code == 200, read.text
    reading = read.json()
    assert reading["z_mm"] == 80 and reading["target_uid"] == "sample"
    candidate = deepcopy(saved["project"])
    candidate["system"]["elements"][0]["length_mm"] = 35
    changed = client.post(url, json={"project": candidate, "base_revision_id": rid, "change_note": "移动透镜"}).json()
    for frame, z, depth in (("fixed_z", 80, 45), ("element_depth", 95, 60)):
        res = client.post(
            f"{url}/{changed['revision_id']}/beam-comparisons",
            json={
                "reading_id": reading["id"],
                "position_reference": frame,
                "registration_note": "同一坐标原点及轴方向，比较两种安放条件。",
                "data_role": "different_configuration",
            },
        )
        assert res.status_code == 200, res.text
        assert res.json()["target_prediction"]["z_mm"] == z
        assert res.json()["target_prediction"]["depth_mm"] == depth


def test_fixed_station_accepts_an_anonymous_train_without_needing_an_element_reference():
    raw = system()
    for e in raw["elements"]:
        e.pop("uid")
    layout = OpticalLayout(system=raw, observation_planes=[station("camera", 80)])
    plane = analyze_sections(layout, analyze_system(layout.system, profile=False))["planes"][0]
    assert plane["element_index"] == 2 and plane["z_mm"] == 80 and plane["depth_mm"] == 60
    assert plane["beams"][0]["radii_mm"]["tangential"] > 0

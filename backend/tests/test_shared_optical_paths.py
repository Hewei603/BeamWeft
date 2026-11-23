import math

import numpy as np
import pytest

from app.workbench.observation import OpticalLayout, analyze_layout
from app.workbench.optics import element_catalog
from app.workbench.wave_study import WavePropagation, propagate_field


def space(uid, length, n=1):
    return dict(name=uid, uid=uid, kind="space", length_mm=length, n=n)


def branch(uid, entry, end, wavelength, radius):
    return dict(
        uid=uid,
        name=uid,
        target_uid=entry,
        through_uid=end,
        wavelength_nm=wavelength,
        tangential=dict(radius_mm=radius, wavefront="waist", m2=1),
    )


def apparatus():
    arm = branch("probe", "entry", "cell", 780, 0.24)
    arm.update(
        target_n=1,
        downstream=[{"target_uid": "air", "n": 1}, {"target_uid": "cell", "n": 1.5}],
        launch_ray={"t_offset_mm": 0.1, "t_angle_mrad": 2},
    )
    return dict(
        system=dict(
            mode="single_pass",
            wavelength_nm=1064,
            input_radius_mm=0.32,
            elements=[
                space("entry", 30),
                dict(kind="lens", name="shared", uid="lens", focal_mm=80),
                space("air", 35),
                space("cell", 8, 1.48),
            ],
        ),
        incident_paths=[arm, branch("local", "cell", "cell", 1064, 0.19)],
        observation_planes=[dict(uid="meter", name="detector", target_uid="cell", depth_mm=8)],
    )


def solve(model):
    return analyze_layout(OpticalLayout.model_validate(model))["sections"]


def test_route_diagnostic_survives_save_and_clears_on_reconnect_without_inventory(client):
    case = client.post("/api/cases", json={"title":"Shared path"}).json()["id"]
    url = f"/api/cases/{case}/optical-project"
    layout = apparatus()
    layout["incident_paths"][0]["through_uid"] = "removed"
    issue = solve(layout)["paths"][0]["message"]
    project = {"title":"Two incident beams", **layout}
    saved = client.post(url, json={"project":project, "change_note":"Open route"}).json()
    warning, = [w for w in saved["analysis"]["scope_warnings"] if issue in w]
    assert "probe" in warning
    assert client.get(f"/api/cases/{case}").json()["parameters"]["current_optical_scope_warnings"].count(warning) == 1
    assert not saved["project"]["component_context"]

    project["incident_paths"][0]["through_uid"] = "cell"
    new = client.post(url, json={"project":project, "base_revision_id":saved["revision_id"], "change_note":"Reconnect"}).json()
    assert warning not in new["analysis"].get("scope_warnings", [])
    assert warning not in client.get(f"/api/cases/{case}").json()["parameters"]["current_optical_scope_warnings"]
    archived = client.get(url, params={"revision_id":saved["revision_id"]}).json()["current"]
    assert warning in archived["analysis"]["scope_warnings"]


def measured(model):
    return {b["uid"]: b for b in solve(model)["planes"][0]["beams"]}


def transfer(f, w, wavelength, n):
    distance = 35 + 8 / n
    a, b = 1 - distance / f, 30 + distance - 30 * distance / f
    return math.hypot(w * a, wavelength * 1e-6 * b / (math.pi * w)), a * 0.1 + b * 0.002


def test_shared_edit_affects_only_beams_that_traverse_the_optic():
    model = apparatus()
    unchanged = measured(model)["local"]
    for f in (80, -130):
        model["system"]["elements"][1]["focal_mm"] = f
        beams = measured(model)
        for uid, wl, w, n in (("primary", 1064, 0.32, 1.48), ("probe", 780, 0.24, 1.5)):
            radius, center = transfer(f, w, wl, n)
            assert beams[uid]["radii_mm"]["tangential"] == pytest.approx(radius)
            if uid == "probe":
                assert beams[uid]["field_axes"]["tangential"]["center_mm"] == pytest.approx(center)
        assert beams["local"] == unchanged
        out = propagate_field(
            WavePropagation(layout=model, beam_uid="probe", plane_uid="meter", samples=256, window_mm=3)
        )
        assert out["status"] == "completed"
        edges = np.array(out["grid"]["edges_mm"])
        centers = (edges[1:] + edges[:-1]) / 2
        marginal = np.sum(out["grid"]["density"], axis=0)
        centroid = np.average(centers, weights=marginal)
        width = 2 * math.sqrt(np.average((centers - centroid) ** 2, weights=marginal))
        assert centroid == pytest.approx(center, abs=1e-6)
        assert width == pytest.approx(radius, abs=0.0015)


def test_inserted_devices_follow_geometry_and_missing_glass_data_stays_local():
    model = apparatus()
    glass = dict(
        kind="thick_lens",
        uid="glass",
        name="shared glass",
        length_mm=5,
        n=1.46,
        front_radius_mm=90,
        back_radius_mm=-130,
    )
    model["system"]["elements"].insert(1, glass)
    model["observation_planes"].append(dict(uid="up", name="upstream", target_uid="entry", depth_mm=2))
    out = solve(model)
    assert out["paths"][0]["status"] == "partial"
    assert out["paths"][1]["status"] == "completed"
    assert [r["element_uid"] for r in out["paths"][0]["shared_route"]] == ["entry", "glass", "lens", "air", "cell"]
    probe = out["planes"][0]["beams"][1]
    assert not probe["radii_mm"] and "shared glass" in probe["notes"]["tangential"]
    assert out["planes"][1]["beams"][1]["radii_mm"]
    assert out["planes"][0]["beams"][2]["radii_mm"]
    with pytest.raises(ValueError, match="shared glass"):
        propagate_field(WavePropagation(layout=model, beam_uid="probe"))
    model["incident_paths"][0]["downstream"].append(dict(target_uid="glass", n=1.52))
    beam = measured(model)["probe"]

    def ray(x, p):
        x += 30 * p
        p += (1 - 1.52) * x / 90
        x += 5 * p / 1.52
        p += (1.52 - 1) * x / -130
        p -= x / 80
        return x + (35 + 8 / 1.5) * p

    assert beam["field_axes"]["tangential"]["center_mm"] == pytest.approx(ray(0.1, 0.002))
    assert beam["radii_mm"]["tangential"] == pytest.approx(
        math.hypot(0.24 * ray(1, 0), 0.00078 * ray(0, 1) / (math.pi * 0.24))
    )
    model["system"]["elements"].pop()
    assert "终点" in solve(model)["paths"][0]["message"]


def test_sharing_begins_on_device_and_respects_declared_transfer_in_each_axis():
    model = apparatus()
    device = element_catalog()["presets"]["study:aligned_lens_power"]
    model["system"]["elements"][1] = {**device, "name": "shared cylinder", "uid": "lens"}
    arm = model["incident_paths"][0]
    arm.update(target_uid="lens", target_n=None)
    beam = measured(model)["probe"]
    d = 35 + 8 / 1.5
    for axis, c in (("tangential", -0.01), ("sagittal", 0)):
        expected = math.hypot(0.24 * (1 + c * d), 0.00078 * d / (math.pi * 0.24))
        assert beam["radii_mm"][axis] == pytest.approx(expected)
    (record,) = solve(model)["paths"][0]["evaluated_devices"]
    assert record["wavelength_nm"] == 780 and record["element_uid"] == "lens"


def test_each_wavelength_has_its_own_power_transfer_through_the_same_optic():
    model = apparatus()

    def budget(wavelength, transmission):
        return {
            "wavelength_nm": wavelength,
            "input_w": {"low": 2},
            "stages": [
                {
                    "scope": "host",
                    "element_uid": el["uid"],
                    "law": {"mode": "net", "retained": {"low": transmission if el["uid"] == "lens" else 1}},
                }
                for el in model["system"]["elements"]
            ],
        }

    model["single_pass_power"] = budget(1064, 0.96)
    model["incident_paths"][0]["power_budget"] = budget(780, 0.72)
    main, probe = solve(model)["power_budgets"]
    for ledger, expected in ((main, 1.92), (probe, 1.44)):
        assert ledger["status"] == "completed"
        assert not ledger["unused_stages"]
        assert ledger["rows"][-1]["outgoing"]["watts"] == pytest.approx([expected, expected])
        assert ledger["planes"][0]["forward"]["watts"] == pytest.approx([expected, expected])

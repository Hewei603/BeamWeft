"""Independent field and power predictions for distinct passive optical networks."""

import math

import numpy as np
import pytest

from app.workbench.network import PortNetwork, evaluate_network
from app.workbench.spatial_ports import SpatialDetector, spatial_detector


def at(uid, port):
    return {"component": uid, "port": str(port)}


def coupler(uid):
    return {"uid": uid, "name": uid, "model": {"kind": "coupler", "through_fraction": 0.5}}


def connect(a, b, **settings):
    return {"a": a, "b": b, "length_mm": 0, **settings}


def gaussian(w=0.45, curvature=100):
    axis = {"radius_mm": w, "wavefront": "curved" if curvature else "waist", "curvature_mm": curvature, "m2": 1}
    return {"kind": "gaussian", "tangential": axis}


def run(components, links, drives, target, system=None, samples=128):
    layout = {
        "system": system or {"mode": "single_pass", "wavelength_nm": 1000, "elements": []},
        "network": {"components": components, "links": links, "drives": drives},
    }
    return spatial_detector(SpatialDetector(layout=layout, detector=target, window_mm=4, samples=samples))


@pytest.mark.parametrize("degrees,shift", [(0, 0), (0, 0.2), (90, 0.2), (180, 0.2)])
def test_two_displaced_wavefronts_match_the_entire_analytic_image(degrees, shift):
    components = [coupler("first"), coupler("second")]
    links = [
        connect(at("second", 1), at("first", 3)),
        connect(at("first", 4), at("second", 2), shift_t_mm=shift, phase_deg=degrees),
    ]
    drives = [{"at": at("first", 1), "power_w": 1, "profile": gaussian()}]
    out = run(components, links, drives, at("second", 3), samples=256)
    assert out["status"] == "completed", out["warnings"]
    w, radius, wavelength = 0.45, 100, 0.001
    overlap = math.exp(-0.5 * (shift / w) ** 2 - 0.5 * (math.pi * w * shift / (wavelength * radius)) ** 2)
    assert out["detected_w"] == pytest.approx((1 - overlap * math.cos(math.radians(degrees))) / 2, abs=1e-10)
    edges = np.array(out["grid"]["edges_mm"])
    # Two field samples per displayed cell along each axis at this resolution.
    axis = np.linspace(edges[0], edges[-1], 256, endpoint=False) + (edges[-1] - edges[0]) / 512
    x, y = np.meshgrid(axis, axis)

    def field(u):
        return math.sqrt(2 / math.pi) / w * np.exp((u * u + y * y) * (-1 / w**2 + 1j * math.pi / (wavelength * radius)))

    image = np.abs((field(x) - np.exp(1j * math.radians(degrees)) * field(x - shift)) / 2) ** 2
    pixel_mean = image.reshape(128, 2, 128, 2).mean(axis=(1, 3))
    assert np.max(np.abs(pixel_mean - np.array(out["grid"]["density"]))) < 2e-9
    assert out["external_w"] == pytest.approx(1, abs=1e-12)
    assert sum(p["outgoing_w"] for p in out["ports"]) == pytest.approx(1)


def test_dense_grid_obeys_the_existing_connected_network_memory_budget():
    components = [coupler("first"), coupler("second")]
    links = [connect(at("first", 3), at("second", 1)), connect(at("first", 4), at("second", 2))]
    with pytest.raises(ValueError, match="内存"):
        run(components, links, [{"at": at("first", 1), "power_w": 1, "profile": gaussian()}], at("second", 3), samples=512)


def test_custom_ports_feedback_reordering_and_internal_power():
    # Quarter-wave carrier via reference phase alone. The two identical mirrors
    # form a resonant cavity, with forward circulating power 1/(1-R).
    R = 0.81
    r, t = [0, math.sqrt(R)], [math.sqrt(1 - R), 0]
    law = {"kind": "matrix", "ports": ["outside", "inside"], "matrix": [[r, t], [t, r]]}
    parts = [{"uid": k, "name": "mirror", "model": law, "transverse_action": "uniform"} for k in ("exit", "entry")]
    links = [connect(at("exit", "inside"), at("entry", "inside"), phase_deg=90)]
    drives = [{"at": at("entry", "outside"), "power_w": 1, "profile": gaussian(curvature=None)}]
    out = run(parts, links, drives, at("entry", "inside"), samples=64)
    assert out["detected_w"] == pytest.approx(1 / (1 - R))
    assert out["external_w"] == pytest.approx(1)
    assert next(p["outgoing_w"] for p in out["ports"] if p["component"] == "exit") == pytest.approx(1)


def test_free_space_reciprocal_shift_loss_and_independent_sources():
    model = {"kind": "matrix", "ports": ["in", "out"], "matrix": [[[0, 0], [1, 0]], [[1, 0], [0, 0]]]}
    parts = [{"uid": k, "name": k, "model": model, "transverse_action": "uniform"} for k in ("east", "west")]
    links = [connect(at("west", "out"), at("east", "in"), shift_s_mm=0.3, length_mm=400, n=1.5, power_transmission=0.6)]
    p = 0.7
    for uid, feed, sensor, sgn in [("east", "out", at("west", "in"), -1), ("west", "in", at("east", "out"), 1)]:
        out = run(
            parts, links, [{"at": at(uid, feed), "power_w": p, "profile": gaussian(w=0.35, curvature=None)}], sensor
        )
        image = np.array(out["grid"]["density"])
        edges = np.array(out["grid"]["edges_mm"])
        ys = (edges[:-1] + edges[1:]) / 2
        mean = np.average(ys, weights=image.sum(axis=1))
        width = 2 * np.sqrt(np.average((ys - mean) ** 2, weights=image.sum(axis=1)))
        assert width == pytest.approx(0.35 * math.hypot(1, 400 * 0.001 / (math.pi * 1.5 * 0.35**2)), rel=1e-8)
        assert mean == pytest.approx(sgn * 0.3, abs=1e-9)
        assert out["detected_w"] == pytest.approx(p * 0.6)
        assert out["removed_w"] == pytest.approx(p * 0.4)
    feeds = [{"at": at("C", i), "profile": gaussian(), "power_w": 1} for i in (1, 2)]
    assert run([coupler("C")], [], feeds, at("C", 3))["detected_w"] == pytest.approx(1)
    for drive, phi in zip(feeds, [0, 90]):
        drive.update(coherence_group="ref", phase_deg=phi)
    assert run([coupler("C")], [], feeds, at("C", 3))["detected_w"] < 1e-23


def test_distinguish_unknown_zero_and_unrelated_sources():
    parts = [coupler("active"), coupler("elsewhere")]
    drive = {"at": at("active", 1), "power_w": 1}
    drives = [drive, {"at": at("elsewhere", 1), "power_w": None}]
    assert run(parts, [], drives, at("active", 3))["status"] == "needs_input"
    drive["power_w"] = 0
    off = run(parts, [], drives, at("active", 3))
    assert off["status"] == "completed" and off["detected_w"] == 0 and len(off["ports"]) == 4
    drive.update(power_w=2, profile=gaussian())
    assert run(parts, [], drives, at("active", 3))["detected_w"] == pytest.approx(1)
    parts[0]["transverse_action"] = "matched_mode"
    assert "匹配模式" in run(parts, [], drives, at("active", 3))["message"]
    del parts[0]["transverse_action"]
    drive["profile"]["tangential"]["m2"] = 2
    with pytest.raises(ValueError, match="M²"):
        run(parts, [], drives, at("active", 3))


def test_upstream_aperture_is_carried_as_field_and_port_power_sets_normalization():
    system = {
        "mode": "single_pass",
        "wavelength_nm": 1000,
        "input_radius_mm": 0.6,
        "elements": [
            {"uid": "flight", "name": "drift", "kind": "space", "length_mm": 1},
            {
                "uid": "pupil",
                "name": "slit",
                "kind": "space",
                "length_mm": 0,
                "clear_aperture": {"shape": "rectangle", "width_mm": 0.5, "height_mm": 2, "center_t_mm": 0.25},
            },
        ],
    }
    drives = [{"at": at("readout", 1), "power_w": 2, "profile": {"kind": "layout", "beam_uid": "primary"}}]
    out = run([coupler("readout")], [], drives, at("readout", 3), system)
    image = np.array(out["grid"]["density"])
    x = np.array(out["grid"]["edges_mm"])[:-1]
    assert out["detected_w"] == pytest.approx(1)
    assert image[:, x < -0.05].max() < 1e-20
    assert image[:, (x > 0.03) & (x < 0.2)].max() > 1
    drives[0]["profile"]["plane_uid"] = "not in this project"
    with pytest.raises(ValueError, match="观察面"):
        run([coupler("readout")], [], drives, at("readout", 3), system)


@pytest.mark.parametrize("seed", [1324, 83])
def test_uniform_mode_limit_of_arbitrary_multiport_feedback(seed):
    rng = np.random.default_rng(seed)
    parts, terminals = [], []
    for size in (3, 4, 3):
        uid = f"test/{rng.integers(1000000)}"
        ports = [f"λ {i}" for i in range(size)]
        basis, _ = np.linalg.qr(rng.normal(size=(size, size)) + 1j * rng.normal(size=(size, size)))
        matrix = 0.8 * basis
        model = {"kind": "matrix", "ports": ports, "matrix": np.stack([matrix.real, matrix.imag], axis=-1).tolist()}
        parts.append({"uid": uid, "name": "same label", "model": model, "transverse_action": "uniform"})
        terminals.extend(at(uid, p) for p in ports)
    rng.shuffle(terminals)
    links = [connect(a, b, phase_deg=rng.uniform(-180, 180)) for a, b in zip(terminals[2::2], terminals[3::2])]
    drive = {"at": terminals[0], "profile": gaussian(curvature=None), "power_w": 2}
    # The ordinary port solver independently factors a single global sparse matrix.
    scalar = evaluate_network(PortNetwork(components=parts, links=links, drives=[drive]), 1000)
    reference = next(p for p in scalar["ports"] if at(p["component"], p["port"]) == terminals[1])["outgoing_w"]
    out = run(parts[::-1], links[::-1], [drive], terminals[1], samples=64)
    assert out["detected_w"] == pytest.approx(reference, abs=1e-12)
    assert out["external_w"] + out["removed_w"] == pytest.approx(out["input_w"])

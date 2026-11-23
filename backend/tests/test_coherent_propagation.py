import math

import numpy as np
import pytest
from scipy.integrate import quad
from scipy.special import erf, j0

from app.study_api import ElementLaw, MissingStudyInput, StrictInput, Study
from app.workbench import tools
from app.workbench.detector_position import _travel
from app.workbench.observation import analyze_layout, host_positions
from app.workbench.optics import OpticalElement, PropagationStudySystem
from app.workbench.studies import _propagate
from app.workbench.wave_study import WavePropagation, propagate_field


def gap(uid, length, **fields):
    return dict(uid=uid, name=uid, kind="space", length_mm=length, **fields)


def setup(elements, *, radius=0.4, **fields):
    return {"system": dict(mode="single_pass", input_radius_mm=radius, wavelength_nm=1000, elements=elements), **fields}


def field(layout, **settings):
    return propagate_field(WavePropagation(layout=layout, **settings))


def grid(output):
    edges = np.array(output["grid"]["edges_mm"])
    return (edges[1:] + edges[:-1]) / 2, np.array(output["grid"]["density"])


def test_main_source_transports_two_distinct_wavefronts_and_their_chief_ray():
    length = 50.
    main = setup([gap("free", length)], radius=None)
    source = {
        "tangential": {"radius_mm": .25, "wavefront": "curved", "curvature_mm": -100, "m2": 1},
        "sagittal": {"radius_mm": .43, "wavefront": "waist", "m2": 1},
        "launch_ray": {"t_offset_mm": .14, "t_angle_mrad": 1, "s_offset_mm": -.2, "s_angle_mrad": -.4},
    }
    main["system"]["source"] = source
    result = field(main, window_mm=3, samples=256)
    coords, intensity = grid(result)
    for marginal, center, w0, vergence in (
        (intensity.sum(axis=0), .14+.001*length, .25, -1/100),
        (intensity.sum(axis=1), -.2-.0004*length, .43, 0),
    ):
        assert np.average(coords, weights=marginal) == pytest.approx(center, abs=1e-5)
        radius = 2 * math.sqrt(np.average((coords-center)**2, weights=marginal))
        expected = w0 * math.hypot(1+length*vergence, length*.001/(math.pi*w0**2))
        # The displayed density is downsampled to a cell-averaged raster.
        assert radius == pytest.approx(expected, abs=.002)

    source["sagittal"]["wavefront"] = "unknown"
    with pytest.raises(MissingStudyInput, match="S.*波前"):
        field(main, samples=128)
    source["sagittal"].update(wavefront="waist", m2=2)
    with pytest.raises(ValueError, match="复振幅"):
        field(main, samples=128)


@pytest.mark.parametrize("a", [0.25, 2.0])
def test_centered_circular_pupil_throughput_is_referenced_to_unbounded_incident_power(a):
    layout = setup([gap("stop", 0, clear_aperture={"shape": "ellipse", "width_mm": 2 * a}), gap("out", 1)], radius=1)
    output = field(layout, samples=512, window_mm=5)
    expected = 1 - math.exp(-2 * a * a)
    assert output["throughput"] == pytest.approx(expected, abs=0.0012)
    assert output["rows"][0]["before"] == pytest.approx(1, abs=1e-10)
    assert output["rows"][0]["after"] == pytest.approx(output["throughput"], abs=2e-14)


def test_offset_rectangles_intersect_on_the_same_field():
    pupil = dict(shape="rectangle", width_mm=0.75, height_mm=1.25)
    layout = setup(
        [
            gap("left", 0, clear_aperture={**pupil, "center_t_mm": -0.125}),
            gap("right", 0, clear_aperture={**pupil, "center_t_mm": 0.125}),
            gap("detector", 0.1),
        ]
    )
    # Intersection is [-.25,.25] x [-.625,.625]. Each stop on its own passes more.
    out = field(layout, window_mm=4, samples=256)
    expected = erf(math.sqrt(2) * 0.25 / 0.4) * erf(math.sqrt(2) * 0.625 / 0.4)
    assert out["throughput"] == pytest.approx(expected, abs=0.0003)
    assert out["rows"][1]["before"] == out["rows"][0]["after"]
    assert out["rows"][1]["after"] < out["rows"][1]["before"]
    wide = field(setup([gap("rotated", 1, clear_aperture={**pupil, "angle_deg": 90})]))
    normal = field(setup([gap("normal", 1, clear_aperture=pupil)]))
    assert np.allclose(grid(wide)[1], grid(normal)[1].T)


def test_focal_diffraction_matches_independent_continuous_hankel_integral():
    f, a, w, lam = 150, 0.3, 0.4, 0.001
    layout = setup(
        [
            dict(name="lens", kind="lens", focal_mm=f, clear_aperture={"shape": "ellipse", "width_mm": 2 * a}),
            gap("out", f),
        ]
    )
    out = field(layout, samples=512, window_mm=5)
    assert out["status"] == "completed", out["warnings"]
    coords, intensity = grid(out)
    k = 2 * math.pi / (lam * f)

    def expected(r):
        amplitude = (
            k * math.sqrt(2 / math.pi) / w * quad(lambda t: t * math.exp(-((t / w) ** 2)) * j0(k * t * r), 0, a)[0]
        )
        return amplitude**2

    center = len(coords) // 2
    profile = np.array([expected(math.hypot(x, coords[center])) for x in coords])
    assert np.max(np.abs(profile - intensity[center])) / profile.max() < 0.02
    # Locate the first minimum and subsequent side lobe in the independent reference.
    falling = np.diff(profile[center:]) < 0
    minimum = center + 1 + np.flatnonzero(falling[:-1] & ~falling[1:])[0]
    peak = center + 1 + np.flatnonzero(~falling[:-1] & falling[1:])[0]
    assert minimum < peak
    assert intensity[center, minimum] < 0.35 * intensity[center, peak]
    assert intensity[center, peak] == pytest.approx(profile[peak], rel=0.15)


def test_rectangle_edges_away_from_grid_lines_match_continuous_integral():
    radius = 0.4
    pupil = dict(shape="rectangle", width_mm=0.8, height_mm=0.4, center_t_mm=0.1)
    out = field(setup([gap("pupil", 2, clear_aperture=pupil)], radius=radius), samples=512, window_mm=5)
    k = math.sqrt(2) / radius
    expected = (erf(k * 0.5) - erf(-k * 0.3)) * erf(k * 0.2) / 2
    assert out["throughput"] == pytest.approx(expected, abs=0.001)


def test_new_registered_phase_model_uses_the_project_wavelength_and_both_axes(monkeypatch):
    class Inputs(StrictInput):
        vacuum_nm: float

    def powers(p):
        ct = -1 / (125 * p.vacuum_nm / 1000)
        return {"transfer": {"matrix_t": [1, 0, ct, 1], "matrix_s": [1, 0, 0, 1]}}

    law = ElementLaw(
        representation="ray_matrix", wavelength_input="vacuum_nm", output="transfer", scalar_transfer="quadratic_phase"
    )
    device = Study(
        "dispersive_phase", "Test phase", "1", "Ideal cylindrical phase screen", Inputs, powers, element_law=law
    )
    monkeypatch.setitem(tools.STUDIES, device.name, (device, "instrument-lab"))
    plate = dict(
        kind="registered",
        name="Plate",
        prescription={"study": device.name, "provider": "instrument-lab", "version": "1"},
    )
    distance, radius = 80, 0.4
    layout = setup([plate, gap("end", distance)], radius=radius)
    layout["system"]["wavelength_nm"] = 800
    coords, density = grid(field(layout, window_mm=4, samples=256))
    for power, marginal in ((-1 / 100, density.sum(axis=0)), (0, density.sum(axis=1))):
        predicted = radius * math.hypot(1 + distance * power, distance * 0.0008 / (math.pi * radius**2))
        assert 2 * math.sqrt(np.average(coords**2, weights=marginal)) == pytest.approx(predicted, abs=0.002)

    invalid = Study(
        "invalid_phase",
        "Invalid phase",
        "1",
        "Contradictory metadata",
        Inputs,
        lambda _: {"transfer": {"matrix_t": [1, 10, 0, 1]}},
        element_law=law,
    )
    monkeypatch.setitem(tools.STUDIES, invalid.name, (invalid, "instrument-lab"))
    plate["prescription"]["study"] = invalid.name
    with pytest.raises(ValueError, match="薄相位面"):
        field(layout)


def test_curved_input_thick_lens_and_oblique_mirror_match_reduced_ray_transfer():
    thick = dict(kind="thick_lens", name="glass", front_radius_mm=80, back_radius_mm=-140, length_mm=6, n=1.55)
    mirror = dict(kind="mirror", name="reflector", radius_mm=-240, aoi_deg=30)
    layout = setup([thick, mirror, gap("end", 60)])
    layout["system"]["input_curvature_mm"] = -280
    out = field(layout, samples=512, window_mm=3)
    assert out["status"] == "completed", out["warnings"]
    coords, density = grid(out)

    def kick(c):
        return np.array([[1, 0], [c, 1]])

    def travel(b):
        return np.array([[1, b], [0, 1]])

    glass = kick((1.55 - 1) / -140) @ travel(6 / 1.55) @ kick((1 - 1.55) / 80)
    waist = 0.4
    q = 1 / (1 / -280 - 1j * 0.001 / (math.pi * waist**2))
    for r, marginal in (
        (-240 * math.cos(math.pi / 6), density.sum(axis=0)),
        (-240 / math.cos(math.pi / 6), density.sum(axis=1)),
    ):
        (a, b), (c, d) = travel(60) @ kick(-2 / r) @ glass
        inv_q = (c * q + d) / (a * q + b)
        radius = math.sqrt(-0.001 / (math.pi * inv_q.imag))
        assert 2 * math.sqrt(np.average(coords**2, weights=marginal)) == pytest.approx(radius, abs=0.0015)


def test_incident_ray_at_registered_depth_uses_its_wavelength_and_its_index():
    arm = {
        "uid": "other",
        "name": "probe",
        "wavelength_nm": 800,
        "target_uid": "host",
        "target_n": 1.5,
        "tangential": {"radius_mm": 0.25, "m2": 1, "wavefront": "waist"},
        "launch_ray": {"t_offset_mm": 0.05, "s_offset_mm": -0.1, "t_angle_mrad": 2, "s_angle_mrad": 1},
    }
    plane = {"uid": "sensor", "name": "sensor", "target_uid": "host", "depth_mm": 75}
    layout = setup([gap("host", 100, n=1.8)], incident_paths=[arm], observation_planes=[plane])
    out = field(layout, beam_uid="other", plane_uid="sensor")
    assert out["status"] == "completed" and out["z_mm"] == 75
    c, d = grid(out)
    sx = d.sum(axis=0)
    sy = d.sum(axis=1)
    for p, center in ((sx, 0.15), (sy, -0.05)):
        assert sum(c * p) / sum(p) == pytest.approx(center, abs=1e-7)
        measured = 2 * math.sqrt(sum((c - center) ** 2 * p) / sum(p))
        predicted = 0.25 * math.hypot(1, 75 * 0.0008 / (1.5 * math.pi * 0.25**2))
        assert measured == pytest.approx(predicted, rel=0.003)
    arm["tangential"]["m2"] = 2
    with pytest.raises(ValueError, match="M²"):
        field(layout, beam_uid="other")


def test_sampling_warnings_and_local_scope_of_gaussian_reference():
    stop = gap("pupil", 0, clear_aperture={"shape": "ellipse", "width_mm": 0.002})

    def plane(name):
        return {"uid": name, "name": name, "target_uid": name, "depth_mm": 1}

    layout = setup([gap("before", 2), stop, gap("after", 2)], observation_planes=[plane("before"), plane("after")])
    out = field(layout, samples=128)
    assert out["status"] == "partial" and any("采样" in w for w in out["warnings"])
    validated = WavePropagation(layout=layout)
    anchors = host_positions(validated.layout.system)
    assert _travel(validated.layout, anchors, "primary", "before", 1, "fixed_z") == (1, 1)
    result = analyze_layout(validated.layout)
    before, after = result["sections"]["planes"]
    assert before["beams"][0]["radii_mm"] and not after["beams"][0]["radii_mm"]
    assert result["intensity_model"] == "unclipped_reference"
    assert "exit_field" not in _propagate(PropagationStudySystem(**layout["system"]))
    assert "clear_aperture" not in OpticalElement(**gap("legacy", 2)).model_dump()
    layout["system"]["elements"].append(dict(name="blackbox", kind="matrix", matrix_t=[1, 0, 0, 1]))
    assert field(layout, plane_uid="before")["status"] == "completed"
    with pytest.raises(ValueError, match="blackbox"):
        field(layout)
    layout["system"].update(mode="ring")
    with pytest.raises(ValueError, match="本征模"):
        field(layout)

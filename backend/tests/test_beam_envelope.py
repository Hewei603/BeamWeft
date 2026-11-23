# -*- coding: utf-8 -*-
"""The layout drawing is only as trustworthy as the envelope it is drawn from.

These pin the two conventions that are easy to get silently wrong: the local
refractive index (forgetting it inflates w by sqrt(n) with no error raised) and
the difference between the reduced and the physical waist position.
"""
import math

import pytest

from app.physics import beam as B
from app.physics.toolkit import _FLAT_ROC_MM, _analyze_length, _cavity_elements, beam_envelope, run_cavity_design

CRYSTAL = {"n": 1.8147, "thickness_mm": 20, "position_mm": 40}


def test_empty_cavity_waist_matches_the_analytic_solution():
    """A symmetric empty cavity has its waist at the exact centre."""
    env = beam_envelope(500.0, 1000.0, 1000.0, 1064.0, None)
    analytic = B.two_mirror_cavity(1000.0, 1000.0, 500.0, 1064.0)

    assert env["waist"]["z_mm"] == 250.0
    assert abs(env["waist"]["w0_mm"] - analytic["waist_w0_mm"]) < 1e-6


def test_samples_agree_with_trace_beam_at_element_planes():
    """The drawing and the analysis must not be able to disagree.

    Both walk the same element list, so equality here is what stops the picture
    drifting away from the numbers printed next to it.
    """
    length = 200.0
    env = beam_envelope(length, 1000.0, 1000.0, 1064.0, CRYSTAL)
    round_trip, fwd = _cavity_elements(length, 1000.0, 1000.0, CRYSTAL)
    trace = B.trace_beam(fwd, B.cavity_mode_q(B.system_matrix(round_trip)), 1064.0)

    def sampled_at(z_mm):
        return min(env["points"], key=lambda p: abs(p["z_mm"] - z_mm))["w_mm"]

    crystal_centre = next(t for t in trace if t["name"] == "crystal center")
    assert abs(sampled_at(length) - trace[-1]["w_mm"]) < 1e-5
    assert abs(sampled_at(50.0) - crystal_centre["w_mm"]) < 1e-5


def test_beam_radius_is_continuous_across_the_crystal_faces():
    """Guards the easiest silent bug: sampling the crystal with n = 1.

    w is continuous at a flat interface — only the divergence changes. Dropping
    the local index inflates w inside the crystal by sqrt(n) (~35% here) and
    raises nothing, so the drawing would simply be wrong.

    Asserted as smoothness over the whole envelope rather than by comparing the
    two samples nearest a face: the step size can place a sample exactly on the
    face, in which case "the point before" and "the point after" are the same
    point and such a check passes no matter what the code does.
    """
    env = beam_envelope(200.0, 1000.0, 1000.0, 1064.0, CRYSTAL)
    widths = [p["w_mm"] for p in env["points"]]
    mean_w = sum(widths) / len(widths)
    biggest_jump = max(abs(b - a) for a, b in zip(widths, widths[1:]))

    # A correct envelope changes by ~0.1% between neighbours; a lost index would
    # put a single ~35% discontinuity at each face.
    assert biggest_jump < 0.05 * mean_w, (
        f"discontinuity of {biggest_jump:.4f} mm against a mean radius of {mean_w:.4f} mm "
        "— the local refractive index is probably not being applied inside the crystal"
    )
    # And the samples must actually record which medium they are in.
    assert {p["n"] for p in env["points"]} == {1.0, CRYSTAL["n"]}


def test_physical_waist_position_differs_from_the_reduced_one_by_the_slab_shift():
    """`waist_from_mirror1_mm` is an optical-path length when a crystal is present.

    Using it as a drawing coordinate would misplace the waist by t(1 - 1/n) —
    here about 9 mm on a 200 mm cavity. The envelope solves Re(q) = 0 per
    segment instead, which is exact and in physical millimetres.
    """
    length = 200.0
    env = beam_envelope(length, 1000.0, 1000.0, 1064.0, CRYSTAL)
    reduced = _analyze_length(length, 1000.0, 1000.0, 1064.0, CRYSTAL)["waist_from_mirror1_mm"]

    expected_shift = CRYSTAL["thickness_mm"] * (1.0 - 1.0 / CRYSTAL["n"])
    assert abs((env["waist"]["z_mm"] - reduced) - expected_shift) < 0.01


def test_unstable_cavity_yields_no_envelope_rather_than_a_misleading_one():
    env = beam_envelope(2500.0, 1000.0, 1000.0, 1064.0, None)
    assert env["points"] == []
    assert env["waist"] is None
    assert env["waist_location"] == "no_confined_mode"
    assert "不稳定" in env["note"]


def test_envelope_spans_the_whole_cavity_and_declares_the_cold_cavity_caveat():
    length = 200.0
    env = beam_envelope(length, 1000.0, 1000.0, 1064.0, CRYSTAL)

    assert env["points"][0]["z_mm"] == 0.0
    assert abs(env["points"][-1]["z_mm"] - length) < 1e-6
    # The recommendation is ranked by thermal-lens margin; the picture is not.
    assert "冷腔" in env["note"]


@pytest.mark.parametrize("flat_at_m2", [False, True])
@pytest.mark.parametrize("thickness", [0, 20])
def test_plane_mirror_waist_in_air_or_touching_a_dielectric(flat_at_m2, thickness):
    length, curved_roc, n, wavelength = 50, 100, 1.8, 1064
    # With a flat-ended cavity the wavefront is plane at that mirror and
    # z_R^2 = L_eff (R-L_eff) in air. A parallel plate replaces t by t/n.
    mirror_z = length if flat_at_m2 else 0
    slab = {"n": n, "thickness_mm": thickness, "position_mm": length-thickness if flat_at_m2 else 0} if thickness else None
    effective = length - thickness + thickness/n
    expected_w = math.sqrt(wavelength * 1e-6 * math.sqrt(effective*(curved_roc-effective)) / math.pi)
    r1, r2 = (curved_roc, _FLAT_ROC_MM) if flat_at_m2 else (_FLAT_ROC_MM, curved_roc)
    record = run_cavity_design({"L_mm": length, "R1_mm": r1, "R2_mm": r2, "crystal": slab, "wavelength_nm": wavelength})
    for envelope in (record["beam_envelope"], beam_envelope(length, r1, r2, wavelength, slab, samples=8)):
        assert envelope["waist_location"] == "inside"
        assert envelope["waist"]["z_mm"] == mirror_z
        assert envelope["waist"]["in_medium_n"] == (n if slab else 1)
        assert envelope["waist"]["w0_mm"] == pytest.approx(expected_w, abs=5e-7)
        assert envelope["points"][-1 if flat_at_m2 else 0]["w_mm"] == envelope["waist"]["w0_mm"]


@pytest.mark.parametrize(("entrance","waist_z"), [(45, 45), (35, 55)])
def test_waist_on_dielectric_face_is_independent_of_sampling(entrance, waist_z):
    slab = {"n": 2, "position_mm": entrance, "thickness_mm": 20}
    for samples in (8, 180, 331):
        e = beam_envelope(100, 300, 300, 1064, slab, samples)
        assert e["waist_location"] == "inside"
        assert e["waist"]["z_mm"] == waist_z
        assert any(p["z_mm"] == waist_z and p["w_mm"] == e["waist"]["w0_mm"] for p in e["points"])


@pytest.mark.parametrize(("r1", "r2"), [(-100, 100), (100, -100), (-1e16, 100), (100, -1e16)])
def test_virtual_waist_stays_outside_even_when_mirror_is_almost_plane(r1, r2):
    result = run_cavity_design({"L_mm": 50, "R1_mm": r1, "R2_mm": r2, "wavelength_nm": 1064})
    assert result["analysis"]["stable"] and result["analysis"]["waist_w0_mm"] > 0
    e = result["beam_envelope"]
    assert e["waist_location"] == "outside" and e["waist"] is None and e["points"]
    if -100 in (r1, r2):
        assert min(p["w_mm"] for p in e["points"]) == pytest.approx(.139835, abs=1e-6)
        assert result["analysis"]["waist_w0_mm"] == pytest.approx(.12110, abs=1e-5)

"""Thin-film TMM solver: validated against analytic thin-film results."""
import math

import numpy as np

from app.physics.materials import n_real
from app.physics.tmm import Layer, Stack, solve_RT, solve_stack, spectrum


def test_bare_interface_matches_fresnel():
    ns = n_real("fused_silica", 1064.0)
    r_fresnel = ((1 - ns) / (1 + ns)) ** 2
    res = solve_stack(Stack(layers=[], incident="air", substrate="fused_silica"), 1064.0, 0.0, "s")
    assert abs(res["R"] - r_fresnel) < 1e-9
    assert abs(res["R"] + res["T"] - 1.0) < 1e-9  # lossless


def test_at_cutoff_layer_and_substrate_remain_finite_in_both_polarizations():
    angle, incident = 45.0, 1.5
    cutoff_index = incident * np.sin(np.deg2rad(angle))
    with np.errstate(divide="raise", invalid="raise"):
        for pol in ("s","p"):
            r, t, a = solve_RT([cutoff_index], [80], 633, angle, pol, incident, cutoff_index)
            np.testing.assert_allclose((r,t,a), (1,0,0), atol=1e-12)
            # A finite evanescent layer can transmit into a higher-index exit.
            actual = solve_RT([cutoff_index], [80], 633, angle, pol, incident, incident)
            nearby = solve_RT([cutoff_index], [80], 633, angle + 1e-7, pol, incident, incident)
            assert 0 < actual[1] < 1
            np.testing.assert_allclose(actual, nearby, atol=1e-8)


def test_opaque_film_has_finite_front_surface_reflectance_and_long_bragg_stack_conserves_power():
    absorbing = 1.1 - 3j
    expected_r = abs((1 - absorbing) / (1 + absorbing)) ** 2
    with np.errstate(over="raise",invalid="raise"):
        for thickness in (1000, 30000, 100000):
            r, t, a = solve_RT([absorbing], [thickness], 633, 0, "s", 1, 1.5)
            np.testing.assert_allclose((r,t,a), (expected_r,0,1-expected_r), atol=1e-12)
        indices = np.tile([2.4,1.45], 1600)
        r,t,a = solve_RT(indices, 633/(4*indices), 633, 0, "p", 1, 1.5)
        np.testing.assert_allclose((r,t,a), (1,0,0), atol=1e-12)


def test_ideal_quarterwave_ar_gives_zero_reflection():
    ns = n_real("fused_silica", 1064.0)
    n_ar = math.sqrt(ns)
    d = 1064.0 / (4 * n_ar)
    st = Stack(layers=[Layer(thickness_nm=d, n=n_ar)], incident="air", substrate="fused_silica")
    res = solve_stack(st, 1064.0, 0.0, "s")
    assert res["R"] < 1e-12


def test_hr_quarterwave_stack_matches_closed_form():
    nH, nL = 2.09, 1.45
    ns = n_real("fused_silica", 1064.0)
    n0 = 1.0
    N = 8
    dH, dL = 1064.0 / (4 * nH), 1064.0 / (4 * nL)
    layers = []
    for _ in range(N):
        layers += [Layer(thickness_nm=dH, n=nH), Layer(thickness_nm=dL, n=nL)]
    st = Stack(layers=layers, incident="air", substrate="fused_silica")
    res = solve_stack(st, 1064.0, 0.0, "s")
    # Equivalent-admittance closed form for (HL)^N with H incident-side, L on substrate.
    Y = (nH / nL) ** (2 * N) * ns
    R_cf = ((n0 - Y) / (n0 + Y)) ** 2
    assert abs(res["R"] - R_cf) < 1e-6
    assert res["R"] > 0.99


def test_s_equals_p_at_normal_incidence():
    st = Stack(layers=[Layer(thickness_nm=200.0, n=2.0)], incident="air", substrate="nbk7")
    res = solve_stack(st, 1064.0, 0.0, "avg")
    assert abs(res["R_s"] - res["R_p"]) < 1e-12


def test_energy_conservation_lossless():
    st = Stack(layers=[Layer(thickness_nm=133.0, n=2.09), Layer(thickness_nm=183.0, n=1.45)],
               incident="air", substrate="fused_silica")
    for aoi in (0.0, 15.0, 45.0):
        for pol in ("s", "p"):
            res = solve_stack(st, 1064.0, aoi, pol)
            assert abs(res["R"] + res["T"] + res["A"] - 1.0) < 1e-9
            assert abs(res["A"]) < 1e-9  # lossless layers -> no absorption


def test_lossy_layer_absorbs():
    # a layer with k>0 must produce A>0 and still R+T+A=1
    st = Stack(layers=[Layer(thickness_nm=500.0, n=complex(2.0, -0.05))], incident="air", substrate="fused_silica")
    res = solve_stack(st, 1064.0, 0.0, "s")
    assert res["A"] > 0.0
    assert abs(res["R"] + res["T"] + res["A"] - 1.0) < 1e-9


def test_spectrum_shape():
    st = Stack(layers=[Layer(thickness_nm=133.0, n=2.09)], incident="air", substrate="fused_silica")
    grid = np.linspace(900, 1200, 50)
    out = spectrum(st, grid, 0.0, "s")
    assert out["R"].shape == grid.shape
    assert np.all(out["R"] >= -1e-12) and np.all(out["R"] <= 1.0 + 1e-9)

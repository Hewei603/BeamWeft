import math

import numpy as np
import pytest
from scipy.integrate import quad
from scipy.special import erf

from app.physics.ray_overlap import weighted_ray_area


def flat_signal(w=0.3):
    # A Rayleigh range much longer than all sections used in the constant-width checks.
    return [(w, 0., 1e24, 0., 0.)] * 2


def test_same_second_moments_do_not_imply_the_same_absorbed_modal_overlap():
    w = .27
    a = [[-w, 0, 0, 0, .11], [w, 0, 0, 0, .11]]
    r = math.sqrt(2) * w
    b = [[-r, 0, 0, 0, .07], [0, 0, 0, 0, .14], [r, 0, 0, 0, .07]]
    for rays in (a, b):
        assert np.average([row[0] ** 2 for row in rays], weights=[row[4] for row in rays]) == pytest.approx(w*w)
    peak = 2 / (math.pi * w*w)
    first, second = peak*math.exp(-2), peak*(1+math.exp(-4))/2
    result = weighted_ray_area(15, 7, flat_signal(w), [*a,*b], 1.8, len(a))
    assert result["replicate_density_difference_per_mm2"] == pytest.approx(second-first)
    assert result["replicate_entrance_weights"] == pytest.approx([.22,.28])
    assert result["area_mm2"] == pytest.approx(.5 / (.22*first + .28*second))
    assert result["positive_ray_count"] == 5 and result["entrance_weight"] == pytest.approx(.5)


def test_astigmatic_signal_and_reduced_ray_slopes_against_independent_quad():
    length, absorption, n = 15, .15, 1.64
    axes = [(.061, -5., 1.9, .04, -.002), (.13, -12., 3.4, -.025, .004)]
    rays = [[-.12, .07, .022, -.009, .22], [.06, -.042, -.004, .012, .05]]
    def product(z):
        s = 0.
        for x, y, pt, ps, weight in rays:
            f = 1.
            for (w, re, im, c, slope), origin, momentum in zip(axes, (x,y), (pt,ps)):
                width = w * abs(complex(re + z, im) / complex(re, im))
                displacement = origin + momentum*z/n - (c+slope*z)
                f *= math.sqrt(2/math.pi)/width * math.exp(-2*(displacement/width)**2)
            s += weight * f
        return s*math.exp(-absorption*z)
    expectation = quad(product, 0, length, epsabs=1e-13, epsrel=1e-13)[0]
    expectation *= absorption / (-math.expm1(-absorption*length) * .27)
    result = weighted_ray_area(length, absorption*length, axes, rays, n)
    assert result["mean_signal_density_per_mm2"] == pytest.approx(expectation, rel=1e-9)
    assert result["relative_quadrature_error"] <= 1e-8


def test_sharp_interior_crossing_is_bracketed_at_its_own_scale():
    length, crossing, slope, w, alpha, n = 500., 234.56781, .047, 1.7e-6, .007, 1.5
    rays = [[-crossing*slope, 0, slope*n, 0, 1]]
    a = math.sqrt(2)*slope/w
    shift = alpha/(2*a)
    integral = math.exp(-alpha*crossing+shift**2) * math.sqrt(math.pi)/(2*a) * (
        erf(a*(length-crossing)+shift)-erf(-a*crossing+shift))
    expected = 2/(math.pi*w*w) * alpha / -math.expm1(-alpha*length) * integral
    found = weighted_ray_area(length, alpha*length, flat_signal(w), rays, n)
    assert found["mean_signal_density_per_mm2"] == pytest.approx(expected, rel=1e-8)


@pytest.mark.parametrize("depth", [1e-305, .1, 1e7, 1e305])
def test_constant_density_invariant_to_axial_absorption_and_surviving_mass(depth):
    result = weighted_ray_area(10., depth, flat_signal(), [[0,0,0,0,0], [0,0,0,0,.03], [1e10,0,0,0,.09]], 1, 1)
    assert result["area_mm2"] == pytest.approx(4*math.pi*.3**2/2, rel=1e-12)
    assert result["replicate_entrance_weights"] == pytest.approx([0,.12])
    assert "replicate_density_difference_per_mm2" not in result


def test_zero_mass_unrepresentable_area_or_unresolved_crossing_do_not_yield_a_finite_area():
    with pytest.raises(ValueError, match="正权重"):
        weighted_ray_area(1, 1, flat_signal(), [[0,0,0,0,0]], 1)
    with pytest.raises(ValueError, match="数值范围|分辨"):
        weighted_ray_area(1, 1, flat_signal(), [[1e200,0,0,0,1]], 1)
    with pytest.raises(ValueError, match="数值范围|分辨"):
        weighted_ray_area(1, 1, flat_signal(1e-24), [[-.011234567,0,.02,0,1]], 1)

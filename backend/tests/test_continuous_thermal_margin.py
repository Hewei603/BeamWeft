import math

import pytest
from scipy.optimize import brentq

from app.physics.design import _round_trip_with_lens, thermal_lens_margin, thermal_lens_tolerance
from app.physics.toolkit import _FLAT_ROC_MM, run_cavity_design


def half_trace(geometry, power):
    matrix = _round_trip_with_lens(*geometry, 1000 / power if power else None)
    return matrix.trace() / 2


@pytest.mark.parametrize("geometry,bracket,sign", [
    ((190, 250, 250.5, None), (4.07, 4.08), -1),
    ((185, 210, 275, {"n": 1.8, "thickness_mm": 12, "position_mm": 25}), (0.60, 0.62), -1),
    ((310, 200, 200, None), (12, 14), 1),
    ((200, 100, 150, None), (19, 21), 1),  # Trace loses its quadratic term.
])
def test_boundary_matches_independent_matrix_product(geometry, bracket, sign):
    expected = brentq(lambda power: half_trace(geometry, power) - sign, *bracket)
    limits = thermal_lens_margin(*geometry, ceiling_dioptre=100)
    assert limits["thermal_lens_limit_kind"] == "critical"
    assert limits["thermal_lens_critical_m"] == sign
    assert limits["thermal_lens_tolerance_dioptre"] == pytest.approx(expected, abs=2e-11)
    assert limits["thermal_lens_tolerance_min_f_mm"] == pytest.approx(1000 / expected)
    assert thermal_lens_tolerance(*geometry, ceiling_dioptre=100, step_dioptre=20) == pytest.approx(expected)


def test_reentrant_gap_and_tangent_break_the_cold_connected_stable_interval():
    geometry = (190, 250, 250.5, None)
    # A 0.1 D grid reports both stations stable and skips the intervening gap.
    assert all(abs(half_trace(geometry, power)) < 1 for power in (4.0, 4.1, 10))
    assert half_trace(geometry, 4.08) < -1
    # Symmetric 300 mm mirrors touch m=-1 exactly, then immediately become stable again.
    geometry = (200, 300, 300, None)
    assert half_trace(geometry, 5) == -1
    assert all(abs(half_trace(geometry, power)) < 1 for power in (4.99, 5.01))
    assert thermal_lens_margin(*geometry, ceiling_dioptre=5)["thermal_lens_limit_kind"] == "critical"
    assert thermal_lens_margin(*geometry, ceiling_dioptre=math.nextafter(5, 0))["thermal_lens_limit_kind"] == "ceiling"
    assert thermal_lens_tolerance(*geometry, step_dioptre=1.3) == 5
    # Distinct radii at the next representable value cannot erase the earlier root.
    assert thermal_lens_tolerance(200, 300, math.nextafter(300, math.inf), None) == 5


@pytest.mark.parametrize("length,r1,r2,kind", [
    (200, 200, 200, "cold_marginal"), (150, 100, 300, "cold_unstable"),
    (50, _FLAT_ROC_MM, _FLAT_ROC_MM, "cold_marginal"),
])
def test_no_positive_margin_from_a_marginal_or_unstable_cold_cavity(length, r1, r2, kind):
    margin = thermal_lens_margin(length, r1, r2, None)
    assert margin["thermal_lens_limit_kind"] == kind
    assert margin["thermal_lens_tolerance_dioptre"] == 0
    assert margin["thermal_lens_tolerance_min_f_mm"] is None


def test_new_fixed_geometry_and_scan_both_record_what_the_limit_means():
    config = {"wavelength_nm": 1064, "R1_mm": 250, "R2_mm": 250.5}
    fixed = run_cavity_design({**config, "L_mm": 190})["analysis"]
    scan = run_cavity_design({**config, "L_scan_mm": {"start": 190, "stop": 191, "step": 1}})["recommended"]
    assert scan["length_mm"] == 190
    assert scan["thermal_lens_tolerance_dioptre"] == fixed["thermal_lens_tolerance_dioptre"]
    assert fixed["thermal_lens_limit_kind"] == scan["thermal_lens_limit_kind"] == "critical"
    assert run_cavity_design({**config, "L_mm": 30})["analysis"]["thermal_lens_limit_kind"] == "ceiling"

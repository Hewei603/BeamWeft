from fractions import Fraction

import pytest
from pydantic import ValidationError

from app.workbench.adjustment_policy import AdjustmentPolicy, inspect_adjustment_policy
from .test_study_tolerance import archive, axis


def request(record, roles, weights, **metadata):
    distribution = {"basis": "observed", "source": "Bench records", **metadata,
                    "scenarios": [{"coordinates": coords, "weight": w} for coords, w in weights]}
    return AdjustmentPolicy(search_id=record.id, roles=roles, distribution=distribution)


def test_same_finite_distribution_compares_designs_without_using_row_counts(db):
    # Two receivers each have a detector at one of the possible focal planes.
    axes = [axis("detector_mm", [90, 110]), axis("focus_mm", [90, 90, 110])]
    case, source, _ = archive(db, axes, [{"output": "defocus_mm", "upper": 1}],
                              lambda detector, focus: ({"defocus_mm": abs(detector - focus)}, {}))
    roles = ["design", "hidden"]
    report = inspect_adjustment_policy(db.get_bind(), case, request(source, roles, [([110], 1), ([90], 9)]))
    assert report["distribution"]["paths"] == [["focus_mm"]]
    assert [s["fraction"] for s in report["distribution"]["scenarios"]] == [.1, .9]
    assert [c["scenario_index"] for c in report["cells"]] == [1, 0, 1, 0]
    for design, expected in zip(report["designs"], [.9, .1]):
        assert design["fixed"]["state"] == "outside"
        assert design["fixed"]["mass"] == design["adaptive"]["mass"] == {"lower": expected, "upper": expected}
    ordinary = inspect_adjustment_policy(db.get_bind(), case, AdjustmentPolicy(search_id=source.id, roles=roles))
    assert "distribution" not in ordinary
    assert all("mass" not in d["fixed"] for d in ordinary["designs"])


def test_joint_mass_preserves_correlation_and_enforces_what_can_be_observed(db):
    # A gauge indicates the focus with imperfect accuracy. Set the detector using only the gauge.
    axes = [axis("gauge_mm", [90, 110]), axis("actual_focus_mm", [90, 110]), axis("detector_mm", [90, 110])]
    case, source, _ = archive(db, axes, [{"output": "defocus_mm", "upper": 1}],
                              lambda gauge, focus, detector: ({"defocus_mm": abs(detector - focus)}, {}))
    weights = [([90, 90], 45), ([90, 110], 5), ([110, 90], 10), ([110, 110], 40)]
    def policy(roles):
        return inspect_adjustment_policy(db.get_bind(), case, request(source, roles, weights))["designs"][0]
    use_gauge = policy(["observed", "hidden", "control"])
    assert use_gauge["fixed"]["mass"] == {"lower": .55, "upper": .55}
    assert use_gauge["adaptive"]["mass"] == {"lower": .85, "upper": .85}
    groups = use_gauge["adaptive"]["groups"]
    assert [(g["fraction"], g["mass"]["lower"], g["lower_option_indices"]) for g in groups] == [
        (.5, .45, [0]), (.5, .4, [1])]
    without_gauge = policy(["hidden", "hidden", "control"])
    assert without_gauge["adaptive"]["mass"] == without_gauge["fixed"]["mass"] == {"lower": .55, "upper": .55}


def test_unknown_mass_changes_bounds_and_can_change_which_control_attains_them(db):
    def read(position, control):
        if (position, control) == (2, 1):
            return {}, {"signal": "detector offline"}
        return {"signal": int(position == control)}, {}
    axes = [axis("position", [0, 1, 2]), axis("control", [0, 1])]
    case, source, _ = archive(db, axes, [{"output": "signal", "lower": 1}], read)
    report = inspect_adjustment_policy(db.get_bind(), case, request(
        source, ["hidden", "control"], [([0], 4), ([1], 3), ([2], 3)], basis="assumed"))
    fixed = report["designs"][0]["fixed"]
    assert fixed["mass"] == {"lower": .4, "upper": .6}
    assert (fixed["lower_option_indices"], fixed["upper_option_indices"]) == ([0], [1])
    assert fixed["options"][1]["mass"] == {"pass": .3, "outside": .4, "unknown": .3, "lower": .3, "upper": .6}


def test_explicit_zero_outside_does_not_remove_the_counterexample_or_imply_positive_mass(db):
    case, source, _ = archive(db, [axis("condition", [0, 1])], [{"output": "signal", "lower": 1}],
                              lambda condition: ({"signal": 1 - condition}, {}))
    report = inspect_adjustment_policy(db.get_bind(), case, request(source, ["observed"], [([0], 3.4), ([1], 0)]))
    fixed = report["designs"][0]["fixed"]
    assert fixed["state"] == "outside" and fixed["mass"] == {"lower": 1, "upper": 1}
    excluded = report["designs"][0]["adaptive"]["groups"][1]
    assert excluded["state"] == "outside" and excluded["fraction"] == excluded["mass"]["upper"] == 0
    assert report["cells"][1]["rows"][0]["run_id"] is not None


def test_extreme_weights_and_tiny_nonzero_differences_are_preserved_in_control_selection(db):
    case, source, _ = archive(db, [axis("weather", [0, 1]), axis("control", [0, 1])],
                              [{"output": "valid", "lower": 1}],
                              lambda weather, control: ({"valid": int(weather == 0 or control == 1)}, {}))
    def compare(small):
        return inspect_adjustment_policy(db.get_bind(), case, request(
            source, ["hidden", "control"], [([0], 1e308), ([1], small)]))
    assert [s["fraction"] for s in compare(1e308)["distribution"]["scenarios"]] == [.5, .5]
    result = compare(1e-300)
    fixed = result["designs"][0]["fixed"]
    assert fixed["lower_option_indices"] == fixed["upper_option_indices"] == [1]
    first, second = result["distribution"]["scenarios"]
    assert 0 < Fraction(second["exact"]["fraction"]) < Fraction(first["exact"]["fraction"]) < 1
    assert Fraction(fixed["options"][0]["mass"]["exact"]["outside"]) == Fraction(second["exact"]["fraction"])


def test_distribution_requires_sourced_complete_joint_support_and_finite_nonnegative_weights(db):
    case, source, _ = archive(db, [axis("x", [0, 1])], [{"output": "value", "lower": 0}],
                              lambda x: ({"value": x}, {}))
    def run(weights, **metadata):
        return inspect_adjustment_policy(db.get_bind(), case, request(source, ["hidden"], weights, **metadata))
    for weights in ([([0], 1)], [([0], 2), ([3], 0)], [([0, 1], 1)], [([0], 1), ([0], 2)]):
        with pytest.raises(ValueError):
            run(weights)
    for bad in (-1, True, "2", float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            run([([0], 1), ([1], bad)])
    for metadata in ({"source": "  "}, {"basis": "unknown"}):
        with pytest.raises(ValidationError):
            run([([0], 1), ([1], 0)], **metadata)
    with pytest.raises(ValidationError):
        run([([0], 0), ([1], 0)])
    # Without any random condition there is exactly one empty joint coordinate.
    fixed = inspect_adjustment_policy(db.get_bind(), case, request(source, ["design"], [([], 2)]))
    assert fixed["distribution"]["paths"] == [] and fixed["distribution"]["scenarios"][0]["fraction"] == 1

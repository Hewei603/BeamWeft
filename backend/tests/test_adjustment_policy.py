"""A read-only qualification of a sampled study across possible adjustment policies."""
import math
from itertools import product

import pytest

from app.models import GeneratedContent
from app.workbench.adjustment_policy import AdjustmentPolicy, inspect_adjustment_policy
from app.workbench.study_tolerance import StudyTolerance, inspect_tolerance
from .test_study_tolerance import archive, axis


def inspect(db, case, source, *roles):
    request = AdjustmentPolicy.model_validate({"search_id": source.id, "roles": list(roles)})
    return inspect_adjustment_policy(db.get_bind(), case, request)


def states(result):
    design, = result["designs"]
    return design["fixed"]["state"], design["adaptive"]["state"]


def coordinates_for(result, option):
    return {tuple(result["cells"][i]["coordinates"]) for i in option["cell_indices"]}


def test_thin_lens_refocus_requires_an_observable_condition_and_preserves_evidence(db):
    # Independent paraxial oracle: a Gaussian waist of 0.5 mm falls on the lens.
    # w(d)^2 = w0^2 * ((1-d/f)^2 + (d/z_R)^2); all lengths in mm.
    waist = .5
    rayleigh = math.pi * waist**2 / .001

    def simulate(focal_length, work_distance):
        spot = waist * math.hypot(1 - work_distance / focal_length, work_distance / rayleigh)
        return {"radius": spot}, {"cost": "catalog price unavailable"}

    axes = [axis("focal_length", [90, 110]), axis("work_distance", [90, 110])]
    limits = [{"output": "radius", "upper": .080}, {"output": "cost", "direction": "min"}]
    case, source, frozen = archive(db, axes, limits, simulate)
    before = db.query(GeneratedContent).count()
    report = inspect(db, case, source, "observed", "control")
    assert states(report) == ("outside", "pass")
    design, = report["designs"]
    for option in design["fixed"]["options"]:
        points = coordinates_for(report, option)
        assert len(points) == 2 and len({point[1] for point in points}) == 1
        assert option["counts"] == {"pass": 1, "outside": 1, "unknown": 0}
    accepted = [option for group in design["adaptive"]["groups"] for option in group["options"]
                if option["state"] == "pass"]
    assert {point for option in accepted for point in coordinates_for(report, option)} == {(90, 90), (110, 110)}
    assert all(group["state"] == "pass" for group in design["adaptive"]["groups"])
    for cell in report["cells"]:
        witness, = cell["rows"]
        row = frozen["rows"][witness["row_index"]]
        assert row["coordinates"] == cell["coordinates"] and row["run_id"] == witness["run_id"]
        assert witness["state"] == cell["state"] and witness["reason"]
        if cell["state"] == "outside":
            assert cell["issues"][0]["output"] == "radius"
            assert cell["issues"][0]["value"] == pytest.approx(row["values"]["radius"])
    assert states(inspect(db, case, source, "hidden", "control")) == ("outside", "outside")
    assert db.query(GeneratedContent).count() == before and source.content == frozen


@pytest.mark.parametrize(("missing_point", "expected"), [
    ((1, 1), ("outside", "unknown")),
    ((1, 2), ("unknown", "pass")),
])
def test_missing_condition_is_unknown_and_a_complete_witness_can_still_prove_existence(db, missing_point, expected):
    axes = [axis("temperature", [1, 2]), axis("bias", [1, 2])]
    case, source, data = archive(db, axes, [{"output": "error", "upper": 0}],
                                 lambda temp, bias: ({"error": abs(temp - bias)}, {}))
    source.content = {**data, "rows": [r for r in data["rows"] if tuple(r["coordinates"]) != missing_point]}
    db.commit()
    report = inspect(db, case, source, "observed", "control")
    assert states(report) == expected
    missing, = [c for c in report["cells"] if tuple(c["coordinates"]) == missing_point]
    assert missing["state"] == "unknown" and missing["reason"]
    assert all(r["row_index"] is None and r["run_id"] is None for r in missing["rows"])


def test_designs_stay_separate_and_hidden_variation_cannot_select_a_control(db):
    # The paired setting has constant translation and a distinct tilt in each sample.
    coupled = {"paths": [["translation"], ["tilt"]], "samples": [[4, 0], [4, 1]]}
    axes = [axis("design", [0, 1]), axis("measured", [0, 1]), axis("unmeasured", [0, 1]), coupled]

    def readout(design, measured, unmeasured, _, tilt):
        target = (measured + unmeasured) % 2 if design == 1 else measured
        return {"error": abs(target - tilt)}, {}

    case, source, _ = archive(db, axes, [{"output": "error", "upper": 0}], readout)
    report = inspect(db, case, source, "design", "observed", "hidden", "control")
    assert len(report["cells"]) == 16
    decisions = {}
    for design in report["designs"]:
        groups = design["adaptive"]["groups"]
        assert len(groups) == 2 and len(design["fixed"]["options"]) == 2
        assert len({tuple(option["control"]) for option in design["fixed"]["options"]}) == 2
        for group in groups:
            for option in group["options"]:
                points = coordinates_for(report, option)
                assert len({x[1] for x in points}) == 1 and {x[2] for x in points} == {0, 1}
                assert len({x[3:] for x in points}) == 1
        key, = design["design"]
        decisions[key] = (design["fixed"]["state"], design["adaptive"]["state"])
    assert decisions == {0: ("outside", "pass"), 1: ("outside", "outside")}


def test_expected_replicates_are_jointly_required_and_excess_replicates_cannot_qualify(db):
    axes = [axis("workpoint", [5, 5]), axis("control", [1, 2])]
    case, source, data = archive(db, axes, [{"output": "error", "upper": 0}], lambda *_: ({"error": 0}, {}))
    rows = [dict(r) for r in data["rows"]]
    rows[2] = {**rows[2], "values": {"error": 1}}
    # Both expected observations exist at control 1; control 2 has an extra observation.
    unexpected_repeat = {**rows[1], "values": {"error": 100}}
    source.content = {**data, "rows": [*rows, unexpected_repeat]}
    db.commit()
    report = inspect(db, case, source, "hidden", "control")
    cells = {tuple(c["coordinates"]): c for c in report["cells"]}
    assert cells[(5, 1)]["state"] == "outside"
    assert {r["state"] for r in cells[(5, 1)]["rows"]} == {"pass", "outside"}
    assert cells[(5, 2)]["state"] == "unknown" and cells[(5, 2)]["reason"]
    assert states(report) == ("unknown", "unknown")
    request = StudyTolerance(search_id=source.id, row_index=0, axis_index=1, scope="all")
    margin = inspect_tolerance(db.get_bind(), case, request)
    assert {s["position"]: s["state"] for s in margin["stations"]} == {
        1: cells[(5, 1)]["state"], 2: cells[(5, 2)]["state"]}

    source.content = {**data, "rows": [rows[0], rows[1], rows[3]]}
    db.commit()
    next_report = inspect(db, case, source, "hidden", "control")
    assert {tuple(c["coordinates"]): c["state"] for c in next_report["cells"]} == {
        (5, 1): "unknown", (5, 2): "pass"}
    assert states(next_report) == ("pass", "pass")
    next_margin = inspect_tolerance(db.get_bind(), case, request)
    assert {s["position"]: s["state"] for s in next_margin["stations"]} == {1: "unknown", 2: "pass"}


def test_old_archive_keeps_its_reading_and_identifies_missing_method_fingerprint(db):
    case, source, data = archive(db, [axis("position", [0])], [{"output": "spot", "upper": .1}],
                                 lambda _: ({"spot": .08}, {}))
    row = dict(data["rows"][0])
    row.pop("solver_sha256")
    old_run = db.get(GeneratedContent, row["run_id"])
    old_run.content = {}
    source.content = {**data, "rows": [row]}
    db.commit()
    report = inspect(db, case, source, "observed")
    cell, = report["cells"]
    witness, = cell["rows"]
    assert cell["state"] == witness["state"] == "pass"
    assert witness["run_id"] == row["run_id"] and witness["solver_identity"] == "unavailable"


def test_wrong_project_and_invalid_solver_receipts_do_not_supply_evidence(db):
    axes = [axis("position", [1, 2, 3]), axis("trim", [0])]
    limits = [{"output": "score", "lower": 0}]
    case, source, data = archive(db, axes, limits, lambda *_: ({"score": 1}, {}))
    foreign_case, _, foreign = archive(db, axes, limits, lambda *_: ({"score": 1}, {}))
    with pytest.raises(ValueError):
        inspect(db, foreign_case, source, "observed", "control")
    rows = [dict(r) for r in data["rows"]]
    rows[1]["solver_sha256"] = "changed"
    rows[2]["run_id"] = foreign["rows"][2]["run_id"]
    source.content = {**data, "rows": rows}
    db.commit()
    report = inspect(db, case, source, "observed", "control")
    cells = {tuple(c["coordinates"]): c for c in report["cells"]}
    assert cells[(1, 0)]["state"] == "pass"
    assert cells[(2, 0)]["state"] == cells[(3, 0)]["state"] == "unknown"
    assert cells[(3, 0)]["rows"][0]["run_id"] is None
    assert states(report) == ("unknown", "unknown")

    source.content = {**data, "rows": [*rows, {**rows[0], "coordinates": [100, 0]}]}
    db.commit()
    with pytest.raises(ValueError):
        inspect(db, case, source, "observed", "control")


def test_unconstrained_objectives_cannot_qualify_and_roles_cover_whole_groups(db):
    axes = [axis("design", [1, 2]), axis("setting", [10, 20])]
    case, source, _ = archive(db, axes, [{"output": "cost", "direction": "min"}], lambda x, y: ({"cost": x * y}, {}))
    with pytest.raises(ValueError):
        inspect(db, case, source, "design", "control")
    case, source, _ = archive(db, axes, [{"output": "cost", "upper": 100}], lambda x, y: ({"cost": x * y}, {}))
    with pytest.raises(ValueError):
        inspect(db, case, source, "design")
    report = inspect(db, case, source, "observed", "hidden")
    assert states(report) == ("pass", "pass")
    design, = report["designs"]
    fixed, = design["fixed"]["options"]
    assert fixed["control"] == [] and len(fixed["cell_indices"]) == 4


def test_all_three_valued_two_by_two_grids_match_bounds_of_boolean_completions(db):
    # Derive expectations from ordinary Boolean policies under EVERY way of filling
    # the missing readings. Independent of the production three-valued reduction.
    matrices = list(product((False, True, None), repeat=4))
    labels = {frozenset({True}): "pass", frozenset({False}): "outside",
              frozenset({False, True}): "unknown"}

    def evaluate(design, condition, control):
        outcome = matrices[int(design)][int(2 * condition + control)]
        return ({}, {"signal": "missing"}) if outcome is None else ({"signal": int(outcome)}, {})

    # A search supports at most 128 combinations. Each batch uses 27 designs * 4 evaluations.
    for start in range(0, len(matrices), 27):
        axes = [axis("design", range(start, start + 27)), axis("condition", [0, 1]), axis("control", [0, 1])]
        case, source, _ = archive(db, axes, [{"output": "signal", "lower": 1}], evaluate)
        report = inspect(db, case, source, "design", "observed", "control")
        for design in report["designs"]:
            design_id, = design["design"]
            measured = matrices[int(design_id)]
            fixed_answers, adaptive_answers = set(), set()
            for answers in product((False, True), repeat=measured.count(None)):
                unknown = iter(answers)
                a, b, c, d = (next(unknown) if value is None else value for value in measured)
                fixed_answers.add((a and c) or (b and d))
                adaptive_answers.add((a or b) and (c or d))
            assert design["fixed"]["state"] == labels[frozenset(fixed_answers)], measured
            assert design["adaptive"]["state"] == labels[frozenset(adaptive_answers)], measured


def test_agent_receives_the_same_two_reopenable_results_that_the_workbench_stores(client, db, monkeypatch):
    import json
    from sqlalchemy.orm import sessionmaker
    from app.workbench import service
    from .test_workbench import finish_with_evidence, use_script

    monkeypatch.setattr(service, "SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    case, source, _ = archive(db, [axis("condition", [1, 2]), axis("setting", [1, 2])],
                              [{"output": "error", "upper": 0}], lambda a, b: ({"error": abs(a-b)}, {}))
    roles = [["observed", "control"], ["hidden", "control"]]
    calls = [("inspect_adjustment_policy", {"search_id": source.id, "roles": r}) for r in roles]
    provider = use_script(monkeypatch, [*calls, finish_with_evidence])
    response = client.post(f"/api/cases/{case}/workbench/runs", json={"message": "比较两种装调条件"})
    assert response.status_code == 202
    run = client.get(f"/api/cases/{case}/workbench").json()["runs"][0]
    ids = run["result"]["evidence_tool_call_ids"]
    assert len(set(ids)) == 2
    delivered = [json.loads(m["content"]) for m in provider.messages[-1] if m["role"] == "tool"]
    for call_id, role, message in zip(ids, roles, delivered, strict=True):
        trace = next(c for c in run["tools"] if c["id"] == call_id)
        view = trace["output"]["workbench_view"]
        assert view == message["result"]["workbench_view"]
        assert view["tool_call_id"] == message["tool_call_id"] == call_id
        assert (view["run_id"], view["comparison_id"]) == (source.content["baseline_id"], source.id)
        assert view["selection"] == {"mode": "adjustment", "roles": role}

from copy import deepcopy

import pytest
from pydantic import Field

from app.models import ExperimentCase, GeneratedContent
from app.study_api import Port, RowOutputs, StrictInput, Study
from app.workbench.studies import run_study
from app.workbench.study_search import SearchMeasure, StudyReassessment, StudySearch, reassess_search, run_search


class Mount(StrictInput):
    position: float = Field(ge=0)
    offset: float
    gain: float = Field(gt=0)


def measure(config):
    x, y = config.position, config.offset
    rows = [{"uid":"sample", "power":x*config.gain}, {"uid":"stray","power":999}]
    if x > 1:
        rows.reverse()
    if y < 0:
        rows = [r for r in rows if r["uid"] != "sample"]
    return {"rows":rows, "travel":x+y, "status":"completed"}


@pytest.fixture
def experiment(db):
    case = ExperimentCase(title="adjustable mount", goal="compare", cavity_type="custom")
    db.add(case)
    db.commit()
    p = Port(quantity="power", label="P", unit="W", scale=1)
    study = Study("mount_power", "power and travel", "1", "synthetic", Mount, measure, metrics={"travel":"travel"},
                  row_outputs={"rows":RowOutputs(columns={"power":p}, identity_fields=("uid",))})
    registry = {study.name:(study,"lab 1")}
    base = run_study(db.get_bind(),case.id,study,"lab 1",{"position":1,"offset":1,"gain":1})
    return case.id, registry, base["study_record"]["id"]


def test_grid_tracks_identity_and_keeps_multiple_feasible_tradeoffs(db, experiment):
    case, registry, rid = experiment
    recipe = StudySearch(run_id=rid,axes=[
        {"paths":[["position"],["offset"]],"samples":[[1,1],[2,2],[3,-1],[-1,0]]},
        {"paths":[["gain"]],"samples":{"start":[1],"stop":[2],"count":2}}],
        measures=[{"output":"rows.0.power","direction":"max","upper":3},{"output":"travel","direction":"min"}])
    def execute():
        return run_search(db.get_bind(),case,recipe,registry)
    report = execute()
    assert (len(report["rows"]),report["unavailable"],report["feasible"],report["preferred"]) == (8,4,3,1)
    baseline_solver = db.get(GeneratedContent, rid).content["solver_receipt"]
    assert report["baseline_solver_receipt"] == baseline_solver
    assert report["solver_receipts"] == {baseline_solver["sha256"]:baseline_solver}
    assert all(r["solver_sha256"] == baseline_solver["sha256"] for r in report["rows"] if "run_id" in r)
    preferred = [r for r in report["rows"] if r.get("preferred")]
    assert preferred[0]["coordinates"] == [1,1,2]
    assert preferred[0]["values"] == {"rows.0.power":2,"travel":2}
    assert report["rows"][2]["values"]["rows.0.power"] == 2  # row 1 moved to row 2
    assert report["rows"][3]["state"] == "outside"
    assert "标识" in report["rows"][4]["message"] and report["rows"][4]["run_id"]
    assert "run_id" not in report["rows"][6]
    assert execute()["id"] == report["id"]
    assert db.get(GeneratedContent,rid).content["inputs"] == {"position":1,"offset":1,"gain":1}

    # Competing objectives genuinely favor different feasible settings. All tradeoffs survive.
    trade = recipe.model_copy(update={"measures":[m.model_copy(update={"upper":None}) for m in recipe.measures]})
    second = run_search(db.get_bind(),case,trade,registry)
    assert [r["coordinates"] for r in second["rows"] if r.get("preferred")] == [[1,1,2],[2,2,2]]
    assert [r["rank"] for r in second["rows"] if r["state"]=="feasible"] == [2,1,2,1]
    with pytest.raises(ValueError,match="本案例"):
        run_search(db.get_bind(),case+1,recipe,registry)


def test_bad_grids_and_outputs(db, experiment):
    case, registry, rid = experiment
    base = dict(run_id=rid, measures=[{"output":"travel","lower":0}])
    for axes in ([{"paths":[["position"],["offset"]],"samples":[[1]]}],
                 [{"paths":[["position"],["position"]],"samples":[[1,2]]}],
                 [{"paths":[[p]],"samples":{"start":[0],"stop":[10],"count":20}} for p in ["position","offset"]]):
        with pytest.raises(ValueError):
            StudySearch(**base,axes=axes)
    good=StudySearch(**base,axes=[{"paths":[["position"]],"samples":[[1],[2]]}])
    result=run_search(db.get_bind(),case,good,registry)
    assert result["feasible"] == 2 and result["ranking_mode"] == "feasibility"
    assert result["preferred"] == 0 and result["preferred_indices"] == []
    assert all("rank" not in row for row in result["rows"])
    boundary=StudySearch(run_id=rid, axes=[{"paths":[["position"],["offset"]],"samples":[[.1,.2],[.1,.200001]]}],
                         measures=[{"output":"travel","upper":.3}])
    contact=run_search(db.get_bind(),case,boundary,registry)
    assert [r["state"] for r in contact["rows"]] == ["feasible","outside"]
    with pytest.raises(ValueError,match="同一求解器"):
        run_search(db.get_bind(),case,good,{})
    with pytest.raises(ValueError,match="基线输入"):
        run_search(db.get_bind(),case,StudySearch(**base,axes=[{"paths":[["missing"]],"samples":[[1]]}]),registry)


def test_interior_targets_and_raw_limits_reuse_the_same_archived_readings(db, experiment, monkeypatch):
    case, registry, rid = experiment
    power = "rows.0.power"
    request = StudySearch(run_id=rid, axes=[{"paths": [["gain"]], "samples": [[.2], [.4], [.5]]}],
                          measures=[{"output": power, "direction": "target", "target": .3}])
    result = run_search(db.get_bind(), case, request, registry)
    assert result["preferred_indices"] == [0, 1]
    assert result["measures"][power]["unit"] == "W"
    assert [r["values"][power] for r in result["rows"]] == [.2, .4, .5]
    assert [r["target_deviations"][power] for r in result["rows"]] == [.1, .1, .2]
    assert run_search(db.get_bind(), case, request, registry)["id"] == result["id"]

    def forbid_solver(*args, **kwargs):
        pytest.fail("Changing the decision criteria must reuse the frozen readings")

    monkeypatch.setattr("app.workbench.study_search.run_study", forbid_solver)
    revision = StudyReassessment(search_id=result["id"], measures=[
        {"output": power, "direction": "target", "target": .48, "upper": .41}])
    revised = reassess_search(db.get_bind(), case, revision)
    assert revised["preferred_indices"] == [1]
    assert [r["state"] for r in revised["rows"]] == ["feasible", "feasible", "outside"]
    assert [r["target_deviations"][power] for r in revised["rows"]] == [.28, .08, .02]
    assert [r["run_id"] for r in revised["rows"]] == [r["run_id"] for r in result["rows"]]
    assert db.get(GeneratedContent, result["id"]).content["preferred_indices"] == [0, 1]
    assert reassess_search(db.get_bind(), case, revision)["id"] == revised["id"]
    pure_limit = StudyReassessment(search_id=revised["id"], measures=[{"output": power, "upper": .51}])
    assert all("target_deviations" not in r for r in reassess_search(db.get_bind(), case, pure_limit)["rows"])


def test_target_cost_combines_with_max_and_keeps_partial_measurements(db, experiment):
    case, registry, rid = experiment
    request = StudySearch(run_id=rid, axes=[{"paths": [["position"], ["offset"]],
                          "samples": [[1, 1], [2, 1], [3, 1], [4, 1], [5, -6]]}],
                          measures=[{"output": "travel", "direction": "target", "target": 3.5},
                                    {"output": "rows.0.power", "direction": "max"}])
    report = run_search(db.get_bind(), case, request, registry)
    assert report["preferred_indices"] == [2, 3]
    assert [r.get("rank") for r in report["rows"]] == [3, 2, 1, 1, None]
    assert report["rows"][4]["target_deviations"] == {"travel": 4.5}
    assert report["rows"][4]["values"] == {"travel": -1}
    assert report["rows"][4]["state"] == "unavailable"
    for target in (0, -1):
        changed = StudyReassessment(search_id=report["id"], measures=[
            {"output": "travel", "direction": "target", "target": target}])
        assert reassess_search(db.get_bind(), case, changed)["preferred_indices"] == [4]


def test_target_distances_can_exceed_binary64_and_still_rank(db, experiment):
    case, registry, rid = experiment
    request = StudySearch(run_id=rid, axes=[{"paths": [["offset"]], "samples": [[9e307], [1e308]]}],
                          measures=[{"output": "travel", "direction": "target", "target": -1e308}])
    result = run_search(db.get_bind(), case, request, registry)
    assert result["preferred_indices"] == [0]
    assert [r["target_deviations"]["travel"].lower() for r in result["rows"]] == ["1.9e+308", "2e+308"]

    far_target = StudySearch(run_id=rid, axes=[{"paths": [["position"]], "samples": [[0], [1]]}],
                             measures=[{"output": "travel", "direction": "target", "target": 1e17}])
    far = run_search(db.get_bind(), case, far_target, registry)
    assert [r["values"]["travel"] for r in far["rows"]] == [1, 2]
    assert far["preferred_indices"] == [1]
    assert len({r["target_deviations"]["travel"] for r in far["rows"]}) == 2


def test_only_target_preferences_take_a_finite_reference_and_old_recipes_are_stable():
    for fields in ({"direction": "target"}, {"direction": "target", "target": float("inf")},
                   {"direction": "target", "target": True},
                   {"direction": "min", "target": 0}, {"target": 0, "lower": -1}):
        with pytest.raises(ValueError):
            SearchMeasure(output="travel", **fields)
    assert SearchMeasure(output="travel", direction="min").model_dump(mode="json") == {
        "output": "travel", "direction": "min", "lower": None, "upper": None, "rationale": ""}


def test_reassess_uses_archived_candidates_including_new_measures_and_recovers_rows(db, experiment, monkeypatch):
    case, registry, rid = experiment
    recipe = StudySearch(run_id=rid, axes=[{"paths":[["position"],["offset"]], "samples":[[1,1],[2,2],[3,-1],[-1,0]]}],
                         measures=[{"output":"travel","direction":"min","upper":3}])
    original = run_search(db.get_bind(), case, recipe, registry)
    assert original["preferred_indices"] == [0, 2]
    assert original["rows"][1]["state"] == "outside"
    assert original["rows"][3]["state"] == "unavailable" and "run_id" not in original["rows"][3]

    def forbidden(*args, **kwargs):
        raise AssertionError("Reassessment must not invoke any solver")

    monkeypatch.setattr("app.workbench.study_search.run_study", forbidden)
    searches = db.query(GeneratedContent).filter_by(case_id=case, content_type="study_search")
    runs = db.query(GeneratedContent).filter_by(case_id=case, content_type="scientific_study")
    run_count = runs.count()
    request = StudyReassessment(search_id=original["id"], measures=[
        {"output":"rows.0.power","direction":"max","rationale":"read the archived sample detector"},
        {"output":"travel","direction":"min"}])
    revised = reassess_search(db.get_bind(), case, request)
    assert searches.count() == 2 and runs.count() == run_count
    assert revised["ranking_mode"] == "pareto" and revised["preferred_indices"] == [0, 1]
    assert revised["source_search_id"] == revised["root_search_id"] == original["id"]
    assert revised["measures"]["rows.0.power"]["unit"] == "W"
    assert revised["rows"][1]["values"] == {"rows.0.power":2, "travel":4}
    assert revised["rows"][2]["values"] == {"travel":2}
    assert "标识" in revised["rows"][2]["measure_issues"]["rows.0.power"]
    assert revised["rows"][3]["message"] == original["rows"][3]["message"]
    assert revised["solver_receipts"] == original["solver_receipts"]
    assert [r.get("run_id") for r in revised["rows"]] == [r.get("run_id") for r in original["rows"]]
    assert revised["recipe"]["axes"] == original["recipe"]["axes"]
    assert reassess_search(db.get_bind(), case, request)["id"] == revised["id"]
    assert searches.count() == 2

    limits = StudyReassessment(search_id=revised["id"], measures=[{"output":"travel","lower":2,"upper":2}])
    filtered = reassess_search(db.get_bind(), case, limits)
    assert filtered["source_search_id"] == revised["id"] and filtered["root_search_id"] == original["id"]
    assert [r["state"] for r in filtered["rows"]] == ["feasible","outside","feasible","unavailable"]
    assert filtered["ranking_mode"] == "feasibility" and filtered["preferred_indices"] == []
    assert db.get(GeneratedContent, original["id"]).content == {k:v for k,v in original.items() if k != "id"}
    assert db.get(GeneratedContent, revised["id"]).content == {k:v for k,v in revised.items() if k != "id"}
    with pytest.raises(ValueError, match="本工程"):
        reassess_search(db.get_bind(), case+1, request)
    with pytest.raises(ValueError, match="基线计算实际发布"):
        reassess_search(db.get_bind(), case, StudyReassessment(search_id=revised["id"], measures=[
            {"output":"unpublished","direction":"max"}]))
    with pytest.raises(ValueError, match="同一评价量"):
        StudyReassessment(search_id=revised["id"], measures=[{"output":"travel","direction":"min"}]*2)
    assert runs.count() == run_count and searches.count() == 3


def test_archived_reassessment_respects_fixed_conditions_and_audits_missing_sources(db, experiment):
    case, _, _ = experiment
    detector = Port(quantity="detector.response", label="sensor signal", unit="mV", scale=.001,
                    conditions={"gain":{"path":"gain", "label":"探测器增益"}}, comparison_conditions=("gain",))

    def exposure(config):
        return {"gain":config.gain, "signal":config.position * config.gain, "travel":config.position}

    study = Study("detector_gain", "detector control", "1", "synthetic", Mount, exposure,
                  metrics={"travel":"travel"}, output_ports={"signal":detector})
    base = run_study(db.get_bind(), case, study, "lab 1", {"position":1, "offset":0, "gain":1})
    grid = run_search(db.get_bind(), case, StudySearch(run_id=base["study_record"]["id"], axes=[
        {"paths":[["position"],["gain"]], "samples":[[1,1],[2,1],[3,2]]}], measures=[
        {"output":"travel","direction":"max"}]), {study.name:(study,"lab 1")})
    request = StudyReassessment(search_id=grid["id"], measures=[{"output":"signal","direction":"max"}])
    reviewed = reassess_search(db.get_bind(), case, request)
    assert reviewed["preferred_indices"] == [1]
    assert reviewed["rows"][1]["values"]["signal"] == 2
    assert reviewed["rows"][2]["state"] == "unavailable" and "探测器增益" in reviewed["rows"][2]["message"]

    # Model a legacy import with a missing candidate reference. Its once-scored value
    # is still readable in the source, but cannot establish provenance for a new decision.
    db.delete(db.get(GeneratedContent, grid["rows"][1]["run_id"]))
    db.commit()
    cached = reassess_search(db.get_bind(), case, StudyReassessment(search_id=reviewed["id"], measures=[
        {"output":"signal", "direction":"min"}]))
    assert cached["rows"][1]["state"] == "unavailable" and "缺少候选计算" in cached["rows"][1]["message"]
    assert cached["rows"][1]["values"] == {} and cached["preferred_indices"] == [0]


def _legacy_detector_search(db, experiment):
    case, registry, rid = experiment
    recipe = StudySearch(run_id=rid, axes=[{"paths":[["position"]], "samples":[[1],[2]]}],
                         measures=[{"output":"rows.0.power","lower":0}])
    search = run_search(db.get_bind(), case, recipe, registry)
    archived = db.get(GeneratedContent, search["id"])
    legacy = deepcopy(archived.content)
    legacy.pop("ranking_mode")
    # Old reports labelled every feasible candidate preferred even with only a hard limit.
    legacy.update(preferred=2, preferred_indices=[0,1])
    for row in legacy["rows"]:
        row.update(rank=1, preferred=True)
        result = db.get(GeneratedContent, row["run_id"])
        saved = deepcopy(result.content)
        saved["definition"]["row_outputs"]["rows"].pop("identity_fields")
        result.content = saved
    archived.content = legacy
    db.commit()
    return search["id"], legacy


def test_old_table_receipts_keep_bound_values_without_inventing_a_new_row_match(db, experiment):
    case, _, _ = experiment
    search_id, legacy = _legacy_detector_search(db, experiment)
    reused = reassess_search(db.get_bind(), case, StudyReassessment(search_id=search_id,
        measures=[{"output":"rows.0.power", "upper":2}]))
    assert reused["ranking_mode"] == "feasibility" and not reused["preferred_indices"]
    assert [r["values"]["rows.0.power"] for r in reused["rows"]] == [1,2]

    # Today's plugin advertises row identities; the archived declaration does not.
    unbound = reassess_search(db.get_bind(), case, StudyReassessment(search_id=search_id,
        measures=[{"output":"rows.1.power", "direction":"max"}]))
    assert unbound["unavailable"] == 2 and unbound["preferred_indices"] == []
    assert all("行标识" in row["message"] for row in unbound["rows"])
    travel = reassess_search(db.get_bind(), case, StudyReassessment(search_id=search_id, measures=[
        {"output":"travel", "direction":"min"}]))
    restored = reassess_search(db.get_bind(), case, StudyReassessment(search_id=travel["id"], measures=[
        {"output":"rows.0.power", "direction":"max"}, {"output":"travel", "direction":"min"}]))
    assert restored["status"] == "completed" and restored["preferred_indices"] == [0,1]
    assert [r["values"] for r in restored["rows"]] == [{"rows.0.power":1,"travel":2},{"rows.0.power":2,"travel":3}]
    assert all(r["root_measurements"] == ["rows.0.power"] for r in restored["rows"])
    assert restored["root_search_id"] == search_id and restored["source_search_id"] == travel["id"]
    assert db.get(GeneratedContent, search_id).content == legacy


@pytest.mark.parametrize("fault", ["missing_root","other_grid","other_case","coordinates","run_id","fingerprint"])
def test_archived_detector_witness_must_match_the_original_grid_and_sample(db, experiment, fault):
    case, _, _ = experiment
    search_id, legacy = _legacy_detector_search(db, experiment)
    middle = reassess_search(db.get_bind(), case, StudyReassessment(search_id=search_id, measures=[
        {"output":"travel", "direction":"min"}]))
    root = db.get(GeneratedContent, search_id)
    if fault == "missing_root":
        db.delete(root)
    elif fault == "other_case":
        other = ExperimentCase(title="other detector", goal="separate project", cavity_type="custom")
        db.add(other)
        db.flush()
        root.case_id = other.id
    else:
        wrong = deepcopy(legacy)
        if fault == "other_grid":
            wrong["recipe"]["axes"][0]["paths"] = [["gain"]]
        elif fault == "coordinates":
            wrong["rows"][0]["coordinates"] = [2]
        elif fault == "run_id":
            wrong["rows"][0]["run_id"] = wrong["rows"][1]["run_id"]
        else:
            wrong["rows"][0]["solver_sha256"] = "0" * 64
        root.content = wrong
    db.commit()
    reviewed = reassess_search(db.get_bind(), case, StudyReassessment(search_id=middle["id"], measures=[
        {"output":"rows.0.power", "direction":"max"}, {"output":"travel", "upper":3}]))
    assert reviewed["rows"][0]["values"] == {"travel":2}
    assert reviewed["rows"][0]["state"] == "unavailable"
    assert "首轮搜索" in reviewed["rows"][0]["measure_issues"]["rows.0.power"]
    if fault in ("missing_root","other_grid","other_case"):
        assert reviewed["unavailable"] == 2 and reviewed["status"] == "needs_input"
    else:
        assert reviewed["preferred_indices"] == [1] and reviewed["status"] == "partial"


def test_restoring_a_legacy_measure_keeps_the_original_condition_failure(db, experiment):
    case, _, _ = experiment
    detector = Port(quantity="detector.response", label="sensor signal", unit="mV", scale=.001,
                    conditions={"gain":{"path":"gain", "label":"探测器增益"}}, comparison_conditions=("gain",))

    def exposure(config):
        return {"rows":[{"uid":"sample","reading":config.gain * config.position,"gain":config.gain}], "travel":config.position}

    study = Study("gain_record", "detector normalization", "1", "synthetic", Mount, exposure,
                  metrics={"travel":"travel"}, row_outputs={"rows":RowOutputs(columns={"reading":detector}, identity_fields=("uid",))})
    base = run_study(db.get_bind(), case, study, "lab", {"position":1,"offset":0,"gain":1})
    path = "rows.0.reading"
    search = run_search(db.get_bind(), case, StudySearch(run_id=base["study_record"]["id"],
        axes=[{"paths":[["gain"]],"samples":[[1],[2]]}], measures=[{"output":path,"direction":"max"}]), {study.name:(study,"lab")})
    assert search["rows"][1]["values"] == {} and "探测器增益" in search["rows"][1]["measure_issues"][path]
    for row in search["rows"]:
        record = db.get(GeneratedContent, row["run_id"])
        archived = deepcopy(record.content)
        archived["definition"]["row_outputs"]["rows"].pop("identity_fields")
        record.content = archived
    db.commit()
    middle = reassess_search(db.get_bind(), case, StudyReassessment(search_id=search["id"], measures=[
        {"output":"travel","direction":"min"}]))
    restored = reassess_search(db.get_bind(), case, StudyReassessment(search_id=middle["id"], measures=[
        {"output":path,"direction":"max"}]))
    assert restored["preferred_indices"] == [0] and restored["rows"][0]["values"][path] == 1
    assert restored["rows"][1]["state"] == "unavailable" and restored["rows"][1]["values"] == {}
    assert "探测器增益" in restored["rows"][1]["measure_issues"][path]


def test_known_violation_survives_an_unavailable_measure_in_a_search_and_its_reassessments(db, experiment):
    case, registry, rid = experiment
    power = {"output":"rows.0.power", "direction":"max"}
    travel = {"output":"travel", "upper":1}
    # This setting lost its sample detector, but the actual recorded travel is 3 + (-1) = 2.
    grid = run_search(db.get_bind(), case, StudySearch(run_id=rid,
        axes=[{"paths":[["position"],["offset"]],"samples":[[3,-1]]}], measures=[power, travel]), registry)

    def check_rejection(report):
        row, = report["rows"]
        assert row["state"] == "outside" and row["outside"] == ["travel"]
        assert row["values"] == {"travel":2} and "标识" in row["measure_issues"]["rows.0.power"]
        assert row["message"] and "rank" not in row
        assert report["status"] == "partial"
        assert (report["feasible"], report["preferred"], report["outside"], report["unavailable"], report["incomplete"]) == (0,0,1,0,1)

    check_rejection(grid)
    unknown = reassess_search(db.get_bind(), case, StudyReassessment(search_id=grid["id"], measures=[
        {**travel, "upper":2}, power]))
    assert unknown["status"] == "needs_input"
    assert unknown["rows"][0]["state"] == "unavailable" and not unknown["rows"][0]["outside"]
    assert (unknown["outside"], unknown["unavailable"], unknown["incomplete"]) == (0,1,1)
    rejected = reassess_search(db.get_bind(), case, StudyReassessment(search_id=unknown["id"], measures=[travel, power]))
    check_rejection(rejected)
    resolved = reassess_search(db.get_bind(), case, StudyReassessment(search_id=rejected["id"], measures=[travel]))
    assert resolved["status"] == "completed" and resolved["outside"] == 1
    assert resolved["unavailable"] == resolved["incomplete"] == 0 and resolved["rows"][0]["outside"] == ["travel"]

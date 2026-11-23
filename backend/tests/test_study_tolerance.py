import math
from itertools import product

import pytest

from app.models import ExperimentCase, GeneratedContent
from app.workbench.study_tolerance import StudyTolerance, inspect_tolerance


def archive(db, axes, limits, evaluate):
    project = ExperimentCase(title="Assembly measurements", goal="", cavity_type="custom")
    db.add(project)
    db.flush()
    rows = []
    for grid in product(*(axis["samples"] for axis in axes)):
        coords = [v for vector in grid for v in vector]
        values, issues = evaluate(*coords)
        run = GeneratedContent(case_id=project.id, content_type="scientific_study", content={"solver_receipt":{"sha256":"source"}})
        db.add(run)
        db.flush()
        rows.append({"coordinates":coords,"run_id":run.id,"solver_sha256":"source","science_status":"completed",
                     "values":values,"measure_issues":issues,"state":"unavailable" if issues else "feasible"})
    base = rows[0]["run_id"]
    data = {"baseline_id":base, "recipe":{"run_id":base,"axes":axes,"measures":limits},
            "measures":{m["output"]:{"label":m["output"],"unit":"mm"} for m in limits},"rows":rows}
    source = GeneratedContent(case_id=project.id, content_type="study_search", content=data)
    db.add(source)
    db.commit()
    return project.id, source, data


def axis(name, samples):
    return {"paths":[[name]], "samples":[[x] for x in samples]}


def inspect(db, case_id, search, row, **changes):
    return inspect_tolerance(db.get_bind(), case_id, StudyTolerance.model_validate(
        {"search_id":search.id,"row_index":row,"axis_index":0,**changes}))


def test_slice_and_full_sampled_envelope_use_only_hard_limits(db):
    # A translating imaging system is sensitive to relative motion of the two mounts.
    settings = [axis("sensor", [1, -1, 0, 2]), axis("image_plane", [0, 1])]
    limits = [{"output":"defocus","upper":1}, {"output":"contrast","direction":"max"}]
    case, record, source = archive(db, settings, limits, lambda x,y: ({"defocus":abs(x-y)}, {"contrast":"camera disconnected"}))
    before = db.query(GeneratedContent).count()
    sliced = inspect(db, case, record, 4)
    assert sliced["lower"]["passed"]["position"] == -1 and sliced["lower"]["termination"] == "edge"
    assert sliced["upper"]["margin"] == 1 and sliced["upper"]["stop"]["position"] == 2
    assert sliced["upper"]["stop"]["row_index"] == 6 and sliced["upper"]["stop"]["issues"][0]["value"] == 2
    assert sliced["nominal"]["state"] == "pass" and sliced["nominal"]["row_index"] == 4
    assert [m["output"] for m in sliced["limits"]] == ["defocus"]
    all_settings = inspect(db, case, record, 4, scope="all")
    assert all_settings["lower"]["margin"] == 0
    assert all_settings["lower"]["stop"]["counts"] == {"pass":1,"outside":1,"unknown":0}
    assert all_settings["lower"]["stop"]["witness_coordinates"] == [-1,1]
    assert all_settings["nominal"]["counts"]["pass"] == 2 and len(all_settings["upper"]["stop"]["issues"]) == 1
    assert db.query(GeneratedContent).count() == before and record.content == source
    with pytest.raises(ValueError,match="本工程"):
        inspect(db,case+1,record,4)


def test_disconnected_transmission_bands_do_not_bridge_failure_or_a_missing_hard_limit(db):
    phases = [0,45,90,135,180,225,270,315,360]
    limits = [{"output":"transmission","lower":.5},{"output":"reference","lower":0}]
    def sensor(angle):
        value = math.cos(math.radians(angle)/2)**2
        return ({"transmission":value}, {"reference":"reference sensor missing"}) if angle in (45,225) else (
            {"transmission":value,"reference":1}, {})
    case, record, _ = archive(db,[axis("degrees",phases)],limits,sensor)
    report = inspect(db,case,record,8)
    assert report["upper"]["termination"] == "edge" and report["upper"]["margin"] == 0
    assert report["lower"]["margin"] == 90 and report["lower"]["passed"]["position"] == 270
    assert report["lower"]["stop"]["position"] == 225 and report["lower"]["stop"]["state"] == "outside"
    assert [i["output"] for i in report["lower"]["stop"]["issues"]] == ["transmission","reference"]
    left = inspect(db,case,record,0)
    stations = left["stations"]
    assert [s["position"] for s in stations] == phases
    assert [s["row_index"] for s in stations] == list(range(len(phases)))
    assert [s["run_id"] for s in stations] == [r["run_id"] for r in record.content["rows"]]
    assert [stations[i]["state"] for i in (0, 1, 4, 8)] == ["pass", "unknown", "outside", "pass"]
    assert left["upper"]["termination"] == "unknown" and left["upper"]["margin"] == 0
    assert left["upper"]["stop"]["issues"][0]["message"] == "reference sensor missing"
    failed = inspect(db,case,record,4)
    assert failed["nominal"]["state"] == "outside" and failed["lower"]["termination"] == "nominal"
    assert failed["upper"]["passed"] is None and failed["upper"]["margin"] is None


def test_missing_rows_do_not_open_an_interval_and_wrong_receipts_cannot_supply_results(db):
    axes = [axis("position",[1,2,3]),axis("mount",[10,20])]
    case, record, data = archive(db,axes,[{"output":"signal","lower":0}],lambda *_: ({"signal":1},{}))
    rows = data["rows"][:]
    rows.pop(3)  # The second setting of slot 2 was never recorded.
    rows[3] = {**rows[3],"solver_sha256":"unrelated"}
    rows[4] = {**rows[4],"run_id":1000000}
    record.content = {**data,"rows":rows}
    db.commit()
    result = inspect(db,case,record,0,scope="all")
    stop = result["upper"]["stop"]
    assert (stop["position"],stop["state"],stop["run_id"],stop["row_index"]) == (2,"unknown",None,None)
    assert stop["witness_coordinates"] == [2,20] and stop["counts"] == {"pass":1,"unknown":1,"outside":0}
    sliced = inspect(db,case,record,0)
    assert sliced["upper"]["stop"]["position"] == 3 and "指纹" in sliced["upper"]["stop"]["reason"]
    missing = inspect(db,case,record,4)
    assert missing["nominal"]["state"] == "unknown" and missing["nominal"]["run_id"] is None


def test_linked_parameters_preserve_the_full_vector_and_ambiguous_ordering_is_reported(db):
    linked = {"paths":[["x"],["yaw"]],"samples":[[0,0],[1,1],[1,3],[2,4]]}
    case, record, _ = archive(db,[linked],[{"output":"image","upper":5}],lambda x,y: ({"image":x+y},{}))
    first = inspect(db,case,record,0)
    stop = first["upper"]["stop"]
    assert stop["state"] == "unknown" and stop["coordinates"] is None
    assert stop["alternative_coordinates"] == [[1,1],[1,3]]
    assert "联动" in stop["reason"] and first["upper"]["passed"]["position"] == 0
    ordered = inspect(db,case,record,1,coordinate_index=1)
    assert ordered["coordinate_path"] == ["yaw"]
    assert ordered["upper"]["passed"]["coordinates"] == [1,3] and ordered["upper"]["margin"] == 2
    assert ordered["upper"]["stop"]["coordinates"] == [2,4]


def test_grid_edges_and_finite_json_at_extreme_coordinates(db):
    case, record, _ = archive(db,[axis("x",[-1e308,1e308])],[{"output":"read","upper":.3}],
                              lambda x: ({"read":.1+.2},{}))
    result = inspect(db,case,record,0)
    assert result["upper"]["termination"] == "edge" and result["upper"]["passed"]["position"] == 1e308
    assert result["upper"]["margin"] is None
    for changes, error in [({"axis_index":1},"参数组"),({"coordinate_index":1},"坐标"),({"row_index":2},"候选行")]:
        with pytest.raises(ValueError, match=error):
            inspect(db,case,record,0,**changes)


def test_objectives_without_a_limit_cannot_define_tolerance(db):
    case, source, _ = archive(db,[axis("distance",[1,2])],[{"output":"focus","direction":"min"}],lambda x: ({"focus":x},{}))
    with pytest.raises(ValueError, match="上限或下限"):
        inspect(db,case,source,0)

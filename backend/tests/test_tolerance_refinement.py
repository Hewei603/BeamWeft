import math
from copy import deepcopy
from dataclasses import replace
from typing import Literal

import pytest
from pydantic import Field

from app.models import ExperimentCase, GeneratedContent
from app.study_api import Port, StrictInput, Study
from app.workbench.studies import run_study
from app.workbench.study_search import StudySearch, run_search
from app.workbench.study_tolerance import StudyTolerance, inspect_tolerance
from app.workbench.tolerance_refinement import MAX_PROBES, RefineStudyTolerance, refine_tolerance


class Screw(StrictInput):
    kind: Literal["screw"] = "screw"
    setting: float


class Detent(StrictInput):
    kind: Literal["detent"] = "detent"
    setting: int


class Rig(StrictInput):
    x: float
    offset: float = 0.
    mount: Screw | Detent = Field(discriminator="kind")
    step: float = Field(default=0, multiple_of=.125)


def aperture(rig):
    # A narrow obscuration is present below the coarsely located outer edge.
    x = rig.x + rig.offset + rig.step + rig.mount.setting
    return {"light":0. if 0.1 < x < 0.16 or x > .78 else 1., "travel":abs(x)}


def unobserved(rig):
    if rig.x == .5:
        return {"status":"partial", "travel":abs(rig.x)}
    return aperture(rig)


def missing_secondary(rig):
    output = aperture(rig)
    if rig.x not in (0.,1.):
        output.pop("travel")
        output["status"] = "partial"
    return output


def signed(rig):
    return {"light":1. if rig.x <= 1. else 0., "travel":0.}


def setup(db, compute=aperture, *, axes=None, mount="screw"):
    case = ExperimentCase(title="Test aperture", goal="", cavity_type="custom")
    db.add(case)
    db.commit()
    method = Study("test_alignment","Aperture","1","Frozen response",Rig,compute,
                   output_ports={"light":Port(quantity="light",unit="W",scale=1,label="Signal",available_on_partial=True),
                                 "travel":Port(quantity="length",unit="mm",scale=.001,label="Travel",available_on_partial=True)})
    producer = "LaserClaw"
    registry = {method.name:(method,producer)}
    base = run_study(db.get_bind(),case.id,method,producer,{"x":0,"mount":{"kind":mount,"setting":0}})
    recipe = StudySearch(run_id=base["study_record"]["id"],
                         axes=axes or [{"paths":[["x"]],"samples":[[0],[1]]}],
                         measures=[{"output":"light","lower":.5},{"output":"travel","direction":"min"}])
    source = run_search(db.get_bind(),case.id,recipe,registry)
    return case.id, source, registry


def request(report, **options):
    return RefineStudyTolerance.model_validate({"search_id":report["id"], "row_index":0, "axis_index":0,
                                               "side":"upper", "resolution":.125, **options})


def test_actual_midpoints_keep_other_parameters_fixed_without_promising_a_continuous_band(db):
    axes = [{"paths":[["offset"]], "samples":[[0],[.125]]}, {"paths":[["x"]],"samples":[[0],[1]]}]
    cid, source, registry = setup(db, axes=axes)
    # The zero-offset section has a sampled pass/fail bracket [0,1]. The other setup
    # actually has a different verdict at the origin, so silently changing it would be observable.
    assert source["rows"][2]["state"] == "outside"
    args = request(source,axis_index=1)
    result = refine_tolerance(db.get_bind(), cid, args, registry)
    info = result["refinement"]
    assert (info["pass_position"],info["outside_position"],info["status"],info["evaluations"]) == (.75,.875,"resolved",3)
    assert info["width"] == .125 and info["source_search_id"] == source["id"]
    assert result["recipe"]["axes"][0]["samples"] == [[0]]
    assert result["recipe"]["axes"][1]["samples"] == [[0],[.5],[.75],[.875],[1]]
    assert result["measures"] == source["measures"]
    for row in result["rows"]:
        run = db.get(GeneratedContent,row["run_id"])
        assert row["coordinates"] == [run.content["inputs"]["offset"],run.content["inputs"]["x"]]
        assert run.content["inputs"]["offset"] == 0 and row["solver_sha256"] == source["rows"][0]["solver_sha256"]
    # The unseen point within [0, .75] fails. The reported transition is local to the sampled points.
    assert aperture(Rig(x=.125,mount=Screw(setting=0)))["light"] < .5
    assert all(r["coordinates"][1] != .125 for r in result["rows"])
    frozen = db.get(GeneratedContent,source["id"]).content
    assert frozen == {k:v for k,v in source.items() if k != "id"}
    before = db.query(GeneratedContent).count()
    repeat = refine_tolerance(db.get_bind(), cid, args, registry)
    assert result["refinement_execution"] == {"computed":3, "reused":0, "without_record":0}
    assert repeat["refinement_execution"] == {"computed":0, "reused":3, "without_record":0}
    assert repeat["original_refinement_execution"] == result["refinement_execution"]
    assert {k:v for k,v in repeat.items() if k != "refinement_execution"} == {
        k:v for k,v in result.items() if k != "refinement_execution"}
    assert db.query(GeneratedContent).count() == before
    with pytest.raises(ValueError, match="固定其他"):
        refine_tolerance(db.get_bind(), cid, request(source,axis_index=1,scope="all"),registry)


def test_a_missing_required_evaluation_ends_refinement_but_a_missing_preference_does_not(db):
    for method, end in [(unobserved, "unknown"), (missing_secondary, "resolved")]:
        cid, source, registry = setup(db, method)
        result = refine_tolerance(db.get_bind(),cid,request(source),registry)
        info = result["refinement"]
        assert info["status"] == end
        sample = next(r for r in result["rows"] if r["coordinates"] == [.5])
        assert sample["state"] == "unavailable" and sample["science_status"] == "partial"
        if end == "unknown":
            assert info["evaluations"] == 1 and (info["pass_position"],info["outside_position"]) == (0,1)
            assert info["blocked"]["position"] == .5 and info["blocked"]["run_id"] == sample["run_id"]
            assert info["blocked"]["issues"][0]["output"] == "light"
            reread = inspect_tolerance(db.get_bind(),cid,StudyTolerance(search_id=result["id"],row_index=0,axis_index=0))
            assert reread["upper"]["termination"] == "unknown"
        else:
            assert info["pass_position"] == .75 and info["evaluations"] == 3


def test_environment_mismatch_and_unidentified_implementations_do_not_run_extra_candidates(db):
    cid, source, registry = setup(db)
    name, (method, producer) = next(iter(registry.items()))
    args = request(source)
    count = db.query(GeneratedContent).count()
    for installation in ({}, {name:(replace(method,compute=signed),producer)}):
        with pytest.raises(ValueError, match="同一求解器"):
            refine_tolerance(db.get_bind(),cid,args,installation)
    archived = db.get(GeneratedContent,source["id"])
    digest = source["rows"][0]["solver_sha256"]
    for field, value in [("complete",False),("opaque_state",True)]:
        altered = deepcopy(source)
        altered.pop("id")
        altered["solver_receipts"][digest][field] = value
        archived.content = altered
        db.commit()
        with pytest.raises(ValueError, match="同一求解器"):
            refine_tolerance(db.get_bind(),cid,args,registry)
    assert db.query(GeneratedContent).count() == count


@pytest.mark.parametrize(("path","mount","maximum"),[
    (["mount","setting"],"detent",1),
    (["step"],"screw",1),
])
def test_discrete_and_stepped_controls_are_not_probed_between_settings(db,path,mount,maximum):
    cid, source, registry = setup(db,axes=[{"paths":[path],"samples":[[0],[maximum]]}],mount=mount)
    count = db.query(GeneratedContent).count()
    with pytest.raises(ValueError, match="离散"):
        refine_tolerance(db.get_bind(),cid,request(source),registry)
    assert db.query(GeneratedContent).count() == count


def test_tagged_continuous_variant_and_moving_two_inputs_have_different_edit_contracts(db):
    cid, source, registry = setup(db,axes=[{"paths":[["mount","setting"]],"samples":[[0],[1]]}])
    assert refine_tolerance(db.get_bind(),cid,request(source),registry)["refinement"]["status"] == "resolved"
    cid, source, registry = setup(db,axes=[{"paths":[["x"],["offset"]],"samples":[[0,0],[1,1]]}])
    with pytest.raises(ValueError, match="联动采样组"):
        refine_tolerance(db.get_bind(),cid,request(source),registry)


def test_budget_and_adjacent_machine_values_report_the_attained_bracket(db):
    cid, source, registry = setup(db)
    result = refine_tolerance(db.get_bind(),cid,request(source,resolution=1e-10),registry)
    info = result["refinement"]
    assert info["status"] == "budget" and info["evaluations"] == MAX_PROBES and info["width"] == 2**-MAX_PROBES
    cid, source, registry = setup(db,signed,axes=[{"paths":[["x"]],"samples":[[1],[math.nextafter(1,2)]]}])
    result = refine_tolerance(db.get_bind(),cid,request(source,resolution=1e-20),registry)
    info = result["refinement"]
    assert info["status"] == "precision" and info["evaluations"] == 0 and info["width"] == math.ulp(1.)
    with pytest.raises(ValueError,match="满足所填分辨率"):
        refine_tolerance(db.get_bind(),cid,request(source,resolution=math.ulp(1.)),registry)


def test_endpoint_readings_keep_their_legacy_evidence_link(db):
    cid, source, registry = setup(db)
    archived = db.get(GeneratedContent, source["id"])
    derived = deepcopy(archived.content)
    derived.update(root_search_id=source["id"], source_search_id=source["id"])
    derived["rows"][0]["root_measurements"] = ["light"]
    previous = GeneratedContent(case_id=cid,content_type="study_search",content=derived)
    db.add(previous)
    db.commit()
    result = refine_tolerance(db.get_bind(),cid,request({"id":previous.id}),registry)
    assert result["rows"][0]["measurement_source_search_id"] == source["id"]
    assert "root_measurements" not in result["rows"][1]

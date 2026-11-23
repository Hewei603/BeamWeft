from copy import deepcopy
import asyncio

import pytest

from app.models import GeneratedContent
from app.workbench import projects
from app.workbench.project_outputs import project_outputs
from app.workbench.studies import run_study
from app.workbench.tools import STUDIES
from app.workbench.study_search import StudySearch, run_search
from app.workbench.study_usage import StudyUsage, study_usage


def set_up(db, client, *, branch=False, fixed=False, inherited_index=False, catalog_medium=False):
    case_id = client.post("/api/cases", json={"title": "Partially measured optical bench"}).json()["id"]
    bench = {"kind": "space", "uid": "rail/1", "name": "Translation rail", "length_mm": 40}
    unknown = {"kind": "thick_lens", "uid": "unknown", "name": "Unlabelled glass", "length_mm": 2, "front_radius_mm": 35}
    arm = {"uid": "red%arm", "name": "Diagnostic arm", "target_uid": bench["uid"], "target_n": 1.4,
           "wavelength_nm": 808, "tangential": {"radius_mm": .25, "wavefront": "waist", "m2": 1},
           "elements": [{"kind": "space", "uid": "rail/1", "name": "Separate input rail", "length_mm": 5}]}
    if inherited_index:
        bench["n"] = 1.4
        arm.update(wavelength_nm=1030, target_n=None, through_uid=bench["uid"])
    if catalog_medium:
        bench.update(n=None, index_reference={"model":"nbk7","wavelength_nm":1030})
        arm.update(target_n=None, through_uid=bench["uid"])
    plane = {"uid":"screen/12", "name":"Camera", **({"position_reference":"fixed_z", "z_mm":12} if fixed else
             {"target_uid":bench["uid"], "depth_mm":12})}
    model = {"title":"Study", "system":{"mode":"single_pass", "wavelength_nm":1030, "input_radius_mm":.25,
             "elements":[bench, unknown]}, "observation_planes":[plane], "incident_paths":[arm] if branch else [],
             "notes":"Second lens remains on the real bench."}
    revision = projects.save_project(db, case_id, projects.SaveProject(project=model, change_note="Bench inventory"))["revision_id"]
    catalog = project_outputs(db, case_id, revision, "optics.ordered-layout.v1", compact=True)["outputs"]
    port = next(k for k, item in catalog.items() if item.get("plane_uid") == plane["uid"] and
                item.get("path_uid") == (arm["uid"] if branch else "primary"))
    study, provider = STUDIES["propagate_coherent_field"]
    out = run_study(db.get_bind(), case_id, study, provider, {"samples":128,"window_mm":2},
                    connections={"layout":{"revision_id":revision, "port":port, "registration":"Same bench and axis."}})
    return case_id, revision, out["study_record"]["id"]


def search(db, case_id, run_id, paths, points):
    recipe = StudySearch(run_id=run_id, registration="Camera tracks the recorded reference.",
        axes=[{"paths":[["layout", *p] for p in paths], "samples":points}], measures=[{"output":"z_mm","direction":"max"}])
    return run_search(db.get_bind(), case_id, recipe, STUDIES)


def test_sweeping_camera_depth_preserves_the_rest_of_the_project_and_old_receipt(db, client):
    cid, rid, run = set_up(db, client)
    src = deepcopy(projects.read_project(db, cid, rid)["project"])
    result = search(db, cid, run, [["system","elements",0,"length_mm"]], [[12],[20],[1000]])
    assert [r["state"] for r in result["rows"]] == ["feasible","feasible","unavailable"]
    assert result["rows"][1]["values"] == {"z_mm":20}
    trial = db.get(GeneratedContent, result["rows"][1]["run_id"]).content
    ref = trial["connections"]["layout"]
    other = projects.read_project(db, cid, ref["revision_id"])
    assert other["model_kind"] == "draft" and other["source_revision_id"] == rid
    expected = deepcopy(src)
    expected["observation_planes"][0]["depth_mm"] = 20
    assert other["project"] == expected and projects.read_project(db, cid)["project"] == src
    assert db.query(GeneratedContent).filter_by(case_id=cid, content_type="optical_snapshot").count() == 1
    assert projects.read_project(db,cid,rid)["project"]["system"]["elements"][1]["n"] is None
    contradictory = search(db, cid, run, [["system","elements",0,"length_mm"],["observation_planes",0,"depth_mm"]],
                           [[12,22]])
    assert contradictory["unavailable"] == 1 and "同时满足" in contradictory["rows"][0]["message"]
    assert db.query(GeneratedContent).filter_by(case_id=cid, content_type="optical_snapshot").count() == 1


def test_dispersive_branch_scan_updates_the_branch_not_the_host_or_duplicate_uid(db, client):
    cid, rid, run = set_up(db, client, branch=True)
    src = deepcopy(projects.read_project(db,cid,rid)["project"])
    result = search(db, cid, run, [["system","elements",0,"length_mm"],["system","elements",1,"n"]],
                    [[5,1.4],[8,1.6]])
    assert result["feasible"] == 2
    row = db.get(GeneratedContent,result["rows"][1]["run_id"]).content
    trial = projects.read_project(db,cid,row["connections"]["layout"]["revision_id"])["project"]
    expected = deepcopy(src)
    expected["incident_paths"][0]["target_n"] = 1.6
    expected["incident_paths"][0]["elements"][0]["length_mm"] = 8
    assert trial == expected
    assert row["inputs"]["layout"]["system"]["wavelength_nm"] == 808
    assert row["output"]["z_mm"] == 20


@pytest.mark.parametrize("inherited", [True,False])
def test_wavelength_sweep_requires_an_index_at_the_new_wavelength(db,client,inherited):
    cid, _, run = set_up(db,client,branch=True,inherited_index=inherited)
    result = search(db,cid,run,[["system","wavelength_nm"]],[[920]])
    assert result["unavailable"] == 1 and result["preferred"] == 0


def test_wavelength_sweep_recalculates_catalog_medium_on_the_branch_only(db,client):
    from app.physics.materials import n_real
    cid, rid, run = set_up(db,client,branch=True,catalog_medium=True)
    result = search(db,cid,run,[["system","wavelength_nm"]],[[920]])
    assert result["feasible"] == 1
    trial = db.get(GeneratedContent,result["rows"][0]["run_id"]).content
    candidate = projects.read_project(db,cid,trial["connections"]["layout"]["revision_id"])["project"]
    expected = deepcopy(projects.read_project(db,cid,rid)["project"])
    expected["incident_paths"][0]["wavelength_nm"] = 920
    assert candidate == expected
    projected = trial["inputs"]["layout"]["system"]["elements"][-1]
    assert projected["n"] == pytest.approx(n_real("nbk7",920))
    assert projected["index_reference"] == {"model":"nbk7","wavelength_nm":920}


def test_a_fixed_station_depth_cannot_overwrite_the_host_length(db, client):
    cid, rid, run = set_up(db, client, fixed=True)
    result = search(db, cid, run, [["system","elements",0,"length_mm"]], [[14]])
    assert result["unavailable"] == 1
    assert "原工程" in result["rows"][0]["message"]
    assert db.query(GeneratedContent).filter_by(case_id=cid,content_type="optical_snapshot").count() == 0
    assert projects.read_project(db,cid)["revision_id"] == rid


def test_impact_distinguishes_downstream_edits_from_a_shifted_external_frame(db, client):
    cid, rid, run = set_up(db, client, branch=True)
    old = projects.read_project(db,cid,rid)["project"]
    def compare(model, parent):
        replacement = projects.save_project(db, cid, projects.SaveProject(
            project=model, base_revision_id=parent, change_note="Measure another part of the bench"))["revision_id"]
        result = study_usage(db.get_bind(), cid, StudyUsage(source_id=rid,compare_revision_id=replacement))
        return replacement, next(item for item in result["items"] if item["id"] == run)
    measured = deepcopy(old)
    measured["system"]["elements"][1]["n"] = 1.53
    second, same = compare(measured,rid)
    assert same["state"] == "same" and not same["inputs"][0]["value_changed"]
    assert same["inputs"][0]["reference_changes"][0]["current"] == f"#{second}"
    measured["system"]["elements"].insert(0, {"kind":"space","uid":"offset","name":"Move the mounted branch","length_mm":30})
    _, moved = compare(measured,second)
    difference = moved["inputs"][0]
    assert moved["state"] == "conditions" and not difference["value_changed"]
    assert {c["key"] for c in difference["condition_changes"]} == {"optics.path-source-z.mm","optics.path-end-z.mm"}


def test_unchanged_values_can_have_different_registration_and_dispersion_owners(db, client):
    cid, rid, run = set_up(db, client, branch=True, inherited_index=True)
    model = deepcopy(projects.read_project(db,cid,rid)["project"])
    model["incident_paths"][0]["target_n"] = 1.4
    plane = model["observation_planes"][0]
    model["observation_planes"] = [{"uid":plane["uid"],"name":plane["name"],"position_reference":"fixed_z","z_mm":12}]
    replacement = projects.save_project(db, cid, projects.SaveProject(
        project=model, base_revision_id=rid, change_note="Keep today's reading but change how it follows hardware"))["revision_id"]
    result = study_usage(db.get_bind(),cid,StudyUsage(source_id=rid,compare_revision_id=replacement))
    change = next(item for item in result["items"] if item["id"]==run)["inputs"][0]
    assert change["state"] == "conditions" and not change["value_changed"]
    assert {d["key"] for d in change["condition_changes"]} == {"observation.mount","index-origin/host/rail%2F1"}


@pytest.mark.parametrize("port", ["v1/layout","layout-prefix-v1/station/primary/layout"])
def test_catalog_identity_and_inactive_fields_cannot_be_search_axes_even_at_the_baseline(db, client, port):
    from app.models import AgentTask, ExperimentCase
    from app.workbench.contracts import ArtifactQuery
    from app.workbench.tools import execute
    cid = client.post("/api/cases", json={"title":"Source and mask audit"}).json()["id"]
    origin = {"model_revision_id":71,"version":2,"name":"Bench part","instance_uid":"one","ordinal":1,"wavelength_nm":800}
    optics = [{"uid":"lens","name":"Ideal lens","kind":"lens","focal_mm":100,"n":None,"component_origin":origin},
              {"uid":"rail","name":"Rail","kind":"space","length_mm":20}]
    project = {"title":"Physical controls", "system":{"mode":"single_pass","wavelength_nm":800,"input_radius_mm":.25,"elements":optics},
               "parts":[{"element_index":1,"source":"Certified spacer", "locked_parameters":["n"]}],
               "observation_planes":[{"uid":"station","name":"Screen","target_uid":"rail","depth_mm":10}]}
    rid = projects.save_project(db,cid,projects.SaveProject(project=project,change_note="Baseline"))["revision_id"]
    study, provider = STUDIES["propagate_coherent_field"]
    run = run_study(db.get_bind(),cid,study,provider,{"samples":128,"window_mm":2},
                    connections={"layout":{"revision_id":rid,"port":port,"registration":"Same beam"}})["study_record"]["id"]
    # A pre-contract receipt may contain broad old permissions; reading or using it must never trust them.
    item = db.get(GeneratedContent,run)
    old = deepcopy(item.content)
    old["project_evidence"]["layout"]["scan_bindings"] = [{"input":["system"],"project":["system"]}]
    item.content = old
    db.commit()
    paths = client.get(f"/api/cases/{cid}/studies/runs/{run}/searches").json()["layout_parameters"]["layout"]
    task = AgentTask(case_id=cid, goal="Inspect and change a source-backed parameter", mode="workbench", status="running")
    db.add(task)
    db.commit()
    args = dict(db=db, case=db.get(ExperimentCase,cid),task=task,provider=None,session_id=None,attachment_ids=[])
    found = asyncio.run(execute("read_artifact", ArtifactQuery(content_id=run,path=["search_parameters","layout"]),**args))
    assert paths == found["content"]
    visible = {tuple(v["path"]) for v in paths}
    element = ["system","elements",0]
    for path in (element+["focal_mm"], ["observation_planes",0,"depth_mm"], ["system","wavelength_nm"]):
        assert tuple(path) in visible
    assert tuple(element+["n"]) not in visible
    for field, current, trial in [(element+["component_origin","model_revision_id"],71,999),
                                  (element+["component_origin","wavelength_nm"],800,850),
                                  (element+["gradient_per_mm"],0,2), (["system","elements",1,"n"],1,1.5)]:
        assert tuple(field) not in visible
        proposal = StudySearch(run_id=run,registration="Same reference",axes=[
            {"paths":[["layout",*field]],"samples":[[current],[trial]]}],measures=[{"output":"z_mm","direction":"min"}])
        result = asyncio.run(execute("search_study_parameters",proposal,**args))
        assert result["unavailable"] == 2 and result["preferred"] == 0
        assert all("原工程" in row["message"] and "run_id" not in row for row in result["rows"])
    assert db.query(GeneratedContent).filter_by(case_id=cid,content_type="optical_snapshot").count() == 0
    result = search(db,cid,run,[element+["focal_mm"]],[[80]])
    assert result["feasible"] == 1
    variant = db.get(GeneratedContent,result["rows"][0]["run_id"]).content
    candidate = projects.read_project(db,cid,variant["connections"]["layout"]["revision_id"])["project"]
    expected = deepcopy(projects.read_project(db,cid,rid)["project"])
    expected["system"]["elements"][0]["focal_mm"] = 80
    assert candidate == expected and candidate["system"]["elements"][0]["n"] is None
    # An incompletely imported old run can still show its original search history.
    incomplete = deepcopy(item.content)
    incomplete["connections"]["layout"]["revision_id"] = 999999
    item.content = incomplete
    db.commit()
    restored = client.get(f"/api/cases/{cid}/studies/runs/{run}/searches").json()
    assert restored["items"] and restored["layout_parameters"]["layout"] == []
    assert search(db,cid,run,[element+["focal_mm"]],[[80]])["unavailable"] == 1


def test_registered_nested_controls_matrix_units_and_calibration_share_one_contract(db,client,monkeypatch):
    from typing import Annotated, Literal
    from pydantic import Field
    from app.study_api import ElementLaw, StrictInput, Study
    from app.workbench.observation import OpticalLayout
    from app.workbench.device_laws import device_definition
    from app.workbench.optics import analyze_system, scan_system
    from app.workbench.project_outputs import LAYOUT

    class Adjustable(StrictInput):
        role: Literal["knob"]
        v: float
        q: Annotated[int, Field(json_schema_extra={"readOnly": True})]

    class Calibration(StrictInput):
        role: Literal["reading"]
        v: float = Field(json_schema_extra={"readOnly": True})

    class Law(StrictInput):
        wl: float
        levels: list[Annotated[Adjustable | Calibration, Field(discriminator="role")]]
        fixed: list[float] = Field(json_schema_extra={"readOnly": True})
        archived: float = Field(3,json_schema_extra={"readOnly": True})
        offset: float = 0

    params = {"levels":[{"role":"knob","v":2,"q":7},{"role":"reading","v":3}],"fixed":[4]}
    params.update(archived=3,offset=0)
    lens = Study("nested_phase_power","Nested phase law","1","Thin quadratic phase in air",Law,
        lambda args:{"map":{"matrix_t":[1,0,-(args.levels[0].v+args.offset)/1000,1]}},
        element_law=ElementLaw(representation="ray_matrix",wavelength_input="wl",output="map",preset=params))
    class LayoutInput(StrictInput):
        layout: OpticalLayout

    def transfer(args):
        optical = analyze_system(args.layout.system,profile=False)
        return {"radius":optical["planes"]["tangential"]["output"]["radius_mm"]}
    feed = Study("physical_transfer","Transfer","1","Gaussian through registered components",LayoutInput,transfer,
                 metrics={"radius":"Radius / mm"},input_ports={"layout":LAYOUT})
    for s in [lens,feed]:
        monkeypatch.setitem(STUDIES,s.name,(s,"independent-lab"))
    cid = client.post("/api/cases",json={"title":"Third party optical laws"}).json()["id"]
    matrix = {"uid":"m","kind":"matrix","name":"Boundary","matrix_t":[1,0,0,1]}
    device = {"uid":"t","kind":"registered","name":"Focus","prescription":{
        "study":lens.name,"version":"1","provider":"independent-lab","parameters":params}}
    end = {"uid":"s","kind":"space","name":"Air","length_mm":20,"clear_aperture":{"shape":"ellipse","width_mm":3}}
    project = {"title":"Laboratory transfer","system":{"mode":"single_pass","wavelength_nm":810,"input_radius_mm":.4,
        "elements":[matrix,device,end]},"observation_planes":[{"uid":"screen","name":"Screen","target_uid":"s","depth_mm":12}]}
    rid = projects.save_project(db,cid,projects.SaveProject(project=project,change_note="Baseline"))["revision_id"]
    assert device_definition(lens,"independent-lab")["scalar_scan_fields"] == ["offset"]
    classic = {"system":project["system"],"element_index":1,"start":0,"stop":2,"count":3}
    with pytest.raises(ValueError):
        scan_system({**classic,"parameter":"parameters.archived"})
    assert len(scan_system({**classic,"parameter":"parameters.offset"})["points"]) == 3
    for port in ["v1/layout","layout-prefix-v1/screen/primary/layout"]:
        base = run_study(db.get_bind(),cid,feed,"independent-lab",{},connections={
            "layout":{"revision_id":rid,"port":port,"registration":"Same bench"}})["study_record"]["id"]
        entries = client.get(f"/api/cases/{cid}/studies/runs/{base}/searches").json()["layout_parameters"]["layout"]
        labels = {tuple(p["path"]):p.get("label") for p in entries}
        mat = ("system","elements",0,"matrix_t")
        dev = ("system","elements",1,"prescription","parameters")
        assert labels[(*mat,1)] == "B / mm" and labels[(*mat,2)] == "C / mm⁻¹"
        knob = (*dev,"levels",0,"v")
        assert knob in labels
        forbidden = [(*dev,"levels",0,"q"), (*dev,"levels",1,"v"), (*dev,"fixed",0), (*dev,"archived")]
        assert all(p not in labels for p in forbidden)
        def submit(fields, values):
            return client.post(f"/api/cases/{cid}/studies/search",json={"run_id":base,"registration":"Same screen and axis",
                "axes":[{"paths":[["layout",*p] for p in fields],"samples":[values]}],"measures":[{"output":"radius","direction":"min"}]}).json()
        for p in forbidden:
            assert submit([p],[9])["unavailable"] == 1
        assert submit([[*mat,0]],[2])["unavailable"] == 1  # The matrix must remain unimodular.
        aperture = ("system","elements",2,"clear_aperture","width_mm")
        assert aperture in labels
        result = submit([[*mat,0],[*mat,3],knob,aperture],[2,.5,5,1.7])
        assert result["feasible"] == 1, result["rows"]
        receipt = db.get(GeneratedContent,result["rows"][0]["run_id"]).content
        elements = projects.read_project(db,cid,receipt["connections"]["layout"]["revision_id"])["project"]["system"]["elements"]
        assert elements[0]["matrix_t"] == [2,0,0,.5] and elements[2]["clear_aperture"]["width_mm"] == 1.7
        assert elements[1]["prescription"]["parameters"] == {**params,"levels":[{"role":"knob","v":5,"q":7},{"role":"reading","v":3}]}


def test_network_law_parameter_changes_power_and_preserves_diagram_and_calibration(db,client,monkeypatch):
    from app.study_api import StrictInput, Study
    from app.workbench.device_laws import device_catalog
    from app.workbench.network import evaluate_network
    from app.workbench.observation import OpticalLayout
    from app.workbench.project_outputs import LAYOUT
    class OpticalInput(StrictInput):
        layout: OpticalLayout
    def analyzer(args):
        port = evaluate_network(args.layout.network,args.layout.network.wavelength_nm)["ports"]
        return {"power_w":next(row["outgoing_w"] for row in port if row["component"]=="plate" and row["port"]=="R-V")}
    method = Study("read_vertical_port","Final analyzer port","1","Passive Jones network",OpticalInput,analyzer,
                   metrics={"power_w":"Analyzer power / W"},input_ports={"layout":LAYOUT})
    monkeypatch.setitem(STUDIES,method.name,(method,"optics-lab"))
    cid = client.post("/api/cases",json={"title":"Polarization setting"}).json()["id"]
    device = device_catalog()[0]["principal_axis_transmission"]
    plate = {"uid":"plate","name":"Retarder","diagram_xy":[12,50],"model":{"kind":"registered","ports":device["ports"],
             "prescription":{**device["prescription"],"parameters":{"angle_deg":45,"second_transmission":1,"delay_nm":500}}}}
    calibrated = {"uid":"second","name":"Calibrated sample","model":{"ports":["out"],"kind":"matrix",
                  "reference_wavelength_nm":1000,"matrix":[[[1,0]]]}}
    network = {"components":[plate,calibrated],"wavelength_nm":1000,"drives":[{"at":{"component":"plate","port":"L-H"},"power_w":.004}]}
    saved = projects.save_project(db,cid,projects.SaveProject(project={"title":"Measurement","system":{
        "mode":"single_pass","elements":[]},"network":network},change_note="Port bench"))
    run = run_study(db.get_bind(),cid,method,"optics-lab",{},connections={"layout":{
        "revision_id":saved["revision_id"],"port":"v1/layout","registration":"Same ports"}})["study_record"]["id"]
    options = client.get(f"/api/cases/{cid}/studies/runs/{run}/searches").json()["layout_parameters"]["layout"]
    fields = {tuple(opt["path"]) for opt in options}
    drive = ("network","components",0,"model","prescription","parameters","delay_nm")
    assert drive in fields and ("network","components",1,"model","matrix",0,0,0) in fields
    assert ("network","components",0,"diagram_xy",0) not in fields
    assert ("network","components",1,"model","reference_wavelength_nm") not in fields
    result = client.post(f"/api/cases/{cid}/studies/search",json={"run_id":run,"registration":"Same ports",
        "axes":[{"paths":[["layout",*drive]],"samples":[[500],[250]]}],"measures":[{"output":"power_w","direction":"max"}]}).json()
    assert [row["values"]["power_w"] for row in result["rows"]] == pytest.approx([.004,.002])

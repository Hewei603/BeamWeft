from dataclasses import replace
from types import SimpleNamespace
import asyncio

import pytest
from pydantic import Field

from app.study_api import Port, StrictInput, Study
from app.workbench.studies import discover_studies
from app.workbench import tools
from app.models import ExperimentCase, GeneratedContent
from app.workbench.contracts import ArtifactQuery
from app.workbench.context import selected_context


class PolarizerInputs(StrictInput):
    input_w: float = Field(ge=0)
    axis_deg: float


def test_live_studies_receive_schematic_positions_unless_the_method_declares_independence():
    from dataclasses import replace
    from app.workbench.spatial_ports import SPATIAL_PORT_STUDY

    assert SPATIAL_PORT_STUDY.presentation("test")["ignores_diagram_positions"] is True
    extension = replace(SPATIAL_PORT_STUDY, name="schematic_inspection", ignores_diagram_positions=False)
    assert "ignores_diagram_positions" not in extension.presentation("test")
    with pytest.raises(ValueError, match="live layout"):
        replace(SPATIAL_PORT_STUDY, live_layout=None)


def test_new_solver_reuses_both_schemas_results_and_history(client, monkeypatch):
    import math
    study = Study("polarizer_test", "偏振片", "1.2", "线偏振光通过理想分析器。", PolarizerInputs,
                  lambda x: {"output_w": x.input_w * math.cos(math.radians(x.axis_deg))**2}, {"output_w": "输出 / W"})
    package = SimpleNamespace(name=study.name, dist=SimpleNamespace(name="test-optics", version="1.0"), load=lambda:study)
    catalog, errors = discover_studies(reserved={"finish"}, plugins=[package])
    assert not errors
    entry = catalog[study.name]
    monkeypatch.setitem(tools.STUDIES, study.name, entry)
    case = client.post("/api/cases", json={"title":"空白研究"}).json()["id"]
    root = f"/api/cases/{case}/studies"
    offered = next(s for s in client.get(root+"/catalog").json()["items"] if s["name"]==study.name)
    assert offered["provider_display_name"] == offered["provider"] == "test-optics 1.0"
    assert offered["inputs"]["properties"]["input_w"]["minimum"]==0
    request = {"version":offered["version"], "provider":offered["provider"], "inputs":{"input_w":8,"axis_deg":60}}
    def post(payload):
        return client.post(root+"/run/"+study.name, json=payload)
    first = post(request)
    assert first.status_code==200, first.text
    out = first.json()
    assert math.isclose(out["output_w"],2)
    repeat = post(request).json()
    assert out["study_record"]["execution"] == "computed" and repeat["study_record"]["execution"] == "reused"
    assert repeat == {**out, "study_record":{**out["study_record"], "execution":"reused"}}
    assert post({**request,"version":"0"}).status_code==409
    assert post({**request,"inputs":{"input_w":-1,"axis_deg":60}}).status_code==422
    assert len(client.get(root+"/runs").json()["items"])==1
    rid = out["study_record"]["id"]
    monkeypatch.delitem(tools.STUDIES, study.name)
    stored = client.get(root+f"/runs/{rid}").json()
    assert stored["definition"]==offered and stored["inputs"]==request["inputs"]
    assert stored["output"]["output_w"]==out["output_w"]
    assert stored["solver_receipt"] == out["study_record"]["solver_receipt"]
    assert stored["installed_solver_receipt"] is None
    assert post(request).status_code==404
    other = client.post("/api/cases", json={"title":"另一个工程"}).json()["id"]
    assert client.get(f"/api/cases/{other}/studies/runs/{rid}").status_code==404
    assert client.get(f"/api/cases/{case}/optical-project").json()["current"] is None


def test_same_advertised_method_version_checks_the_installed_calculation_and_keeps_legacy_data(client, db, monkeypatch):
    case_id = client.post("/api/cases", json={"title":"历史通光量标定"}).json()["id"]
    root = f"/api/cases/{case_id}/studies"
    original = Study("test_transmission", "Transfer", "1", "Synthetic transfer.", PolarizerInputs,
                     lambda p: {"output_w":2 * p.input_w})
    provider = "LaserClaw"
    inputs = {"input_w":3., "axis_deg":0.}

    def install(study):
        monkeypatch.setitem(tools.STUDIES, study.name, (study, provider))

    def calculate(**kwargs):
        payload = {"version":"1", "provider":provider, "inputs":{} if kwargs else inputs, **kwargs}
        response = client.post(root + "/run/" + original.name, json=payload)
        assert response.is_success, response.text
        return response.json()

    def read(rid):
        result = client.get(f"{root}/runs/{rid}")
        assert result.is_success, result.text
        return result.json()

    install(original)
    first = calculate()["study_record"]
    captured = first["solver_receipt"]
    assert captured["complete"] and captured["provider"] == provider
    assert calculate(reuse_inputs_from=first["id"])["study_record"]["execution"] == "reused"
    legacy = GeneratedContent(case_id=case_id, content_type="scientific_study", content={
        "definition":original.describe(provider), "inputs":inputs, "output":{"output_w":5.9}, "connections":{},
    })
    db.add(legacy)
    db.commit()

    install(replace(original, compute=lambda p: {"output_w":3 * p.input_w}))
    new = calculate()
    changed = new["study_record"]
    assert new["output_w"] == 9 and changed["id"] != first["id"]
    assert changed["execution"] == "computed" and changed["solver_receipt"]["sha256"] != captured["sha256"]
    assert changed["solver_receipt"]["version"] == captured["version"] == "1"
    assert calculate()["study_record"]["id"] == changed["id"]
    archived = read(first["id"])
    assert archived["output"]["output_w"] == 6 and archived["solver_receipt"] == captured
    assert archived["installed_solver_receipt"] == changed["solver_receipt"]
    def agent_read(rid, path):
        return asyncio.run(tools.execute("read_artifact", ArtifactQuery(content_id=rid, path=path), db=db,
            case=db.get(ExperimentCase,case_id), task=None, provider=None, session_id=None, attachment_ids=[]))
    via_agent = agent_read(first["id"], "installed_solver_receipt.sha256")
    assert via_agent["content"] == changed["solver_receipt"]["sha256"]
    assert "本次读取时" in via_agent["receipt_sources"]["installed_solver_receipt"]
    copied = calculate(reuse_inputs_from=first["id"])
    assert copied["output_w"] == 9 and copied["study_record"]["input_origin"]["solver_sha256"] == captured["sha256"]
    assert copied["study_record"]["id"] != first["id"]
    old = read(legacy.id)
    assert "solver_receipt" not in old
    assert old["installed_solver_receipt"]["scope"] == "method_only" and not old["installed_solver_receipt"]["complete"]
    unavailable = agent_read(legacy.id, ["solver_receipt", "application", "version"])
    assert unavailable["content"] is None and "原计算执行时" in unavailable["receipt_sources"]["solver_receipt"]
    summary = agent_read(legacy.id, "")
    assert summary["method"]["solver_receipt"] is None and summary["method"]["installed_solver_receipt"]["scope"] == "method_only"
    assert summary["receipt_sources"] == via_agent["receipt_sources"]
    from_legacy = calculate(reuse_inputs_from=legacy.id)["study_record"]
    assert from_legacy["id"] != legacy.id and "solver_sha256" not in from_legacy["input_origin"]
    assert read(legacy.id)["output"] == {"output_w":5.9}

    # The numeric function can stay identical while its published unit or condition changes.
    revised = replace(original, output_ports={"output_w":Port(quantity="power", label="功率", unit="W", scale=1)})
    install(revised)
    declared = calculate()
    assert declared["output_w"] == 6 and declared["study_record"]["id"] != first["id"]
    solver = declared["study_record"]["solver_receipt"]
    assert solver["implementation_sha256"] == captured["implementation_sha256"]
    assert solver["contract_sha256"] != captured["contract_sha256"]

    legacy.content = {key:value for key, value in legacy.content.items() if key != "inputs"}
    db.commit()
    refused = client.post(root + "/run/" + original.name, json={
        "version":"1", "provider":provider, "inputs":{}, "reuse_inputs_from":legacy.id})
    assert refused.status_code == 422 and "缺少可复用的输入参数" in refused.json()["detail"]
    assert read(legacy.id)["output"] == {"output_w":5.9}


def test_read_one_saved_output_preserves_quantity_types_and_case_scope(client, db):
    case_id = client.post("/api/cases", json={"title":"多通道读数"}).json()["id"]
    other = client.post("/api/cases", json={"title":"另一套实验"}).json()["id"]
    scalar = {"quantity":"example.reading", "label":"读数", "unit":"V", "scale":1}
    state_port = {**scalar, "shape":"state", "unit":"example.sensor.v1"}
    unusual = '探头/"A"%'
    state = {"origin": [0, 1], "valid":True, "uncertainty":None}
    record = GeneratedContent(case_id=case_id, content_type="scientific_study", content={
        "available_outputs": {k:state_port for k in ("rows.0.state", "ports.0", unusual)} |
                             {"voltage":scalar, "bad":scalar},
        "output": {"rows":[{"state":state}], "ports":{"0":state}, unusual:state, "voltage":0,
                   "bad":True, "unpublished":[7], "raster":[9]*30000}})
    db.add(record)
    db.commit()
    def read(port, case=case_id):
        return client.get(f"/api/cases/{case}/studies/runs/{record.id}/output", params={"port":port})
    for path in ("rows.0.state", "ports.0", unusual):
        response = read(path)
        assert response.status_code == 200 and response.json()["value"] == state
        assert response.json()["port"]["connects_as"] == ["state"]
        assert len(response.content) < 1024
    assert read("voltage").json()["value"] == 0
    assert read("bad").status_code == 422
    assert read("unpublished").status_code == read("rows.0").status_code == 404
    assert read("voltage", other).status_code == 404


def test_declared_method_upgrade_keeps_bound_readings_and_rejects_changed_defaults(client, db, monkeypatch):
    from app.study_api import Port
    from app.workbench.studies import ConnectedStudy
    case = client.post("/api/cases", json={"title":"跨版本计量"}).json()["id"]
    other = client.post("/api/cases", json={"title":"独立工程"}).json()["id"]
    port = Port(quantity="test.angle",label="角",unit="deg",scale=1)
    source = GeneratedContent(case_id=case,content_type="scientific_study",
        content={"available_outputs":{"angle":port.model_dump()},"output":{"angle":30}})
    db.add(source)
    db.flush()
    connections = {"axis_deg":{"run_id":source.id,"port":"angle"}}
    old_provider, current_provider = "example-optics 1.0", "example-optics 2.0"
    class Meter(StrictInput):
        axis_deg: float
        calibration: float = 1.
    method = Study("meter_test", "Meter", "new", "Synthetic.", Meter, lambda p:{"angle":p.axis_deg*p.calibration},
        input_ports={"axis_deg":port}, accepts_inputs_from=((old_provider,"old"),))
    monkeypatch.setitem(tools.STUDIES, method.name, (method,current_provider))
    def archive(calibration, provider):
        row = GeneratedContent(case_id=case, content_type="scientific_study",
            content={"definition":{"name":method.name,"provider":provider,"version":"old"},
                     "inputs":{"calibration":calibration,"axis_deg":30}, "authored_fields":[],
                     "connections":connections, "output":{"angle":30*calibration}})
        db.add(row)
        db.commit()
        return row.id
    original, different, unclaimed = archive(1.,old_provider), archive(1.5,old_provider), archive(1.,"other")
    request = {"version":"new", "provider":current_provider, "inputs":{}, "reuse_inputs_from":original}
    def submit(body, case_id=case):
        return client.post(f"/api/cases/{case_id}/studies/run/{method.name}",json=body)
    upgraded = submit(request).json()
    assert upgraded["angle"] == 30 and upgraded["study_record"]["connections"] == connections
    assert upgraded["study_record"]["input_origin"] == {"run_id":original,"version":"old","name":method.name,"provider":old_provider}
    agent = asyncio.run(tools.execute("run_connected_study", ConnectedStudy(study=method.name,**request), db=db,
        case=db.get(ExperimentCase,case), task=SimpleNamespace(id=None), provider=None,session_id=None,attachment_ids=[]))
    assert agent["study_record"]["id"] == upgraded["study_record"]["id"]
    assert "calibration" in submit({**request,"reuse_inputs_from":different}).json()["detail"]
    for body in ({**request,"inputs":{"calibration":3.}}, {**request,"connections":connections}, {**request,"reuse_inputs_from":unclaimed}):
        assert submit(body).status_code == 422
    assert submit(request,other).status_code == 422


def test_plugin_collision_and_load_failure_keep_builtin_names():
    def broken():
        raise ImportError("absent dependency")
    plugins = [SimpleNamespace(name=name,load=broken) for name in
               ("fit_power_curve","finish","duplicate","duplicate","unavailable")]
    catalog, issues = discover_studies(reserved={"finish"},plugins=plugins)
    assert catalog["fit_power_curve"][1]=="LaserClaw"
    assert "duplicate" not in catalog and "unavailable" not in catalog and "finish" not in catalog
    assert {i["name"] for i in issues}=={p.name for p in plugins}


def test_installed_extension_groups_share_identical_exports_and_reject_conflicts(tmp_path, monkeypatch):
    import sys
    from types import ModuleType

    module = ModuleType("beamweft_registry_fixture")
    loaded = []

    def resolve(name):
        loaded.append(name)
        return Study(name, name, "1", "Entry point fixture", PolarizerInputs, lambda args: {"w": args.input_w})

    module.__getattr__ = resolve
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.syspath_prepend(str(tmp_path))
    current, legacy = "beamweft.studies.v1", "laserclaw.studies.v1"

    def install(name, groups):
        metadata = tmp_path / (name.replace("-", "_") + "-1.0.dist-info")
        metadata.mkdir()
        (metadata / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
        lines = []
        for group, exports in groups.items():
            lines.append(f"[{group}]")
            lines.extend(f"{tool} = {module.__name__}:{target}" for tool, target in exports)
        (metadata / "entry_points.txt").write_text("\n".join(lines))

    def same(*names):
        return [(name, name) for name in names]

    install("bw-fixture-a", {
        current: same("bw_modern", "bw_dual", "bw_contested", "bw_mismatched"),
        legacy: [*same("bw_older", "bw_dual"), ("bw_mismatched", "bw_other_target")],
    })
    install("bw-fixture-b", {legacy: same("bw_contested")})
    catalog, issues = discover_studies()
    assert sorted(loaded) == ["bw_dual", "bw_modern", "bw_older"]
    assert all(catalog[name][1] == "bw-fixture-a 1.0" for name in loaded)
    assert {issue["name"] for issue in issues if issue["name"].startswith("bw_")} == {"bw_contested", "bw_mismatched"}
    assert not {"bw_contested", "bw_mismatched"} & catalog.keys()
    assert catalog["fit_power_curve"][1] == "LaserClaw"


def test_case_history_remains_addressable_beyond_agent_context_window(client, db):
    case_id = client.post("/api/cases", json={"title": "计算历史"}).json()["id"]
    other_id = client.post("/api/cases", json={"title": "另一个实验"}).json()["id"]
    rows = [GeneratedContent(case_id=case_id, content_type="scientific_study", content={"summary":str(i)}) for i in range(27)]
    db.add_all(rows + [GeneratedContent(case_id=other_id, content_type="scientific_study", content={"summary":"另一案例"})])
    db.commit()
    def read(**kwargs):
        return asyncio.run(tools.execute("read_artifact", ArtifactQuery(**kwargs), db=db, case=db.get(ExperimentCase, case_id),
                                         task=None, provider=None, session_id=None, attachment_ids=[]))
    first = read(content_type="scientific_study")
    assert len(first["items"]) == 25 and first["items"][0]["id"] == rows[-1].id
    last = read(content_type="scientific_study", before_id=first["next_before_id"])
    assert [r["id"] for r in last["items"]] == [rows[1].id, rows[0].id]
    assert read(content_id=rows[0].id, path="summary")["content"] == "0"


def test_selected_study_exposes_paged_exact_output_keys_to_agent(client, db):
    case = db.get(ExperimentCase, client.post("/api/cases", json={"title":"跨方法引用"}).json()["id"])
    ports = {f"rows.{i}.reading": {"label": str(i)} for i in range(31)}
    study = GeneratedContent(case_id=case.id, content_type="scientific_study",
                             content={"definition":{"name":"analyzer","title":"分析结果","version":"2","provider":"LaserClaw"},
                                      "available_outputs":ports, "output":{"raw":[3,4,5]}})
    db.add(study)
    db.commit()
    selected = selected_context(db, case.id, {"artifact_ids":[study.id]})["artifacts"][0]
    assert selected["title"] == "分析结果" and selected["version"] == "2"
    assert selected["provider_display_name"] == "BeamWeft"
    assert selected["read_project_basis"] == {"content_id": study.id, "path": ["project_basis"]}
    def read(params):
        call = tools.execute("read_artifact", ArtifactQuery(**params), db=db, case=case,
                             task=None, provider=None, session_id=None, attachment_ids=[])
        view = asyncio.run(call)
        assert view["method"] == {"title":"分析结果","version":"2","provider_display_name":"BeamWeft"}
        return view["content"]
    first = read(selected["read_outputs"])
    assert first["total"] == len(ports) and len(first["items"]) == 24
    second = read({**selected["read_outputs"], "offset":first["next_offset"]})
    assert first["items"] | second["items"] == ports and second["next_offset"] is None
    assert read({"content_id":study.id,"path":["available_outputs","rows.25.reading","label"]}) == "25"
    assert read({"content_id":study.id,"path":"output.raw", "offset":1,"limit":1})["items"] == [4]
    with pytest.raises(ValueError, match="可用字段"):
        read({"content_id":study.id,"path":"result.output.raw"})


def test_agent_can_read_unused_criteria_and_interpret_a_historical_filter(db):
    case = ExperimentCase(title="Archived observations", goal="", cavity_type="custom")
    db.add(case)
    db.flush()
    base = GeneratedContent(case_id=case.id, content_type="scientific_study", content={
        "definition":{"metrics":{"signal":"Signal", "noise":"Noise"}},
        "output":{"signal":9, "noise":2}})
    db.add(base)
    db.flush()
    record = GeneratedContent(case_id=case.id, content_type="study_search", content={
        "baseline_id":base.id, "recipe":{"measures":[{"output":"signal", "lower":5}]},
        "measures":{"signal":{"label":"Signal", "unit":""}}, "preferred":1,
        "rows":[{"state":"feasible", "preferred":True, "rank":1, "values":{"signal":9}}]})
    db.add(record)
    db.commit()
    references = selected_context(db, case.id, {"artifact_ids":[record.id]})["artifacts"][0]

    def read(address):
        return asyncio.run(tools.execute("read_artifact", ArtifactQuery(**address), db=db, case=case, task=None,
                                         provider=None, session_id=None, attachment_ids=[]))

    options = read(references["read_options"])
    assert set(options["content"]["items"]) == {"signal", "noise"}
    assert read(references["read_criteria"])["content"] == record.content["recipe"]["measures"]
    rows = read(references["read_candidates"])
    assert rows["content"]["items"] == record.content["rows"]
    assert "通过筛选" in rows["interpretation_notes"][0]

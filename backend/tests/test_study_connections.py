"""End-to-end checks on the existing case study entry points."""
import math
from io import BytesIO
from typing import Annotated

from openpyxl import Workbook
import pytest
from pydantic import Field

from app.study_api import Condition, Port, RowOutputs, StrictInput, Study
from app.workbench import tools


def test_frozen_engineering_power_and_length_as_study_inputs(client, bench, monkeypatch):
    root, post, create = bench
    endpoint = root.removesuffix("/studies")+"/optical-project"
    cell = "介质/a %"
    elements = [{"kind":"space","uid":cell,"name":"试样","length_mm":5,"n":1.5}]
    law = {"mode":"bulk","entry":{"low":.8},"exit":{"low":1},"alpha_per_mm":{"low":math.log(2)/5}}
    project = {"title":"单程试样","system":{"mode":"single_pass","wavelength_nm":780,"elements":elements},
        "single_pass_power":{"wavelength_nm":780,"input_w":{"low":4,"high":8},"stages":[{"scope":"host","element_uid":cell,"law":law}]}}
    revision = client.post(endpoint,json={"project":project,"change_note":"透射实验"}).json()["revision_id"]
    out = client.get(root+"/project-outputs",params={"revision_id":revision}).json()["outputs"]
    def ref(name):
        return {"revision_id":revision,"port":next(key for key,ev in out.items() if ev["port"]["quantity"]==name)}
    absorb,length,forward = ref("optics.absorbed-power"),ref("geometry.length"),ref("optics.power")
    inputs = {"heat_fraction":[.2,.25],"radius_mm":1.5,"conductivity":10,"zone_edges_mm":[.5],"relative_density":[1],"basis":"假设，均匀圆斑"}
    connections={"absorbed_w":absorb,"length_mm":length}
    first=post("radial_heat_balance",inputs,connections)
    assert first.status_code==200,first.text
    assert first.json()["heat_w"] == pytest.approx([.32,.8])
    rid=first.json()["study_record"]["id"]
    stored=client.get(root+f"/runs/{rid}").json()
    assert stored["inputs"]["absorbed_w"] == pytest.approx([1.6,3.2])
    assert stored["project_evidence"]["absorbed_w"]["element_uid"] == cell
    assert "alpha_per_mm" in stored["project_evidence"]["absorbed_w"]["stage_law"]
    assert stored["connections"]==connections
    class InMilliwatts(StrictInput):
        power: tuple[float,float]
    downstream=Study("calorimeter_bounds","预测量热仪区间","1","热流 / mW",InMilliwatts,
        lambda args:{"received":list(args.power)},input_ports={"power":Port(quantity="thermal.heat-flow",label="热流",unit="mW",scale=.001,shape="interval")})
    monkeypatch.setitem(tools.STUDIES,downstream.name,(downstream,"independent fixture"))
    transfer=post(downstream.name,{},{"power":{"run_id":rid,"port":"heat_w"}})
    assert transfer.json()["received"] == pytest.approx([320,800])
    assert post("radial_heat_balance",inputs,{**connections,"absorbed_w":forward}).status_code==422
    other=create()
    assert post("radial_heat_balance",inputs,connections,at=other).status_code==422
    assert client.get(other+"/project-outputs",params={"revision_id":revision}).status_code==404
    project["single_pass_power"]["stages"][0]["law"]={"mode":"net","retained":{"low":.1}}
    newer=client.post(endpoint,json={"base_revision_id":revision,"project":project,"change_note":"只知透过率"}).json()["revision_id"]
    new_out=client.get(root+"/project-outputs",params={"revision_id":newer}).json()["outputs"]
    assert not any(ev["port"]["quantity"]=="optics.absorbed-power" for ev in new_out.values())
    assert post("radial_heat_balance",inputs,connections).json()["study_record"]["id"]==rid
    assert post("radial_heat_balance",inputs,{**connections,"absorbed_w":{**absorb,"revision_id":newer}}).status_code==422
    project["single_pass_power"]["stages"][0]["law"]=law
    project["single_pass_power"]["input_w"]=None
    unscaled=client.post(endpoint,json={"base_revision_id":newer,"project":project,"change_note":"源面功率未知"}).json()["revision_id"]
    unscaled_outputs=client.get(root+"/project-outputs",params={"revision_id":unscaled}).json()["outputs"]
    assert not any(ev["port"]["unit"]=="W" for ev in unscaled_outputs.values())
    project["single_pass_power"]["input_w"] = {"low": 4}
    fixed = client.post(endpoint, json={"base_revision_id": unscaled, "project": project,
                                        "change_note": "核对旧工程引用"}).json()["revision_id"]
    amounts = client.get(root + "/project-outputs", params={"revision_id":fixed}).json()["outputs"]
    ports = [k for k, v in amounts.items() if v["port"]["quantity"] == "optics.absorbed-power"]
    assert len(ports) == 1
    original_key = ports[0]
    assert amounts[original_key]["port"]["connects_as"] == ["interval", "scalar"]
    old_ref = {"revision_id": fixed, "port": original_key + "/value"}
    assert post("radial_heat_balance", inputs, {**connections, "absorbed_w": old_ref}).json()["heat_w"] == pytest.approx([.32,.4])
    assert post("radial_heat_balance", inputs, {**connections, "absorbed_w":
                {"revision_id":revision, "port":original_key + "/value"}}).status_code == 422


@pytest.fixture
def bench(client):
    def create():
        cid = client.post("/api/cases", json={"title": "空白光路"}).json()["id"]
        return f"/api/cases/{cid}/studies"
    root = create()

    def post(name, inputs, connections=None, at=root):
        study, provider = tools.STUDIES[name]
        return client.post(f"{at}/run/{name}", json={"version": study.version, "provider": provider,
                           "inputs": inputs, "connections": connections or {}})
    return root, post, create


def test_emission_table_registration_keeps_corresponding_original_rows(client, bench):
    root, post, _ = bench
    case = root.removesuffix("/studies")
    raw = b"T_um,S_um,dx_dz,dy_dz,p_mW\n50,80,.01,-.02,7\n90,75,.02,NA,35\n-30,40,-.03,.04,3\n"
    fid = client.post(case+"/attachments", files={"file": ("emission.csv", raw)}).json()["id"]
    view = client.post(case+"/measurement-view", json={"attachment_id": fid, "x_column": 1, "y_column": 2}).json()
    fields = [("x_mm", "um"), ("y_mm", "um"), ("direction_x", "1"), ("direction_y", "1"), ("power_w", "mW")]
    bindings = {key: {"table": view["selection"], "column": i, "unit": unit, "sha256": view["source"]["sha256"]}
                for i, (key, unit) in enumerate(fields, start=1)}
    inputs = {"plane": "launch", "basis": "original forward slopes and mW", "medium_n": 1.0,
              "directions": "slopes", "weighting": "radiant_power"}
    assert post("assemble_ray_bundle", inputs, bindings).status_code == 422
    for column in bindings.values():
        column["omit_incomplete_rows"] = True
    response = post("assemble_ray_bundle", inputs, bindings)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["launched_power_w"] == pytest.approx(.010)
    assert result["bundle"]["rays"][0] == pytest.approx([.05, .08, .01, -.02, .7])
    assert result["bundle"]["rays"][1] == pytest.approx([-.03, .04, -.03, .04, .3])
    record = client.get(root+"/runs/"+str(result["study_record"]["id"])).json()
    evidence, = record["file_evidence"]
    assert evidence["row_numbers"] == [2, 4] and evidence["omitted_rows"] == [3]
    assert evidence["source"]["sha256"] == view["source"]["sha256"]


@pytest.mark.parametrize("workbook", [False, True])
def test_label_and_physical_series_bind_original_paired_rows(client, bench, monkeypatch, workbook):
    root, post, _ = bench
    case = root.removesuffix("/studies")
    class Samples(StrictInput):
        signal: list[float]
        labels: list[Annotated[str, Field(max_length=16)]] | None = Field(None, max_length=4, title="试样号")
    power = Port(quantity="sample.power", label="功率", unit="W", scale=1, alternate_units={"mW":.001}, shape="series")
    def identify(args):
        return {"rows":[{"source_index":i, "label":label, "signal":value}
                        for i, (label,value) in enumerate(zip(args.labels,args.signal),1)]}
    study = Study("read_samples", "按样本整理功率", "1", "样本原件", Samples,
                  identify, table=("rows", {"label":"样本号","signal":"功率"}), table_input_rows={"labels":"source_index"},
                  input_ports={"signal":power},
                  paired_inputs=(("signal","labels"),))
    monkeypatch.setitem(tools.STUDIES, study.name, (study,"independent instrument"))
    rows = [["id","power"],["0001",120],[" ",20],["  a,b; c  ",35],["bad reading",None],["0000",40]]
    if workbook:
        book = Workbook()
        for row in rows:
            book.active.append(row)
        stream = BytesIO()
        book.save(stream)
        filename, raw = "bench.xlsx", stream.getvalue()
    else:
        filename = "bench.csv"
        raw = 'id,power\n0001,120\n ,20\n"  a,b; c  ",35\nbad reading,\n0000,40\n'.encode()
    file = client.post(case+"/attachments", files={"file":(filename,raw)}).json()["id"]
    plan = client.get(root+"/catalog/"+study.name).json()
    assert plan["text_column_inputs"] == {"labels":{"label":"试样号","max_items":4,"max_length":16}}
    assert set(plan["input_ports"]) == {"signal"}
    assert plan["table_input_rows"] == {"labels":"source_index"}
    view = client.post(case+"/measurement-view",json={"attachment_id":file,"x_column":None,
        "y_column":2,"first_row":2,"delimiter":"comma","decimal_mark":"."}).json()
    table = {"table":view["selection"],"sha256":view["source"]["sha256"],"omit_incomplete_rows":True}
    binding = {"labels":{**table,"column":1},"signal":{**table,"column":2,"unit":"mW"}}
    result = post(study.name,{},binding)
    assert result.is_success, result.text
    assert [(r["label"],r["signal"]) for r in result.json()["rows"]] == [("0001",.12),("  a,b; c  ",.035),("0000",.04)]
    rid = result.json()["study_record"]["id"]
    stored = client.get(root+f"/runs/{rid}").json()
    receipt, = stored["file_evidence"]
    assert receipt["fields"] == ["labels","signal"] and receipt["source"]["sha256"] == table["sha256"]
    assert receipt["row_numbers"] == [2,4,6] and receipt["omitted_rows"] == [3,5]
    assert "unit" not in stored["connections"]["labels"] and stored["inputs"]["labels"] == ["0001","  a,b; c  ","0000"]
    missing = post(study.name,{},{k:{**v,"omit_incomplete_rows":False} for k,v in binding.items()})
    assert missing.status_code == 422 and "第 3 条、第 1 列缺少非空文本" in missing.text
    for extra in ({"unit":"1"}, {"calibration":{"gain":1, "note":"baseline"}}, {"condition_note":"假定"}):
        invalid = post(study.name,{}, {**binding, "labels":{**binding["labels"], **extra}})
        assert invalid.status_code == 422 and "移除物理单位" in invalid.text
    assert post(study.name,{}, {**binding,"labels":{"run_id":rid,"port":"signal"}}).status_code == 422


def test_label_only_method_honours_its_own_schema_and_original_record_window(client, bench, monkeypatch):
    root, post, _ = bench
    case = root.removesuffix("/studies")
    class Labels(StrictInput):
        names: list[Annotated[str, Field(max_length=768)]] = Field(max_length=4000)
    study = Study("count_samples","样本登记","1","原始文本",Labels,lambda a:{"n":len(a.names), "last":a.names[-1]})
    monkeypatch.setitem(tools.STUDIES,study.name,(study,"instrument"))
    long_id = "x"*740
    raw = ("\n".join(["name"]+["A"]*3010+[long_id])+"\n").encode()
    file = client.post(case+"/attachments",files={"file":("labels.txt",raw)}).json()["id"]
    view = client.post(case+"/measurement-view",json={"attachment_id":file,"delimiter":"single","first_row":2}).json()
    assert view["selection"]["y_column"] is None and view["scanned"] == 3011 and view["last_scanned"] == 3012
    result = post(study.name,{},{"names":{"table":view["selection"],"sha256":view["source"]["sha256"],"column":1}})
    assert result.is_success, result.text
    assert result.json()["n"] == 3011 and result.json()["last"] == long_id
    too_long = client.post(case+"/attachments",files={"file":("long.csv",b"id\n"+b"x"*769)}).json()["id"]
    bad_view = client.post(case+"/measurement-view",json={"attachment_id":too_long,"first_row":2}).json()
    invalid = post(study.name,{},{"names":{"table":bad_view["selection"],"sha256":bad_view["source"]["sha256"],"column":1}})
    assert invalid.status_code == 422 and "超过 768 个字符" in invalid.text
    book = Workbook()
    sheet = book.active
    sheet.append(["names"])
    sheet["A2"] = 1
    sheet["A2"].number_format = "0000"
    out = BytesIO()
    book.save(out)
    file = client.post(case+"/attachments",files={"file":("numbers.xlsx",out.getvalue())}).json()["id"]
    view = client.post(case+"/measurement-view",json={"attachment_id":file,"first_row":2}).json()
    as_text = {"table":view["selection"],"sha256":view["source"]["sha256"],"column":1,"omit_incomplete_rows":True}
    rejection = post(study.name,{},{"names":as_text})
    assert rejection.status_code == 422 and "前导零" in rejection.text


def test_saved_fit_with_manual_labels_replays_and_mixed_origins_keep_registration(client, bench, db):
    from app.models import GeneratedContent
    from app.workbench.studies import recorded_study_inputs
    root, post, _ = bench
    case = root.removesuffix("/studies")
    def original(filename, raw):
        file = client.post(case+"/attachments",files={"file":(filename,raw.encode())}).json()["id"]
        view = client.post(case+"/measurement-view",json={"attachment_id":file,"first_row":2,"decimal_mark":".",
                                                          "delimiter":"comma"}).json()
        return {"table":view["selection"],"sha256":view["source"]["sha256"]}
    table = original("scan.csv","z,r\n"+"\n".join(f"{z},{math.hypot(.1,.003*z)}" for z in range(-50,51,10)))
    ids = [f"frame-{i}" for i in range(11)]
    columns = {"z_mm":{**table,"column":1,"unit":"mm"},"radius_mm":{**table,"column":2,"unit":"mm"}}
    authored = {"data_source":"已标定半径与同向导轨，旧工程按行登记帧号","holdout_positions_mm":[-30,20]}
    historical = post("fit_beam_caustic",{**authored,"acquisition_ids":ids},columns)
    assert historical.is_success, historical.text
    saved = historical.json()
    study, provider = tools.STUDIES["fit_beam_caustic"]
    # An installed v5 recipe is replayed with its authored fields and frozen connections.
    # The cross-version reuse API is reserved for explicitly declared predecessor versions.
    old = db.get(GeneratedContent,saved["study_record"]["id"])
    definition = dict(old.content["definition"], paired_inputs=[["z_mm","radius_mm"]])
    definition.pop("text_column_inputs")
    old.content = {**old.content, "definition":definition}
    db.commit()
    archived = client.get(root+f"/runs/{old.id}").json()
    replay = client.post(root+"/run/fit_beam_caustic",json={"version":study.version,"provider":provider,
        "inputs":recorded_study_inputs(archived),"connections":archived["connections"]})
    assert replay.is_success, replay.text
    assert replay.json()["holdout"] == saved["holdout"]
    other = original("labels.csv","id\n"+"\n".join(ids))
    refs = {**columns,"acquisition_ids":{**other,"column":1}}
    bad = post(study.name, authored, refs)
    assert bad.status_code == 422 and "不同来源" in bad.text
    combined = post(study.name,authored,{k:{**v,"registration":"两份采集按相同的相机触发次序排列"} for k,v in refs.items()})
    assert combined.is_success and combined.json()["holdout"] == saved["holdout"]
    record = client.get(root+f"/runs/{combined.json()['study_record']['id']}").json()
    attached = next(e for e in record["file_evidence"] if "acquisition_ids" in e["fields"])
    index = record["definition"]["table_input_rows"]["acquisition_ids"]
    assert [attached["row_numbers"][p[index]-1] for p in combined.json()["holdout"]["points"]] == [4,9]
    invalid_numeric = post(study.name,{**authored,"radius_mm":[.2]*11},{"z_mm":columns["z_mm"]})
    assert invalid_numeric.status_code == 422 and "不同来源" in invalid_numeric.text


def test_shared_state_registration_is_frozen_and_reconfirmed_for_new_sources(client, bench, monkeypatch):
    class Signal(StrictInput):
        value: float

    class Paired(StrictInput):
        left: Signal
        right: Signal
        setup: str

    state = Port(quantity="fixture.reading", unit="fixture", shape="state", label="传感器读数", scale=1)
    capture = Study("capture_fixture", "采集", "1", "test", Signal,
                    lambda args: {"signal":args.model_dump()}, output_ports={"signal":state})
    compare = Study("paired_fixture", "比较", "1", "test", Paired,
                    lambda args: {"difference":args.left.value-args.right.value, "setup":args.setup},
                    input_ports={"left":state,"right":state}, registration_inputs={"left":"setup","right":"setup"})
    for study in (capture,compare):
        monkeypatch.setitem(tools.STUDIES,study.name,(study,"fixture"))
    root, post, _ = bench
    def source(value):
        return {"run_id":post(capture.name,{"value":value}).json()["study_record"]["id"], "port":"signal"}
    first, second, new = [source(v) for v in (1,2,3)]
    raw, links = {"setup":"共用水平仪及读数时间"}, {"left":first,"right":{**second,"registration":"第二个传感器轴反向"}}
    result = post(compare.name,raw,links)
    assert result.status_code == 200,result.text
    initial = result.json()["study_record"]
    assert initial["connections"]["left"] == {**first,"registration":raw["setup"],"registration_from":"setup"}
    assert initial["connections"]["right"] == links["right"]
    edited = post(compare.name,{"setup":"新定位"},initial["connections"]).json()["study_record"]["connections"]
    assert edited["left"]["registration"] == "新定位" and edited["right"] == links["right"]
    assert post(compare.name,raw,{**links,"left":{**first,"registration_from":"unknown"}}).status_code == 422
    assert post(compare.name,{"setup":" "},links).status_code == 422
    pair = post(compare.name,raw,{"left":first,"right":second}).json()["study_record"]
    req = {"run_id":pair["id"],"source_id":first["run_id"],"replacement_id":new["run_id"]}
    preview = client.post(root+"/replay",json=req).json()
    assert set(preview["steps"][-1]["registrations"]) == {"setup"}
    assert client.post(root+"/replay",json={**req,"execute":True}).json()["result_id"] is None
    report = client.post(root+"/replay",json={**req,"execute":True,"registrations":{str(pair["id"]):{"setup":"重测后对准"}}}).json()
    stored = client.get(root+"/runs/"+str(report["result_id"])).json()
    assert stored["inputs"]["setup"] == "重测后对准" and stored["output"]["difference"] == 1
    assert all(ref["registration"] == "重测后对准" and ref["registration_from"] == "setup" for ref in stored["connections"].values())
    req["run_id"] = initial["id"]
    report = client.post(root+"/replay",json={**req,"execute":True,"registrations":{str(initial["id"]):{"setup":"重测后对准"}}}).json()
    assert report["result_id"] is None and "先前单独登记" in report["steps"][-1]["message"]


def test_geometry_ports_remain_usable_without_a_beam_profile(client, bench):
    root, post, _ = bench
    result = post("analyze_optical_system", {"mode": "single_pass", "wavelength_nm": 532,
                  "elements": [{"kind":"lens", "name":"透镜", "focal_mm":80},
                               {"kind":"space", "name":"像方空气", "length_mm":10}]}).json()
    record = client.get(f"{root}/runs/{result['study_record']['id']}").json()
    assert record["output"]["status"] == "partial"
    for axis in ("tangential","sagittal"):
        key = f"planes.{axis}.collimated_focus_from_exit_mm"
        assert record["output"]["planes"][axis]["collimated_focus_from_exit_mm"] == pytest.approx(70)
        assert record["available_outputs"][key]["unit"] == "mm"
    assert "exit_field" not in record["available_outputs"]


def test_replay_only_dependent_steps_and_keep_physical_sources(client, bench, monkeypatch):
    root, post, create = bench
    project = {"title":"红外基频", "system":{"mode":"single_pass","wavelength_nm":1064,
               "elements":[{"kind":"space","uid":"air","name":"空气","length_mm":80}]}}
    endpoint = root.removesuffix("/studies") + "/optical-project"
    old = client.post(endpoint,json={"project":project,"change_note":"起始"}).json()["revision_id"]
    project["system"]["wavelength_nm"] = 1030
    new = client.post(endpoint,json={"project":project,"base_revision_id":old,"change_note":"换光源"}).json()["revision_id"]
    wave = {"revision_id":old,"port":"v1/primary/wavelength"}
    def calculation(args, links):
        response = post("compute_phase_match",args,links)
        assert response.status_code == 200, response.text
        return response.json()["study_record"]["id"]
    def output(rid):
        return {"run_id":rid,"port":"matched_wavelength_nm"}
    def read(rid):
        return client.get(root+f"/runs/{rid}").json()
    first = calculation({"crystal":"bbo"},{"lambda1_nm":wave})
    independent = calculation({"crystal":"bbo","lambda1_nm":1600},{})
    last = calculation({"crystal":"bbo"},{"lambda1_nm":output(first),"lambda2_nm":output(independent)})
    data = {"run_id":last,"source_id":old,"replacement_id":new}
    before = read(last)
    preview = client.post(root+"/replay",json=data).json()
    assert {s["id"]:s["action"] for s in preview["steps"]} == {first:"recalculate",independent:"reuse",last:"recalculate"}
    for step in preview["steps"]:
        if step["action"] == "recalculate":
            assert step["saved_solver_receipt"] == step["installed_solver_receipt"] == read(step["id"])["solver_receipt"]
    assert client.get(root+"/runs").json()["items"][0]["id"] == last
    report = client.post(root+"/replay",json={**data,"execute":True}).json()
    assert report["status"] == "completed",report
    for step in report["steps"]:
        if step["action"] == "computed":
            assert step["execution"] == "computed" and step["result_solver_receipt"] == read(step["result_id"])["solver_receipt"]
    final = read(report["result_id"])
    assert final["output"]["matched_wavelength_nm"] == pytest.approx(1/(1/515+1/800))
    assert final["connections"]["lambda2_nm"] == output(independent)
    assert read(final["connections"]["lambda1_nm"]["run_id"])["connections"]["lambda1_nm"] == {**wave,"revision_id":new}
    assert read(last) == before
    repeat = client.post(root+"/replay",json={**data,"execute":True}).json()
    assert repeat["result_id"] == report["result_id"]
    assert {step["execution"] for step in repeat["steps"] if step["action"] == "computed"} == {"reused"}
    assert client.post(create()+"/replay",json=data).status_code == 422
    assert client.post(root+"/replay",json={**data,"replacement_id":last}).status_code == 422
    monkeypatch.delitem(tools.STUDIES,"compute_phase_match")
    missing = client.post(root+"/replay",json={**data,"execute":True}).json()
    assert missing["result_id"] is None
    assert [r["action"] for r in missing["steps"] if r["id"] in (first,last)] == ["unavailable","waiting"]


def test_replay_follows_stable_row_identity_across_reordering_and_rejects_ambiguous_rows(client, bench, monkeypatch):
    root, post, _ = bench
    class Channels(StrictInput):
        channels: list[str]
        readings: list[float]
    class Readout(StrictInput):
        value: float
    voltage = Port(quantity="instrument.voltage",label="电压",unit="V",scale=1)
    sampling = Study("channel_bank","仪器通道","1","合成仪器",Channels,
                     lambda a:{"rows":[{"id":uid,"label":"探头","v":v} for uid,v in zip(a.channels,a.readings)]},
                     row_outputs={"rows":RowOutputs(columns={"v":voltage},coordinates={"label":"探头"},
                                                   identity_fields=("id",))})
    reader = Study("read_channel","示数","1","读一个通道",Readout,lambda a:{"value":a.value},
                   {"value":"电压 / V"},input_ports={"value":voltage})
    for method in (sampling,reader):
        monkeypatch.setitem(tools.STUDIES,method.name,(method,"独立插件"))
    def sample(names,values):
        return post(sampling.name,{"channels":names,"readings":values}).json()["study_record"]["id"]
    old = sample(["A","B"],[3,3])
    new = sample(["B","A"],[14,28])
    target = post(reader.name,{},{"value":{"run_id":old,"port":"rows.1.v"}}).json()["study_record"]["id"]
    def execute(replacement):
        return client.post(root+"/replay",json={"run_id":target,"source_id":old,
                           "replacement_id":replacement,"execute":True}).json()
    report = execute(new)
    assert report["comparison"] == [{"label":"电压 / V","previous":3,"current":14}]
    assert report["steps"][-1]["row_matches"] == [{"input":"value","label":"电压","coordinates":{"id":"B"},"old_row":2,"new_row":1}]
    record = client.get(root+f"/runs/{report['result_id']}").json()
    assert record["connections"]["value"] == {"run_id":new,"port":"rows.0.v"}
    for replacement in (sample(["B","B"],[14,21]),sample(["A"],[5])):
        report = execute(replacement)
        assert report["result_id"] is None and report["steps"][-1]["action"] == "unavailable"
        assert "缺失或重复" in report["steps"][-1]["message"]


def test_replay_recovers_partial_work_without_reusing_an_old_frame_note(client, bench, monkeypatch):
    root, post, _ = bench
    from app.workbench.project_outputs import WAVELENGTH
    class AtWavelength(StrictInput):
        wavelength: float
    class Calibrated(StrictInput):
        reading: dict
    state = Port(quantity="calibration.state",label="标定",unit="calibration.v1",shape="state",scale=1,
                 registration_label="校准时的参考面")
    calibration = Study("reference_state","参考面标定","1","合成标定",AtWavelength,
        lambda a:{"state":{"wavelength":a.wavelength}},input_ports={"wavelength":WAVELENGTH},output_ports={"state":state})
    readout = Study("apply_reference_state","读取标定","1","合成读数",Calibrated,
        lambda a:{"reading":a.reading["wavelength"]},input_ports={"reading":state})
    for method in (calibration,readout):
        monkeypatch.setitem(tools.STUDIES,method.name,(method,"laboratory"))
    source = {"title":"标定波长","system":{"mode":"single_pass","wavelength_nm":633,
               "elements":[{"kind":"space","name":"标定段","length_mm":2}]}}
    endpoint = root.removesuffix("/studies")+"/optical-project"
    old = client.post(endpoint,json={"project":source,"change_note":"标定"}).json()["revision_id"]
    source["system"]["wavelength_nm"] = 640
    new = client.post(endpoint,json={"base_revision_id":old,"project":source,"change_note":"更换"}).json()["revision_id"]
    middle = post(calibration.name,{},{"wavelength":{"revision_id":old,"port":"v1/primary/wavelength"}}).json()["study_record"]["id"]
    final = post(readout.name,{},{"reading":{"run_id":middle,"port":"state","registration":"原装置的坐标说明"}}).json()["study_record"]["id"]
    request = dict(run_id=final,source_id=old,replacement_id=new,execute=True)
    partial = client.post(root+"/replay",json=request).json()
    assert partial["status"]=="partial" and partial["result_id"] is None
    first = partial["steps"][0]["result_id"]
    assert first and partial["steps"][-1]["action"]=="unavailable"
    assert partial["steps"][-1]["registrations"]["reading"]["previous"]=="原装置的坐标说明"
    corrected = client.post(root+"/replay",json={**request,"registrations":{final:{"reading":"已核对新装置的参考面"}}}).json()
    assert corrected["status"]=="completed" and corrected["steps"][0]["result_id"]==first
    result = client.get(root+f"/runs/{corrected['result_id']}").json()
    assert result["output"]["reading"]==640 and result["connections"]["reading"]["registration"]=="已核对新装置的参考面"
    assert client.post(root+"/replay",json={**request,"registrations":{final:{"wrong_field":"错配"}}}).status_code==422


@pytest.mark.parametrize("use_rays", [False, True])
def test_project_gain_chain_matches_wavelength_and_geometry_through_two_saved_calculations(client, bench, use_rays):
    root, post, _ = bench
    model = {"title":"交叉来源核对", "system":{"mode":"single_pass", "wavelength_nm":1064, "input_radius_mm":.15,
             "elements":[{"name":"介质","uid":"rod","kind":"space","n":1.5,"length_mm":5}]}, "incident_paths":[]}
    for uid, nm in (("pA",808),("pB",808),("pC",940)):
        model["incident_paths"].append({"uid":uid,"name":uid,"wavelength_nm":nm,"target_uid":"rod","target_n":1.6,
            "tangential":{"radius_mm":.09,"m2":1,"wavefront":"waist"}, "power_budget":{"wavelength_nm":nm,"input_w":{"low":4},
            "stages":[{"scope":"host","element_uid":"rod","law":{"mode":"bulk","entry":{"low":1},"exit":{"low":1},
            "alpha_per_mm":{"low":math.log(2)/5}}}]}})
    endpoint = root.removesuffix("/studies")+"/optical-project"
    revision = client.post(endpoint,json={"project":model,"change_note":"三个泵浦输入"}).json()["revision_id"]
    ports = client.get(root+"/project-outputs",params={"revision_id":revision}).json()["outputs"]
    def project_field(quantity, **fields):
        return {"revision_id":revision,"port":next(k for k,v in ports.items() if v["port"]["quantity"]==quantity
                and all(v.get(key)==value for key,value in fields.items()))}
    layout = project_field("optics.ordered-layout.v1")
    connections = {"layout":{**layout,"registration":"按本版展开的 T/S 轴"}}
    if use_rays:
        source = post("assemble_ray_bundle", {"wavelength_nm":808,"medium_n":1,"plane":"发射面",
            "basis":"合成的两条带权入射光线","quality":"documented","weighting":"equal_flux",
            "x_mm":[-.02,.02],"y_mm":[0,0],"directions":"slopes","direction_x":[0,0],"direction_y":[0,0]}).json()
        connections["pump_bundle"] = {"run_id":source["study_record"]["id"],"port":"bundle",
                                       "registration":"pA 发射面，沿工程正 T/S 轴，权重按发射面功率。"}
    area = post("absorbed_mode_area", {"pump_uid":"pA"} if use_rays else {}, connections).json()
    assert len(area["rows"]) == (1 if use_rays else 3)
    if use_rays:
        assert area["rows"][0]["positive_ray_count"] == 2 and area["rows"][0]["pump_uid"] == "pA"
    ref = {"run_id":area["study_record"]["id"],"port":"rows.0.area_mm2"}
    material = {"pump_nm":808,"signal_nm":1064,"emission_cm2":2e-19,"pump_yield":.8,"lifetime_us":200,
                "max_absorbed_w":5, "basis":"受激发射与寿命取合成样品值"}
    wrong = post("four_level_modal_gain",{**material,"signal_nm":532},{"area_mm2":ref})
    assert wrong.status_code==422 and "1064" in wrong.text
    gain = post("four_level_modal_gain",material,{"area_mm2":ref}).json()["study_record"]
    loop = {"signal_nm":1064,"survival":[.8,.8],"traversals":2,"basis":"假设等效返回，另计镜面损耗"}
    curve = {"run_id":gain["id"],"port":"curve","registration":"同一信号模式及试样"}
    def evaluate(uid, **fields):
        watt = project_field("optics.absorbed-power",path_uid=uid)
        return post("cw_roundtrip_balance",loop,{"curve":curve,"operating_w":{**watt,**fields}})
    assert evaluate("pA").json()["operating_state"]=="above"
    wrong = evaluate("pB")
    assert wrong.status_code==422 and "样品段" in wrong.text
    registered = evaluate("pB",registration="独立探查对称支路，假设相同模式和归一化沉积剖面")
    assert registered.status_code==200 and "跨工况" in registered.json()["warnings"][0]
    wrong = evaluate("pC",registration="不同泵浦波长")
    assert wrong.status_code==422 and "940" in wrong.text and "808" in wrong.text


def test_external_packages_can_exchange_conditioned_values_without_builtin_identifiers(bench, monkeypatch):
    _, post, _ = bench
    class Calibrated(StrictInput):
        fixture: str
        kelvin: float
        factor: float
    class Interpret(StrictInput):
        mount: str
        temperature_k: float
        scale_factor: float
    port = Port(quantity="lab.instrument-calibration", label="标定倍率", unit="1", scale=1)
    def tagged(fixture, temperature):
        return port.model_copy(update={"conditions":{
            "fixture.id":Condition(path=fixture,label="安装座",value_type="text"),
            "lab.temperature.K":Condition(path=temperature,label="温度 / K")}})
    a = Study("calibration_source","仪器标定","1","独立包 A",Calibrated,lambda a:a.model_dump(),
              output_ports={"factor":tagged("fixture","kelvin")})
    b = Study("calibration_reader","读数换算","1","独立包 B",Interpret,lambda b:{"reading":b.scale_factor},
              input_ports={"scale_factor":tagged("mount","temperature_k")})
    for study in (a,b):
        monkeypatch.setitem(tools.STUDIES,study.name,(study,"第三方"))
    out = post(a.name,{"fixture":"ABC-2","kelvin":290,"factor":.95}).json()
    ref = {"scale_factor":{"port":"factor","run_id":out["study_record"]["id"]}}
    assert post(b.name,{"mount":"ABC-2","temperature_k":290},ref).json()["reading"]==.95
    for mount, temp, mismatch in (("ABC-1",290,"安装座"),("ABC-2",298,"温度")):
        wrong = post(b.name,{"mount":mount,"temperature_k":temp},ref)
        assert wrong.status_code==422 and mismatch in wrong.text


def test_cut_and_rejoin_preserves_both_complex_planes_and_frequency(client, bench):
    root, post, create = bench
    phase = post("compute_phase_match", {"crystal": "bbo", "lambda1_nm": 1064}).json()
    wavelength = {"run_id": phase["study_record"]["id"], "port": "matched_wavelength_nm"}
    front = [{"kind": "space", "name": "距离", "length_mm": 23},
             {"kind": "matrix", "name": "无名称推断", "matrix_t": [1, 0, -.014, 1], "matrix_s": [1, 0, -.008, 1]}]
    back = [{"kind": "space", "name": "介质", "n": 1.8, "length_mm": 27},
            {"kind": "lens", "name": "L", "focal_mm": 90}, {"kind": "space", "name": "空气", "length_mm": 19}]
    name = "analyze_optical_system"
    source = {"mode": "single_pass", "input_radius_mm": .4, "input_curvature_mm": -120, "beam_quality_m2": 1.4}
    first = post(name, {**source, "elements": front}, {"wavelength_nm": wavelength}).json()
    field = {"run_id": first["study_record"]["id"], "port": "exit_field", "registration": "两段间无空隙，沿传播方向 T/S 轴重合"}
    recipe = {"wavelength_nm": wavelength, "input_field": field}
    next_inputs = {"mode": "single_pass", "elements": back}
    res = post(name, next_inputs, recipe)
    assert res.status_code == 200, res.text
    second = res.json()
    whole = post(name, {**source, "wavelength_nm": 532, "elements": front + back}).json()
    assert second["exit_field"]["tangential"] != second["exit_field"]["sagittal"]
    for axis in ("tangential", "sagittal"):
        for key in ("q_real_mm", "q_imag_mm", "radius_mm", "curvature_mm"):
            assert second["planes"][axis]["output"][key] == pytest.approx(whole["planes"][axis]["output"][key], rel=1e-12)
    assert second["exit_field"]["beam_quality_m2"] == 1.4
    stored_id = second["study_record"]["id"]
    stored = client.get(root+f"/runs/{stored_id}").json()
    assert stored["connections"] == recipe
    assert stored["inputs"]["input_field"] == first["exit_field"]
    assert post(name, next_inputs, recipe).json()["study_record"]["id"] == stored_id
    assert post(name, {**next_inputs, "wavelength_nm": 532}, recipe).status_code == 422
    assert post(name, {**next_inputs, "wavelength_nm": 633}, {"input_field": field}).status_code == 422
    assert post(name, next_inputs, {**recipe, "input_field": {**field, "registration": ""}}).status_code == 422
    assert post(name, next_inputs, recipe, at=create()).status_code == 422
    cavity = {"mode":"standing_wave", "wavelength_nm":532, "elements":[{"name":"a","kind":"mirror"},
              {"kind":"space","name":"b","length_mm":10},{"name":"c","kind":"mirror","radius_mm":100}]}
    result = post(name, cavity).json()
    assert result["stable_both_planes"] and "exit_field" not in result
    assert post(name, cavity, {"input_field":field}).status_code == 422


def test_contact_optics_can_be_split_at_a_reference_plane(bench):
    from app.workbench.optics import analyze_system

    _, post, _ = bench
    name = "analyze_optical_system"
    source = {"mode": "single_pass", "wavelength_nm": 780, "input_radius_mm": 0.4}
    first = {"kind": "lens", "name": "A", "focal_mm": 60}
    second = {"kind": "lens", "name": "B", "focal_mm": -90}
    combined = analyze_system({**source, "elements": [first, second]})
    step = post(name, {**source, "elements": [first]}).json()
    field = {"run_id": step["study_record"]["id"], "port": "exit_field", "registration": "两元件共面同轴"}
    chained = post(name, {"mode": "single_pass", "wavelength_nm": 780, "elements": [second]}, {"input_field": field})
    assert chained.is_success, chained.text
    for result in (combined, chained.json()):
        for plane in result["planes"].values():
            assert plane["output"]["radius_mm"] == pytest.approx(0.4)
            assert plane["output"]["curvature_mm"] == pytest.approx(-180)
            assert plane["output"]["z_mm"] == 0


def test_registered_beam_preserves_independent_widths_pointing_and_inherited_wavelength(bench, client):
    root, post, create = bench
    method = "analyze_optical_system"
    measured = {"study_id": 7, "measured_axis": "Y", "registration": "camera Y follows beam S",
                "coordinate_mm": 12, "wavelength_nm": 780, "radius_mm": .41, "curvature_mm": None,
                "width_basis": "second_moment", "m2": 2.1}
    source = {"tangential": {"radius_mm": .27, "wavefront": "curved", "curvature_mm": -140, "m2": 1},
              "sagittal": {"radius_mm": .41, "wavefront": "waist", "m2": 2.1, "second_moment_radius": True,
                           "fit_origin": measured},
              "launch_ray": {"t_offset_mm": .16, "t_angle_mrad": -2, "s_offset_mm": -.19, "s_angle_mrad": 1.5}}
    face = {"kind": "lens", "name": "front", "focal_mm": 70}
    drift = {"kind": "space", "name": "flat slab", "length_mm": 60, "n": 1.5}
    setup = {"mode": "single_pass", "source": source, "elements": [face], "wavelength_nm": 780}
    upstream = post(method, setup).json()
    first = upstream["study_record"]["id"]
    assert "exit_field" not in upstream and "exit_beam" in upstream["study_record"]["available_outputs"]
    note = "coincident air faces, same T/S axes and transverse origin"
    link = {"input_beam": {"run_id": first, "port": "exit_beam", "registration": note}}
    downstream = {"mode": "single_pass", "elements": [drift]}
    referenced = post(method, downstream, link)
    assert referenced.status_code == 200, referenced.text
    result = referenced.json()
    saved = result["study_record"]["id"]
    fixed = post(method, {**downstream, "wavelength_nm": 780}, link).json()["study_record"]["id"]
    assert fixed != saved
    assert result["exit_beam"]["sagittal"]["fit_origin"]["coordinate_mm"] == 12
    assert result["exit_beam"]["sagittal"]["second_moment_radius"] is True
    assert result["exit_beam"]["tangential"]["second_moment_radius"] is False
    for key, prefix in (("tangential", "t"), ("sagittal", "s")):
        axis = source[key]
        a, b, c, d = 1 - 40 / 70, 40, -1 / 70, 1
        w, inv_r = axis["radius_mm"], 1 / axis["curvature_mm"] if axis.get("curvature_mm") else 0
        projected = w * math.hypot(a + b * inv_r, b * 780e-6 * axis["m2"] / (math.pi * w*w))
        assert result["exit_beam"][key]["radius_mm"] == pytest.approx(projected, rel=1e-12)
        assert result["planes"][key]["output"]["radius_mm"] == pytest.approx(projected, rel=1e-12)
        x = source["launch_ray"][prefix + "_offset_mm"]
        p = source["launch_ray"][prefix + "_angle_mrad"] / 1000
        assert result["exit_beam"]["launch_ray"][prefix + "_offset_mm"] == pytest.approx(a*x + b*p)
        assert result["exit_beam"]["launch_ray"][prefix + "_angle_mrad"] == pytest.approx(1000 * (c*x + d*p))
    assert post(method, {**downstream, "wavelength_nm": 1064}, link).status_code == 422
    assert post(method, {**downstream, "input_radius_mm": .1}, link).status_code == 422
    assert post(method, downstream, {"input_beam": {**link["input_beam"], "registration": ""}}).status_code == 422
    assert post(method, downstream, link, at=create()).status_code == 422
    component = {"model_revision_id": 1, "version": 2, "instance_uid": "calibration", "ordinal": 1, "name": "library lens",
                 "wavelength_nm": 632.8}
    calibration = post(method, {**setup, "elements": [{**face, "component_origin": component}]}).json()
    upstream_note, = calibration["exit_beam"]["model_warnings"]
    assert "632.8" in upstream_note and "780" in upstream_note
    conditional = post(method, downstream, {"input_beam": {**link["input_beam"], "run_id": calibration["study_record"]["id"]}}).json()
    assert upstream_note in conditional["model_warnings"] and conditional["exit_beam"]["model_warnings"] == [upstream_note]
    clipped = post(method, {**setup, "elements": [{**face, "clear_aperture": {"shape": "ellipse", "width_mm": .2}}]})
    assert clipped.status_code == 200 and "exit_beam" not in clipped.json()
    target = "planes.tangential.output.radius_mm"
    assert target not in clipped.json()["study_record"]["available_outputs"]
    grid = client.post(root+"/search", json={"run_id": first, "axes": [
        {"paths": [["source", "tangential", "m2"]], "samples": [[1], [2]]}], "measures": [{"output": target, "direction": "min"}]})
    assert grid.is_success, grid.text
    rows = grid.json()["rows"]
    assert rows[0]["state"] == "feasible" and rows[1]["state"] == "unavailable" and "束宽口径" in rows[1]["message"]
    alt = post(method, {**setup, "wavelength_nm": 1064}).json()["study_record"]["id"]
    def replay(run):
        return client.post(root+"/replay", json={"run_id": run, "source_id": first, "replacement_id": alt, "execute": True,
            "registrations": {str(run): {"input_beam": note}}}).json()
    followed = replay(saved)
    assert followed["status"] == "completed", followed
    restored = client.get(root+f"/runs/{followed['result_id']}").json()
    assert restored["inputs"]["wavelength_nm"] == 1064 and restored["connections"]["input_beam"]["run_id"] == alt
    assert "wavelength_nm" not in restored["authored_fields"]
    pinned = replay(fixed)
    assert pinned["result_id"] is None and "同一真空波长" in pinned["steps"][-1]["message"]


def test_incomplete_axis_reappears_at_conjugate_plane_through_a_registered_handoff(bench, client):
    root, post, _ = bench
    method = "analyze_optical_system"
    lens = {"kind": "lens", "focal_mm": 20, "name": "relay"}
    gap = {"kind": "space", "length_mm": 40, "name": "twice focal length"}
    source = {"tangential": {"radius_mm": .4, "m2": 1, "wavefront": "waist"},
              "sagittal": {"radius_mm": .61, "m2": 1, "wavefront": "unknown"},
              "launch_ray": {"s_offset_mm": -.175, "s_angle_mrad": None}}
    setup = {"mode": "single_pass", "source": source, "wavelength_nm": 1030}
    upstream = post(method, {**setup, "elements": [gap, lens]}).json()
    middle = upstream["exit_beam"]
    assert upstream["status"] == "partial" and "exit_beam" in upstream["study_record"]["available_outputs"]
    assert middle["sagittal"]["radius_mm"] is None and middle["launch_ray"]["s_offset_mm"] is None
    assert middle["launch_ray"]["s_angle_mrad"] is None and "sagittal" in middle["origins"]
    assert "planes.tangential.output.radius_mm" in upstream["study_record"]["available_outputs"]
    assert "planes.sagittal.output.radius_mm" not in upstream["study_record"]["available_outputs"]
    reference = {"input_beam": {"run_id": upstream["study_record"]["id"], "port": "exit_beam",
                                "registration": "common unfolded forward frame"}}
    plan = {"mode": "single_pass", "elements": [gap]}
    joined = post(method, plan, reference).json()
    direct = post(method, {**setup, "elements": [gap, lens, gap]}).json()
    for result in (direct, joined):
        beam = result["exit_beam"]
        assert beam["sagittal"]["radius_mm"] == pytest.approx(.61)
        assert beam["sagittal"]["wavefront"] == "unknown" and beam["launch_ray"]["s_angle_mrad"] is None
        assert beam["launch_ray"]["s_offset_mm"] == pytest.approx(.175)
        assert beam["tangential"]["radius_mm"] == pytest.approx(.4)
        face = result["planes"]["sagittal"]["output"]
        assert face["radius_mm"] == pytest.approx(.61) and "q_real_mm" not in face
        assert "planes.sagittal.output.radius_mm" in result["study_record"]["available_outputs"]
    assert direct["planes"]["sagittal"]["profile_breaks"] == [1]
    assert [pt["z_mm"] for pt in direct["planes"]["sagittal"]["samples"]] == [0, 80]
    preview = client.post(root.removesuffix("/studies")+"/optical-project/preview", json={**setup, "elements": [gap, lens, gap]})
    assert preview.status_code == 200
    partial = preview.json()["planes"]["sagittal"]
    assert partial["profile_breaks"] == [1] and partial["profile"] == [[0, .61], [80, .61]]
    assert partial["radii_mm"] == [None, None, .61]
    inconsistent = {**middle, "sagittal": {**middle["sagittal"], "radius_mm": .2}}
    assert post(method, {**plan, "input_beam": inconsistent}).status_code == 422
    assert post(method, {**plan, "input_beam": {**inconsistent, "origins": {}}}).status_code == 200
    for matrix in ([[0, 0], [0, 0]], [[1, 0], [0, -1]], [[2, 0], [0, 1]], [[1e15, 1e15], [1e15, 1e15]]):
        broken = {**middle["origins"]["sagittal"], "matrix": matrix}
        response = post(method, {**plan, "input_beam": {**middle, "origins": {"sagittal": broken}}})
        assert response.status_code == 422 and "ABCD" in response.text
    unknown = {"tangential": {}, "launch_ray": dict.fromkeys(("t_offset_mm", "t_angle_mrad", "s_offset_mm", "s_angle_mrad"))}
    empty = post(method, {**setup, "source": unknown, "elements": [gap]})
    assert empty.status_code == 200 and "exit_beam" not in empty.json()


def test_independent_voltage_plugins_share_values_without_discarding_bounds(bench, client, db, monkeypatch):
    root, post, _ = bench

    class Bounds(StrictInput):
        reading: tuple[float, float]

    class Reading(StrictInput):
        reading: float

    class Samples(StrictInput):
        reading: list[float]

    def install(name, schema, shape, unit, scale):
        port = Port(quantity="electrical.voltage", label="电压", unit=unit, scale=scale, shape=shape)
        study = Study(name, name, "1", "保留本次电压读数", schema, lambda args: {"value": args.reading},
                      input_ports={"reading": port}, output_ports={"value": port})
        monkeypatch.setitem(tools.STUDIES, name, (study, name + " lab plugin"))
        return name

    bounds = install("voltmeter_limits", Bounds, "interval", "V", 1)
    reading = install("meter_readout", Reading, "scalar", "mV", .001)
    samples = install("digitizer_samples", Samples, "series", "V", 1)

    def send(source, destination):
        previous = source["study_record"]["id"]
        return post(destination, {}, {"reading": {"run_id": previous, "port": "value"}})

    for fixed in (0., -1.75):
        source = post(bounds, {"reading": [fixed, fixed]}).json()
        measured = send(source, reading).json()
        assert measured["value"] == pytest.approx(1000 * fixed)
        assert send(measured, bounds).json()["value"] == pytest.approx([fixed, fixed])
        history = client.get(root + "/runs").json()["items"]
        original = next(item for item in history if item["id"] == source["study_record"]["id"])
        assert original["available_outputs"]["value"]["connects_as"] == ["interval", "scalar"]
    interval = post(bounds, {"reading": [1., math.nextafter(1., 2.)]}).json()
    assert send(interval, reading).status_code == 422
    assert send(interval, samples).status_code == 422
    pair = post(samples, {"reading": [2., 2.]}).json()
    assert send(pair, bounds).status_code == 422

    from app.models import GeneratedContent
    old = db.get(GeneratedContent, source["study_record"]["id"])
    old_ports = {key: {k: v for k, v in declaration.items() if k != "connects_as"}
                 for key, declaration in old.content["available_outputs"].items()}
    old.content = {**old.content, "available_outputs": old_ports}
    db.commit()
    history = client.get(root + "/runs").json()["items"]
    restored = next(item for item in history if item["id"] == old.id)
    assert restored["available_outputs"]["value"]["connects_as"] == ["interval", "scalar"]
    assert send(source, reading).json()["value"] == pytest.approx(-1750)


def test_plugin_quantities_conversion_provenance_and_unavailable_results(bench, monkeypatch):
    _, post, _ = bench

    class Sample(StrictInput):
        points: list[float]
        label: str = Field(min_length=1)
        status: str = "completed"

    def make(name, kind, unit, scale):
        port = Port(quantity=kind, label=kind, unit=unit, scale=scale, shape="series")
        study = Study(name, name, "1", "序列转换", Sample, lambda x: {"values": x.points, "status": x.status},
                      input_ports={"points": port}, output_ports={"values": port})
        monkeypatch.setitem(tools.STUDIES, name, (study, "independent"))
    make("series_a", "optics.power", "mW", .001)
    make("series_b", "optics.power", "W", 1)
    make("series_c", "optics.vacuum-wavelength", "nm", 1e-9)

    def emit(label, status="completed"):
        result = post("series_a", {"points":[500, 2000], "label":label, "status":status}).json()
        return {"points":{"run_id":result["study_record"]["id"], "port":"values"}}
    first, other, partial = emit("测量 A"), emit("测量 B"), emit("不完整", "partial")
    def use(ref, name="series_b"):
        return post(name, {"label":"接收"}, ref)
    result = use(first).json()
    assert result["values"] == [.5, 2] and math.isfinite(sum(result["values"]))
    assert use(first).json()["study_record"]["id"] == result["study_record"]["id"]
    assert use(other).json()["study_record"]["id"] != result["study_record"]["id"]
    assert use(partial).status_code == use(first,"series_c").status_code == 422
    assert use({"points":{**first["points"], "port":"status"}}).status_code == 422
    monkeypatch.delitem(tools.STUDIES, "series_a")
    assert use(first).json() == {**result, "study_record":{**result["study_record"], "execution":"reused"}}


def test_row_ports_remain_addressable_after_reordering_and_provider_removal(bench, client, monkeypatch):
    root, post, other = bench

    class Acquisition(StrictInput):
        channels: list[str]
        values: list[tuple[float, float] | None]
        status: str = "completed"

    class Display(StrictInput):
        value: float

    voltage = Port(quantity="electrical.voltage", label="电压范围", unit="V", scale=1, shape="interval")
    instrument = Study("scope_windows", "示波器读数", "1", "各通道的电压范围", Acquisition,
                       lambda a: {"status": a.status, "scan": {"samples": tuple(
                           {"channel": c, "volts": v} for c, v in zip(a.channels, a.values, strict=True))}},
                       row_outputs={"scan.samples": RowOutputs(columns={"volts": voltage}, coordinates={"channel": "通道"})})
    meter = Study("display_millivolts", "毫伏表", "1", "单值读数", Display, lambda a: {"reading": a.value},
                  input_ports={"value": Port(quantity="electrical.voltage", label="电压", unit="mV", scale=.001)})
    for study in (instrument, meter):
        monkeypatch.setitem(tools.STUDIES, study.name, (study, "independent bench extension"))
    args = {"channels": ["相同/名称.α", "相同/名称.α", "断开"], "values": [[.25, .25], [1, 2], None]}
    result = post(instrument.name, args).json()["study_record"]
    first, second = "scan.samples.0.volts", "scan.samples.1.volts"
    offers = result["available_outputs"]
    assert set(offers) == {first, second}
    assert offers[first]["label"] != offers[second]["label"] and args["channels"][0] in offers[first]["label"]
    assert "scalar" in offers[first]["connects_as"] and "scalar" not in offers[second]["connects_as"]
    assert post(instrument.name, {**args, "status": "partial"}).json()["study_record"]["available_outputs"] == {}
    reordered = post(instrument.name, {**args, "values": list(reversed(args["values"]))}).json()["study_record"]
    assert first not in reordered["available_outputs"]
    monkeypatch.delitem(tools.STUDIES, instrument.name)
    assert client.get(root + f"/runs/{result['id']}").json()["available_outputs"] == offers
    def use(key, case=root):
        return post(meter.name, {}, {"value": {"run_id": result["id"], "port": key}}, at=case)
    assert use(first).json()["reading"] == pytest.approx(250)
    assert use(first, other()).status_code == use(second).status_code == use("scan.samples.2.volts").status_code == 422


def test_partial_network_publishes_only_independently_solved_ports(bench):
    _, post, _ = bench
    components = [{"uid": name, "name": name, "model": {"kind": "coupler", "through_fraction": .25}}
                  for name in ("free/name.1", "unpowered")]
    args = {"wavelength_nm": 640, "components": components, "drives": [
        {"at": {"component": components[0]["uid"], "port": "1"}, "power_w": 4}]}
    result = post("solve_port_network", args).json()
    assert result["status"] == "partial"
    available = result["study_record"]["available_outputs"]
    assert len(available) == 4
    for i, row in enumerate(result["ports"]):
        assert row["component"] == components[0]["uid"]
        assert row["name"] in available[f"ports.{i}.outgoing_w"]["label"]
    args["drives"] = []
    assert post("solve_port_network", args).json()["study_record"]["available_outputs"] == {}


def test_discovery_from_a_frozen_quantity_preserves_roles_scope_and_history(bench, client, monkeypatch):
    root, post, other_case = bench
    phase = post("compute_phase_match", {"crystal":"bbo", "lambda1_nm":1064}).json()
    reference = {"run_id":phase["study_record"]["id"], "port":"matched_wavelength_nm"}

    def search(source, *, case=root, **filters):
        return client.post(case.removesuffix("/studies") + "/capabilities/search",
                           json={"source":source, "limit":20, **filters})

    page = search(reference, query="compute_phase_match", limit=1).json()
    assert page["connection_action"] == "run_connected_study"
    hit, = page["items"]
    assert hit["name"] == "compute_phase_match"
    assert set(hit["matching_inputs"]) == {"lambda1_nm", "lambda2_nm"}
    assert search(reference, accepts_quantity="optics.power").json()["total"] == 0
    assert search(reference, case=other_case()).status_code == 422
    assert search({**reference, "port":"solutions.0.theta_deg"}).status_code == 422
    assert search(reference, sources=[{"quantity":"optics.power","unit":"W","scale":1,"connects_as":["scalar"]}]).status_code == 422

    # Removing a method must not remove its archived values from the workflow.
    monkeypatch.delitem(tools.STUDIES, "compute_phase_match")
    method = "analyze_optical_system"
    hit, = search(reference, query=method, limit=1).json()["items"]
    assert list(hit["matching_inputs"]) == ["wavelength_nm"]
    connected = post(method, {"mode":"single_pass", "input_radius_mm":.5,
                              "elements":[{"kind":"space","name":"air","length_mm":10}]},
                     {"wavelength_nm":reference})
    assert connected.status_code == 200
    assert connected.json()["wavelength_nm"] == pytest.approx(532)


def test_partial_fit_publishes_supported_geometry_and_never_exposes_missing_m2(bench):
    _, post, _ = bench
    positions = [25, -60, 0, 60, -25]
    widths = [math.hypot(.1, .01*z) for z in positions]
    args = {"radius_mm":widths,"z_mm":positions,"data_source":"second moment, same homogeneous rail","medium_n":None}
    result = post("fit_beam_caustic",args).json()
    assert result["status"] == "partial" and result["m2"] is None
    assert result["output_assessments"]["m2"]["missing_inputs"] == ["wavelength_nm","medium_n"]
    assert result["data"]["z_mm"] == positions and result["data"]["curve_z_mm"] == sorted(result["data"]["curve_z_mm"])
    ports = result["study_record"]["available_outputs"]
    assert "m2" not in ports and "geometry" in ports
    binding = {"run_id":result["study_record"]["id"],"port":"geometry","registration":"same rail and direction"}
    projected = post("predict_caustic_widths",{"positions_mm":[5,150]}, {"geometry":binding}).json()
    assert projected["status"] == "partial" and projected["points"][1]["extrapolated"]
    assert set(projected["study_record"]["available_outputs"]) == {"points.0.radius_mm"}
    assert projected["points"][0]["radius_mm"] == pytest.approx(math.hypot(.1,.05))
    assert post("predict_caustic_widths",{"positions_mm":[0]}, {"geometry":{**binding,"port":"m2"}}).status_code == 422
    assert post("predict_caustic_widths",{"positions_mm":[0]}, {"geometry":{**binding,"registration":""}}).status_code == 422
    for settings in ({"wavelength_nm":0},{"medium_n":-1}):
        assert post("fit_beam_caustic",{**args,**settings}).status_code == 422
    known = post("fit_beam_caustic",{**args,"medium_n":1.5,"wavelength_nm":800}).json()
    assert known["m2"] == pytest.approx(math.pi*1.5*.1*.01/.0008)
    weak_z = [50,60,70,80,90,100]
    weak = post("fit_beam_caustic",{**args,"z_mm":weak_z,"radius_mm":[math.hypot(.1,.01*z) for z in weak_z]}).json()
    assert weak["waist_z_mm"] == pytest.approx(0,abs=1e-6) and weak["output_assessments"]["geometry"]["state"]=="limited"
    assert weak["study_record"]["available_outputs"] == {}


def test_fit_repeatability_is_carried_to_an_in_range_prediction(bench):
    _, post, _ = bench
    z = [-90,-45,-25,0,25,45,90]
    result = post("fit_beam_caustic",{
        "z_mm":[at for at in z for _ in range(2)],
        "radius_mm":[math.hypot(.08,.004*at)*scale for at in z for scale in (.8,1.2)],
        "wavelength_nm":635,"medium_n":1,"data_source":"D4sigma, repeated captures at each rail position",
    }).json()
    assert result["status"] == "partial"
    assert result["output_assessments"]["geometry"]["state"] == "available"
    reference = {"run_id":result["study_record"]["id"],"port":"geometry","registration":"same axial origin and direction"}
    predicted = post("predict_caustic_widths",{"positions_mm":[0]}, {"geometry":reference}).json()
    assert predicted["status"] == "partial" and predicted["warnings"] == result["prediction_warnings"]
    assert not predicted["points"][0]["extrapolated"] and predicted["radius_mm"] == pytest.approx([.08])


def test_grouped_validation_keeps_held_measurements_out_of_downstream_geometry(bench):
    _, post, _ = bench
    z = [-135, -50, -20, 0, 20, 50, 135, 160, 175, 175]
    nominal = [math.hypot(.15, .003 * at) for at in z]
    observed = nominal.copy()
    observed[-3:] = [nominal[-3] + .006, nominal[-2] + .003, nominal[-1] - .003]
    inputs = {"z_mm": z, "radius_mm": observed, "acquisition_ids": [str(i) for i in range(7)] + ["paired", "paired", "repeat"],
              "holdout_positions_mm": [160], "data_source": "fixed X, second-moment radii", "wavelength_nm": 1030, "medium_n": 1}
    response = post("fit_beam_caustic", inputs)
    assert response.status_code == 200, response.text
    result = response.json()
    validation = result["holdout"]
    assert validation["positions_mm"] == [160,175]
    assert [point["input_index"] for point in validation["points"]] == [8,9,10]
    assert [p["predicted_width"] for p in validation["points"]] == pytest.approx(nominal[-3:])
    assert all(p["extrapolated"] for p in validation["points"])
    assert validation["station_rms_radius_mm"] == pytest.approx(math.hypot(.006,.003) / math.sqrt(2))
    assert result["geometry"]["sampled_range_mm"] == [-135,135]
    assert result["data"]["z_mm"] == z[:-3]
    assert result["geometry"]["waist_radius_mm"] == pytest.approx(.15)
    ref = {"run_id":result["study_record"]["id"], "port":"geometry", "registration":"original X and z axes"}
    forecast = post("predict_caustic_widths", {"positions_mm":[175]}, {"geometry":ref})
    assert forecast.status_code == 200, forecast.text
    continued = forecast.json()
    assert continued["points"][0]["extrapolated"] and continued["radius_mm"] == pytest.approx([nominal[-1]])
    assert post("fit_beam_caustic", {**inputs, "radius_mm":[*observed[:-1], -1]}).status_code == 422
    assert post("fit_beam_caustic", {**inputs, "acquisition_ids":["a"]}).status_code == 422


def test_single_station_keeps_repeatability_without_offering_beam_geometry(bench, client):
    root, post, _ = bench
    result = post("fit_beam_caustic", {
        "z_mm": [0, 0], "radius_mm": [.064, .096], "wavelength_nm": 635,
        "medium_n": 1, "data_source": "two independent captures at the same plane",
    }).json()
    assert result["status"] == "partial"
    stats = result["repeatability"]
    assert stats["pooled_within_sd_mm"] == pytest.approx(math.sqrt(2) * .016)
    assert stats["degrees_of_freedom"] == 1 and stats["station_mean_fit_rms_mm"] is None
    assert stats["within_fraction_of_radius_variation"] is None
    assert "geometry" not in result and "m2" not in result
    record = client.get(f"{root}/runs/{result['study_record']['id']}").json()
    assert record["available_outputs"] == {}
    assert record["output"]["data"] == {"z_mm": [0, 0], "radius_mm": [.064, .096]}
    reference = {"run_id": record["id"], "port":"geometry", "registration":"same plane"}
    assert post("predict_caustic_widths", {"positions_mm":[10]}, {"geometry":reference}).status_code == 422


def test_partial_heat_uses_independent_boundary_and_bulk_measurements(bench):
    _,post,_ = bench
    args={"absorbed_w":[4,4],"heat_fraction":[.25,.25],"basis":"conditional, steady-state test"}
    heat=post("radial_heat_balance",args).json()
    assert heat["heat_w"] == [1,1] and set(heat["study_record"]["available_outputs"])=={"heat_w"}
    wall=post("radial_heat_balance",{**args,"radius_mm":2,"length_mm":10,"side_conductance":2000}).json()
    assert wall["surface_to_sink_k"] == pytest.approx([1/(2*math.pi*.002*.010*2000)]*2)
    assert set(wall["study_record"]["available_outputs"]) == {"surface_to_sink_k","heat_w"}
    assert wall["output_assessments"]["axis_to_surface_k"]["missing_inputs"]==["conductivity","zone_edges_mm","relative_density"]


def test_provider_with_an_unjustified_output_creates_no_record_and_can_be_retried(bench,client,monkeypatch):
    root, post, _ = bench
    class LabReading(StrictInput):
        signal: float
        responsivity: float | None = None
    def compute(args):
        return {"power":args.signal / (args.responsivity or 1)}
    plugin=Study("lab_photometry","实验室光度计","4","已校准线性范围",LabReading,compute,
                 output_ports={"power":Port(quantity="optics.power",label="功率",unit="W",scale=1)},
                 output_requirements={"power":("signal","responsivity")})
    monkeypatch.setitem(tools.STUDIES,plugin.name,(plugin,"custom-lab"))
    failure=post(plugin.name,{"signal":2})
    assert failure.status_code==500 and "custom-lab" in failure.text and "responsivity" in failure.text
    assert client.get(root+"/runs").json()["items"]==[]
    retry=post(plugin.name,{"signal":2,"responsivity":.5}).json()
    assert retry["power"]==4 and retry["output_assessments"]["power"]["state"]=="available"


def test_scientific_inputs_read_whole_original_rows_and_freeze_mapping(bench, client):
    root, post, other_case = bench
    case = root.removesuffix("/studies")
    data = ["sequence;pump_mW;out_uW"]
    for i in range(2100):
        pump = i % 20
        out = "OL" if i == 1050 else str(max(0, pump-5)*250000)
        data.append(f"{i};{pump*1000};{out}")
    file = client.post(case+"/attachments", files={"file":("test.log", "\n".join(data).encode())}).json()["id"]
    view = client.post(case+"/measurement-view", json={"attachment_id": file, "x_column":1, "y_column":2,
                                                       "x_lower":25, "x_upper":2050}).json()
    assert view["sampled"] and view["valid"] > len(view["points"])
    def col(c, unit):
        return {"table":view["selection"], "sha256":view["source"]["sha256"], "column":c, "unit":unit}
    a, b = col(2, "mW"), col(3, "uW")
    sources = {"pump_w":a, "output_w":b}
    args = {"pump_basis":"incident", "data_source":"合成记录，原件以微瓦记录输出。"}
    def run(at=root):
        return post("fit_power_curve", args, sources, at=at)
    rejected = run()
    assert rejected.status_code == 422 and "1052" in rejected.text
    assert not client.get(root+"/runs").json()["items"]
    a["omit_incomplete_rows"] = True
    assert run().status_code == 422
    b["omit_incomplete_rows"] = True
    response = run()
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["threshold_pump"] == pytest.approx(5) and result["slope_efficiency_pct"] == pytest.approx(25)
    rid = result["study_record"]["id"]
    record = client.get(root+f"/runs/{rid}").json()
    evidence, = record["file_evidence"]
    expected_rows = [i+2 for i in range(25,2051) if i != 1050]
    assert evidence["row_numbers"] == expected_rows and evidence["omitted_rows"] == [1052]
    assert evidence["outside"] == 74 and evidence["source"]["sha256"] == view["source"]["sha256"]
    assert record["inputs"]["pump_w"] == [(row-2)%20 for row in expected_rows]
    assert len(record["output"]["data"]["output_w"]) > len(view["points"])
    assert run().json()["study_record"]["id"] == rid
    assert run(other_case()).status_code == 422
    b["calibration"] = {"gain": .5, "offset":0, "note":"独立校准记录"}
    assert run().json()["slope_efficiency_pct"] == pytest.approx(12.5)
    b["unit"] = "nm"
    assert run().status_code == 422
    b["unit"], b["sha256"] = "uW", "0"*64
    assert run().status_code == 422


def test_independent_table_selections_require_explicit_pairing_and_preserve_sheet(bench, client):
    from io import BytesIO
    from openpyxl import Workbook
    root, post, _ = bench
    case = root.removesuffix("/studies")
    workbook = Workbook()
    workbook.active.title = "Notes"
    sheet = workbook.create_sheet("Rail")
    sheet.append(["position/cm", "radius/µm"])
    for z in range(-3, 4):
        sheet.append([z, 180*math.hypot(1,z/2)])
    buffer = BytesIO()
    workbook.save(buffer)
    fid = client.post(case+"/attachments", files={"file":("rail.xlsx", buffer.getvalue())}).json()["id"]
    view = client.post(case+"/measurement-view", json={"attachment_id":fid,"sheet":"Rail"}).json()
    def col(c,unit):
        return {"sha256":view["source"]["sha256"],"table":view["selection"],"column":c,"unit":unit}
    z, w = col(1,"cm"), col(2,"µm")
    args = {"wavelength_nm":1000,"data_source":"合成，高斯半径"}
    def run(sources, inputs=args):
        return post("fit_beam_caustic",inputs,sources)
    fit=run({"z_mm":z,"radius_mm":w}).json()
    assert fit["waist_radius_mm"] == pytest.approx(.18)
    assert fit["study_record"]["file_evidence"][0]["rows_used"] == 7
    partial={"z_mm":z}
    manual={**args,"radius_mm":[.18*math.hypot(1,n/2) for n in range(-3,4)]}
    response=run(partial,manual)
    assert response.status_code==422 and "对应" in response.text
    z["registration"] = "手填值依照 Rail 工作表第 2 至 8 行的顺序"
    assert run(partial,manual).json()["waist_radius_mm"] == pytest.approx(.18)


def test_external_study_can_read_arbitrary_original_columns_without_preview_axis_coupling(bench, client, monkeypatch):
    root, post, _ = bench
    case = root.removesuffix("/studies")

    class Signals(StrictInput):
        a: list[float]
        b: list[float]
        c: list[float]

    voltage = Port(quantity="electrical.voltage", label="电压", unit="V", scale=1, shape="series", alternate_units={"mV":.001})
    definition = Study("mixed_signals", "多通道采集", "1", "同一采集时刻的电压和", Signals,
                       lambda s: {"sum":[sum(row) for row in zip(s.a, s.b, s.c, strict=True)]},
                       input_ports={k:voltage for k in ("a","b","c")}, paired_inputs=(("a","b","c"),))
    monkeypatch.setitem(tools.STUDIES, definition.name,(definition,"external lab"))
    original=b"counter,unused,a,b,c\n3,off,1000,2000,3000\n2,0,2000,3000,4000\n2,0,3000,4000,5000\n1,0,4000,5000,6000\n"
    fid=client.post(case+"/attachments",files={"file":("scope.csv",original)}).json()["id"]
    view=client.post(case+"/measurement-view",json={"attachment_id":fid,"first_row":2,"x_column":1,"y_column":2,"x_lower":2}).json()
    sources={k:{"table":{**view["selection"],"y_column":j}, "column":c,"unit":"mV","sha256":view["source"]["sha256"]}
             for k,j,c in (("a",2,3),("b",3,4),("c",3,5))}
    response=post(definition.name,{},sources)
    assert response.status_code==200,response.text
    result=response.json()
    assert result["sum"]==[6,9,12]
    record=client.get(root+"/runs/"+str(result["study_record"]["id"])).json()
    group,=record["file_evidence"]
    assert group["row_numbers"]==[2,3,4] and group["outside"]==1 and group["omitted_rows"]==[]

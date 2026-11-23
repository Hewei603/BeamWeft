from app.study_api import Port, RowOutputs, StrictInput, Study
from app.workbench import tools
from app.workbench.discovery import BrowserSearch, CapabilityDirectory, CapabilitySearch, SourceSignature
from app.workbench.studies import _assess_outputs, _available
import pytest


def test_external_method_cannot_publish_a_number_without_its_declared_evidence():
    class Calibration(StrictInput):
        reading: float
        gain: float | None = None
    flux = Port(quantity="lab.flux",label="标定通量",unit="W",scale=1)
    cal = Study("calibrate","通量标定","1","实验室测量",Calibration,lambda _: {},
                output_ports={"flux":flux}, output_requirements={"flux":("reading","gain")})
    values={"reading":1,"gain":None}
    assert _assess_outputs(cal,{},values)["flux"]["state"] == "needs_input"
    with pytest.raises(ValueError,match="给出了数值"):
        _assess_outputs(cal,{"flux":7},values)
    with pytest.raises(ValueError,match="没有返回"):
        _assess_outputs(cal,{},{"reading":1,"gain":2})
    assert _assess_outputs(cal,{"flux":0},{"reading":0,"gain":2})["flux"]["state"]=="available"


@pytest.mark.parametrize("status", ["completed", "partial"])
def test_row_quality_filter_requires_an_explicit_boolean_from_the_method(status):
    volts = Port(quantity="lab.voltage",label="探头电压",unit="V",scale=1)
    readings = RowOutputs(columns={"v":volts}, available_on_partial=True, where={"quality.clipped":False})
    method = Study("range_readings","示波记录","1","各通道波形取样",Inputs,lambda _: {},row_outputs={"samples":readings})
    rows = [{"v":1,"quality":{"clipped":flag}} for flag in (True,False,0,"false",None)]
    assert set(_available(method,{"status":status,"samples":[*rows,{"v":2}]})) == {"samples.1.v"}


class Inputs(StrictInput):
    value: float


def test_large_directory_is_pageable_and_rules_are_independent_of_names():
    studies = {}
    for i in range(205):
        name = f"method_{i:03}"
        method = Study(name, f"光度计 {i:03}", "7", "手动定义标定范围。",
                       Inputs, lambda p: {"flux":p.value}, keywords=("radiometer",))
        studies[name] = (method,"lab 7")
    registry = {name:(s.inputs,s.scope) for name,(s,_) in studies.items()}
    directory = CapabilityDirectory(registry,studies,{})
    first = directory.search(CapabilitySearch(query="ＲＡＤＩＯＭＥＴＥＲ",limit=20))
    assert (len(first["items"]),first["total"],first["next_offset"]) == (20,205,20)
    assert all("inputs" not in row for row in first["items"])
    seen = [row["name"] for row in first["items"]]
    offset = first["next_offset"]
    while offset is not None:
        page = directory.search(CapabilitySearch(query="radiometer",offset=offset,limit=20))
        seen.extend(row["name"] for row in page["items"])
        offset = page["next_offset"]
    assert len(seen)==len(set(seen))==len(studies)
    assert directory.search(CapabilitySearch(query="method_204"))["items"][0]["name"] == "method_204"
    assert directory.search(CapabilitySearch(query="计"))["total"] == len(seen)
    assert directory.search(CapabilitySearch(query="not-present"))["items"] == []


def test_search_scientific_connections_keeps_units_shape_and_state_contracts():
    power = Port(quantity="test.flux",label="Flux",unit="W",scale=1,shape="scalar")
    method = Study("power", "功率", "1", "Flux", Inputs, lambda p: {}, input_ports={"value":power})
    directory = CapabilityDirectory({"power":(Inputs,"Flux"),"act":(Inputs,"Record flux")},{"power":(method,"test")},{})
    request = BrowserSearch(query="flux")
    def names(unit, scale, shapes):
        source = SourceSignature(quantity="test.flux",unit=unit,scale=scale,connects_as=shapes)
        return [r["name"] for r in directory.search(request,[source])["items"]]
    assert names("mW",.001,["scalar","interval"]) == ["power"]
    assert names("W",.001,["scalar"]) == []
    assert names("W",1,["interval"]) == []
    assert directory.search(CapabilitySearch(query="flux",original_files=True))["items"] == []
    assert directory.search(CapabilitySearch(accepts_quantity="test.unknown"))["total"] == 0
    assert directory.search(CapabilitySearch(query="flux",kind="action"))["total"] == 1


def test_browser_pages_and_single_definition_share_registered_metadata(client):
    root = "/api/cases/"+str(client.post("/api/cases",json={"title":"目录试验"}).json()["id"])
    result = client.post(root+"/capabilities/search",json={"kind":"study","query":"fit_power_curve","limit":1}).json()
    item, = result["items"]
    assert item["name"] == "fit_power_curve" and "inputs" not in item
    definition = client.get(root+"/studies/catalog/"+item["name"]).json()
    assert definition["provider"] == item["provider"] and definition["version"] == item["version"]
    assert definition["provider_display_name"] == item["provider_display_name"] == "BeamWeft"
    branded = client.post(root+"/capabilities/search",json={"kind":"study","query":"BeamWeft"}).json()
    assert branded["items"] and all(row["provider_display_name"]=="BeamWeft" for row in branded["items"])
    assert "pump_w" in definition["inputs"]["properties"]
    assert client.get(root+"/studies/catalog/absent").status_code == 404
    assert client.post(root+"/capabilities/search",json={"offset":-1}).status_code == 422
    assert "capabilities" not in client.get(root+"/workbench?include_capabilities=false").json()
    assert client.get(root+"/workbench").json()["capabilities"]
    assert client.post("/api/cases/99999999/capabilities/search",json={}).status_code==404
    assert all(row["name"] not in tools.STUDIES for row in tools.schemas(tools.STARTING_TOOLS))

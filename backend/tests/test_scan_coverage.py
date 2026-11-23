import json

import pytest

from app.workbench.optics import scan_system, scan_system_2d


def cavity():
    return {"mode":"standing_wave", "wavelength_nm":1000, "elements":[
        {"name":"Plane", "kind":"mirror"}, {"name":"Bench", "kind":"space", "length_mm":50},
        {"name":"End", "kind":"mirror", "radius_mm":100}]}


def axis(index, parameter, start, stop, count):
    return {"element_index":index, "parameter":parameter, "start":start, "stop":stop, "count":count}


@pytest.mark.parametrize("start,stop,count,expected", [
    (-3,-1,3,("needs_input",0,3)), (-10,110,13,("partial",11,2)), (100,120,3,("completed",3,0))])
def test_scan_distinguishes_invalid_geometry_from_valid_unstable_cavities(start, stop, count, expected):
    result = scan_system({"system":cavity(), **axis(1,"length_mm",start,stop,count)})
    assert (result["status"], result["valid_count"], result["invalid_count"]) == expected
    assert result["partial_count"] == 0 and result["completed_count"] == result["valid_count"]
    for row in result["points"]:
        if row["value"] <= 0:
            assert row["status"] == "invalid" and row["message"] and "tangential" not in row
        else:
            assert row["status"] == "completed"
            assert row["tangential"]["m"] == pytest.approx(1 - 2 * row["value"]/100)
            assert row["stable_both_planes"] == (0 < row["value"] < 100)


def test_map_lookup_carries_dependencies_and_preserves_valid_nonstable_cells():
    x = axis(1,"length_mm",-10,110,13)
    y = axis(2,"radius_mm",100,120,3)
    report = scan_system_2d({"system":cavity(), "x":x, "y":y})
    assert report["status"] == "partial" and report["valid_count"] == 33 and report["invalid_count"] == 6
    failed = {(i,j):g["message"] for g in report["invalid_groups"] for i,j in g["cells"]}
    assert "Bench" in failed[0,0] and "正" in failed[0,1]
    for i,radius in enumerate(report["y"]):
        for j,length in enumerate(report["x"]):
            stable = report["maps"]["stable_both"][i][j]
            if (i,j) in failed:
                assert length <= 0 and stable is None
            else:
                assert report["maps"]["sagittal"][i][j] == pytest.approx(1-2*length/radius)
                assert stable == int(0<length<radius)
    report = scan_system_2d({"system":cavity(),"x":axis(1,"length_mm",-8,-2,4),"y":y})
    assert report["status"] == "needs_input" and report["valid_count"] == 0
    assert len(report["invalid_groups"]) == 1 and len(report["invalid_groups"][0]["cells"]) == 12


def test_incomplete_incident_beam_preserves_a_valid_solved_axis():
    config={"mode":"single_pass","wavelength_nm":1000,"source":{
        "tangential":{"m2":1,"radius_mm":.5,"wavefront":"waist"},
        "sagittal":{"m2":1,"radius_mm":.6}}, "elements":[{"name":"Gap","kind":"space","length_mm":30}]}
    result=scan_system({"system":config,**axis(0,"length_mm",10,30,3)})
    assert (result["status"],result["valid_count"],result["completed_count"],result["partial_count"],
            result["invalid_count"],result["stable_count"]) == ("partial",3,0,3,0,None)
    assert all(row["tangential"]["output"]["radius_mm"]>.5 and "波前" in row["sagittal"]["message"]
               for row in result["points"])


def test_finite_extreme_endpoints_do_not_create_unserializable_scan_coordinates():
    sweep = axis(1,"length_mm",-1e308,1e308,3)
    result = scan_system({"system":cavity(), **sweep})
    assert [row["value"] for row in result["points"]] == [-1e308,0,1e308]
    assert result["status"] == "needs_input" and result["step"] == 1e308
    grid = scan_system_2d({"system":cavity(), "x":sweep, "y":axis(2,"radius_mm",80,100,2)})
    assert grid["x"] == [-1e308,0,1e308] and grid["invalid_count"] == 6
    json.dumps([result,grid], allow_nan=False)

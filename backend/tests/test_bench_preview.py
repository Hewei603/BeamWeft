import pytest

from app.workbench.optics import analyze_system


def test_archive_transport_preserves_every_sample_and_negotiates_the_encoding(client, db):
    case = client.post("/api/cases", json={"title":"光场记录"}).json()
    url = f"/api/cases/{case['id']}/optical-project"
    project = {"title":"标定", "system":{"mode":"single_pass", "wavelength_nm":532, "input_radius_mm":.4,
               "samples_per_segment":150, "elements":[{"kind":"space","name":"空气","length_mm":100}]}}
    saved = client.post(url, json={"project":project,"change_note":"登记"}).json()
    compressed = client.get(url, headers={"Accept-Encoding":"gzip"})
    plain = client.get(url, headers={"Accept-Encoding":"gzip;q=0, *;q=1"})
    assert compressed.headers["content-encoding"] == "gzip" and "content-encoding" not in plain.headers
    assert "accept-encoding" in compressed.headers["vary"].lower()
    assert int(compressed.headers["content-length"]) < len(plain.content)
    assert compressed.json() == plain.json()
    assert compressed.json()["current"]["analysis"] == saved["analysis"]
    assert client.get(url,headers={"Accept-Encoding":"identity;q=0,gzip;q=0"}).status_code == 406


def test_compressed_scientific_grid_round_trips_without_persisting_rejected_writes(client):
    case = client.post("/api/cases", json={"title":"Grid on the detector"}).json()
    root = f"/api/cases/{case['id']}/studies"
    study = "propagate_coherent_field"
    layout = {"system":{"mode":"single_pass","wavelength_nm":780,"input_radius_mm":.3,
                        "elements":[{"uid":"s","name":"Air","kind":"space","length_mm":10}]}}
    data = {"provider":"LaserClaw", "version":"2", "inputs":{"layout":layout,"samples":128,"window_mm":2}}
    gzip = {"Accept-Encoding":"gzip"}
    preview = client.post(f"{root}/preview/{study}",json=data,headers=gzip)
    assert preview.status_code == 200 and preview.headers["content-encoding"] == "gzip"
    save = f"{root}/run/{study}"
    assert client.post(save,json=data,headers={"Accept-Encoding":"identity;q=0,gzip;q=0"}).status_code == 406
    assert client.get(root+"/runs").json()["items"] == []
    added = client.post(save,json=data,headers=gzip)
    assert added.status_code == 200 and added.headers["content-encoding"] == "gzip"
    url = f"{root}/runs/{added.json()['study_record']['id']}"
    compressed = client.get(url,headers=gzip)
    plain = client.get(url,headers={"Accept-Encoding":"identity"})
    assert compressed.json() == plain.json()
    assert int(compressed.headers["content-length"]) < len(plain.content)
    assert plain.json()["output"]["grid"] == added.json()["grid"] == preview.json()["grid"]


def test_live_state_is_case_scoped_read_only_and_resolved_like_an_archived_connection(client):
    def case(title):
        cid = client.post("/api/cases", json={"title": title}).json()["id"]
        return f"/api/cases/{cid}/studies"

    root, other = case("同轴相干续算"), case("独立实验工程")
    name = "propagate_coherent_field"
    space = {"uid": "s", "name": "空气", "kind": "space", "length_mm": 6}
    system = {"mode": "single_pass", "wavelength_nm": 780, "input_radius_mm": .4,
              "elements": [{**space, "clear_aperture": {"shape":"ellipse","width_mm":.6}}]}
    data = {"provider":"LaserClaw","version":"2","inputs":{"layout":{"system":system},"window_mm":2,"samples":128}}
    source = client.post(f"{root}/run/{name}", json=data)
    assert source.status_code == 200, source.text
    producer = source.json()["study_record"]["id"]
    receiver = {**data, "inputs":{**data["inputs"], "layout":{"system":{**system,"elements":[space]}}}}
    ref = {"run_id":producer,"port":"exit_field","registration":"同一实验标记面，空气与坐标轴对齐。"}
    bound = {**receiver, "connections":{"input_field":ref}}
    preview = client.post(f"{root}/preview/{name}", json=bound)
    assert preview.status_code == 200, preview.text
    assert 0 < preview.json()["throughput"] < 1
    for invalid in [{**ref,"registration":""}, {**ref,"port":"throughput"}]:
        assert client.post(f"{root}/preview/{name}",json={**receiver,"connections":{"input_field":invalid}}).status_code == 422
    assert client.post(f"{other}/preview/{name}",json=bound).status_code == 422
    assert [row["id"] for row in client.get(root+"/runs").json()["items"]] == [producer]
    assert client.get(other+"/runs").json()["items"] == []
    stored = client.post(f"{root}/run/{name}",json=bound).json()
    assert stored["study_record"]["connections"]["input_field"] == ref
    assert preview.json()["grid"] == stored["grid"]


@pytest.mark.parametrize("system", [
    {"mode": "single_pass", "input_radius_mm": .3, "input_curvature_mm": -200, "beam_quality_m2": 1.3,
     "elements": [{"kind": "space", "name": "air", "length_mm": 30}, {"kind": "lens", "name": "L", "focal_mm": 50},
                  {"kind": "space", "name": "slab", "length_mm": 70, "n": 1.6}]},
    {"mode": "standing_wave", "elements": [{"kind": "mirror", "name": "flat"},
     {"kind": "space", "name": "gap", "length_mm": 240}, {"kind": "mirror", "name": "M2", "radius_mm": 200}]},
    {"mode": "ring", "elements": [{"kind": "matrix", "name": "cylindrical", "matrix_t": [1, 0, -.01, 1],
      "matrix_s": [1, 0, .01, 1]}, {"kind": "space", "name": "return", "length_mm": 100}]},
])
def test_nonpersistent_preview_reuses_full_solver_at_every_element(client, system):
    case = client.post("/api/cases", json={"title": "Bench preview"}).json()
    base = f"/api/cases/{case['id']}/optical-project"
    config = {**system, "wavelength_nm": 1064, "samples_per_segment": 150}
    preview = client.post(base + "/preview", json=config)
    assert preview.status_code == 200
    actual = preview.json()
    reference = analyze_system(config)
    for name, data in reference["planes"].items():
        plane = actual["planes"][name]
        assert plane["radii_mm"] == pytest.approx([node["radius_mm"] for node in data["elements"]])
        assert plane["m"] == data.get("m")
        assert plane["stable"] == data.get("stable")
        if data.get("stable") is False:
            assert plane["profile"] == []
        else:
            assert 0 < len(plane["profile"]) < len(data["samples"])
            assert plane["profile"][-1][-1] == pytest.approx(data["output"]["radius_mm"])
    assert client.get(base).json()["revisions"] == []

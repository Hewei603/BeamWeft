import csv
import io
from copy import deepcopy
import numpy as np
import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from app.models import GeneratedContent
from app.open_case import open_package
from app.physics.caustic import fit_caustic
from app.physics.materials import n_real
from app.workbench.beam_comparisons import CompareBeam, compare_reading
from app.workbench.beam_studies import _compare_to_project
from app.workbench.service import validate_finish


WAVELENGTH = 808
N = 1.4
WAIST = .060
M2 = 1.6
THETA = M2 * WAVELENGTH * 1e-6 / (np.pi * N * WAIST)
STATIONS = np.array([0., 10., 23., 38., 55., 70., 85., 100.])


def width(z):
    return np.hypot(WAIST, THETA*(z-45))


def experiment(client, name):
    return client.post("/api/cases", json={"title": name}).json()["id"]


def attach(client, case, name, values):
    return client.post(f"/api/cases/{case}/attachments", files={"file": (name, values, "application/octet-stream")}).json()["id"]


def mapping(file):
    return {"kind": "table", "attachment_id": file, "delimiter": "semicolon", "first_row": 2, "position_column": 2,
            "width_column": 3, "z_unit": "cm", "width_unit": "um", "width_definition": "d4sigma_diameter",
            "wavelength_nm": WAVELENGTH, "medium_n": N, "axis": "X", "reference": "rail datum at 0 mm", "conditions": "A",
            "homogeneous_interval": True}


def csv_file():
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(["scan", "rail cm", "D4sigma um"])
    for z in STATIONS:
        writer.writerow(["A", z/10, 2000*width(z)])
    return out.getvalue().encode("utf-16")


def test_station_holdout_keeps_acquisitions_together_and_scores_original_cells(client):
    case = experiment(client, "分组预测")
    api = f"/api/cases/{case}/beam-studies"
    rows = [(z, f"frame-{z}", 0.) for z in [-90, -70, -50, -30, -10, 0, 10, 90]]
    rows += [(30, "A", .012), (30, "A2", -.002), (50, "A", -.008), (50, "B", .01),
             (70, "B", .005), (120, "C", .018)]
    # A final original reading in a previously used exposure is outside the selection.
    rows.append((110, "frame-90", .011))
    def hardware_radius(z):
        return np.hypot(.105, .004*z)
    def numeric(value):
        return f"{value:.12f}".replace(".", ",")
    data = ["batch;rail / cm;D4sigma / um"]
    data.extend(f"{group};{numeric(z/10)};{numeric(2000*(hardware_radius(z)+delta))}" for z, group, delta in rows)
    file = attach(client, case, "cameras.csv", "\n".join(data).encode())
    source = {**mapping(file), "acquisition_column": 1, "decimal_mark": ",", "last_row": len(data)-1}
    def study(stations):
        reply = client.post(api, json={"source": source, "holdout": {"positions_mm": stations}})
        assert reply.is_success, reply.text
        return reply.json()
    record = study([120, 30])
    assert record["fit"]["waist_radius_mm"] == pytest.approx(.105)
    assert record["fit"]["divergence_half_angle_rad"] == pytest.approx(.004)
    assert record["fit"]["waist_z_mm"] == pytest.approx(0, abs=1e-8)
    assert record["fit"]["m2"] == pytest.approx(np.pi*N*.105*.004/(WAVELENGTH*1e-6))
    assert len(record["points"]) == 8
    h = record["holdout"]
    assert h["status"] == "scored" and h["grouping"] == "acquisition_column"
    assert h["width_basis"] == "second_moment" and h["axis"] == "X"
    assert h["training_range_mm"] == [-90,90] and h["positions_mm"] == [30,50,70,120]
    assert [p["row"] for p in h["points"]] == list(range(10,16))
    assert all(p["use"] == "held_out" for p in h["points"])
    for p, (z, group, delta) in zip(h["points"], rows[8:]):
        assert p["acquisition_id"] == group and p["z_mm"] == z
        assert p["predicted_width"] == pytest.approx(2000*hardware_radius(z))
        assert p["residual_mm"] == pytest.approx(delta, abs=1e-10)
        assert p["width_residual"] == pytest.approx(2000*delta)
        assert p["extrapolated"] == (z == 120)
    station_ms = [(.012**2+.002**2)/2, (.008**2+.01**2)/2, .005**2, .018**2]
    assert h["station_rms_radius_mm"] == pytest.approx(np.sqrt(np.mean(station_ms)))
    assert study([30,120,30])["id"] == record["id"]
    assert study([50,120,30])["study_key"] == record["study_key"]
    assert client.get(api, params={"study_id":record["id"]}).json() == record
    forecast = client.post(api+f"/{record['id']}/predict",json={"positions_mm":[120]}).json()["predictions"][0]
    assert forecast["extrapolated"] and forecast["radius_mm"] == pytest.approx(hardware_radius(120))
    check = client.post(api, json={"source":{**source, "first_row":len(data), "last_row":len(data)},
                                   "reference_fit":{"study_id":record["id"], "note":"标尺未移动"}}).json()
    assert check["check"]["counts"] == {"fit_acquisition":1} and check["check"]["radius_rms_mm"] is None
    for withheld, reason in [([31], "未能唯一定位"), ([-90,-70,-50,-30,-10,0,10,30,120], "至少需要 5")]:
        failure = study(withheld)
        assert failure["status"] == "needs_input" and reason in failure["message"]
    assert client.post(api, json={"source":source, "holdout":{"positions_mm":[30]},
                                  "reference_fit":{"study_id":record["id"],"note":"同轴"}}).status_code == 422
    assert client.post(api, json={"source":{**source,"acquisition_column":source["width_column"]}}).status_code == 422


def test_registered_holdout_partitions_original_acquisitions_and_source_provenance(client):
    case = experiment(client, "注册原件中的保留组")
    root = f"/api/cases/{case}"
    planes = [{"uid":f"p{i}", "name":str(z), "target_uid":"rail", "depth_mm":z} for i,z in enumerate(STATIONS)]
    project = {"title":"相机导轨","system":{"mode":"single_pass","wavelength_nm":WAVELENGTH,
               "elements":[{"uid":"rail","name":"空气","kind":"space","length_mm":100,"n":1}]},
               "observation_planes":planes}
    version = client.post(root+"/optical-project",json={"project":project,"change_note":"已登记空气段"}).json()["revision_id"]
    readings, files = [], []
    for i,z in enumerate(STATIONS):
        # Two separately uploaded copies of the same raw acquisition must remain together.
        encoded = b"same-camera-acquisition" if i in (1,6) else f"sweep-{i}".encode()
        file = attach(client, case, f"run-{i}.txt", encoded)
        payload = {"revision_id":version,"plane_uid":f"p{i}","beam_uid":"primary","attachment_id":file,
                   "locator":f"frame {i}","conditions":"单一导轨扫描","wavelength_nm":WAVELENGTH,
                   "definition":"d4sigma_diameter","axes_aligned":True,"unit":"um","tangential":2000*width(z)}
        reading = client.post(root+"/optical-project/beam-readings",json=payload)
        assert reading.is_success, reading.text
        readings.append(reading.json()["id"])
        files.append(file)
    source = {"kind":"registered","revision_id":version,"reading_ids":readings,"axis":"tangential"}
    result = client.post(root+"/beam-studies",json={"source":source,"holdout":{"positions_mm":[10]}}).json()
    h = result["holdout"]
    assert h["grouping"] == "source_file" and h["positions_mm"] == [10,85]
    assert {p["locator"] for p in h["points"]} == {"frame 1","frame 6"}
    assert {p["attachment_id"] for p in h["points"]} == {files[1],files[6]}
    assert {p["reading_id"] for p in h["points"]} == {readings[1],readings[6]}
    assert all(abs(p["residual_mm"]) < 1e-8 for p in h["points"])
    assert set(result["source"]["reading_ids"]) == set(readings)-{readings[1],readings[6]}
    assert {f["attachment_id"] for f in result["metadata"]["files"]} == set(files)-{files[1],files[6]}
    assert {f["attachment_id"] for f in h["files"]} == set(files)
    just_train = client.post(root+"/beam-studies",json={"source":result["source"]}).json()
    assert result["study_key"] == just_train["study_key"] and result["fit"] == just_train["fit"]
    launch = client.post(root+"/optical-project/measured-launch",json={
        "study_id":result["id"],"revision_id":version,"axis":"tangential","path_uid":"primary",
        "source_z_mm":0,"registration":"相机的 T 与工程 T 对齐，导轨原点即工程源面。"})
    assert launch.is_success, launch.text
    compare = root+f"/optical-project/{launch.json()['revision_id']}/beam-comparisons"
    setup = {"position_reference":"fixed_z","registration_note":"坐标系相同，模型使用训练读数拟合",
             "data_role":"held_out","target_beam_uid":"primary"}
    reserved = client.post(compare,json={**setup,"reading_id":readings[1]}).json()
    assert reserved["data_role"] == "held_out"
    same_exposure = client.post(root+"/optical-project/beam-readings",json={**payload,"locator":"another frame"}).json()
    linked = client.post(compare,json={**setup,"reading_id":same_exposure["id"]}).json()
    assert linked["data_role"] == "unspecified" and linked["unverified_fit_ids"] == [result["id"]]


def test_holdout_widths_cannot_rescue_unresolved_training_geometry(client):
    case = experiment(client, "无法解析的训练段")
    api = f"/api/cases/{case}/beam-studies"
    rows = ["acquisition;rail / cm;D4sigma / um", "k0;0;1800",
            *(f"k{z};{z};200" for z in range(1,8)), ";8;1700"]
    file = attach(client, case, "plateau.txt", "\n".join(rows).encode())
    options = {"source":mapping(file),"holdout":{"positions_mm":[0,80]}}
    result = client.post(api,json=options).json()
    assert result["status"] == "partial" and "fit" not in result and result["holdout"]["status"] == "not_scored"
    assert result["holdout"]["grouping"] == "position" and "采集编号" in result["holdout"]["grouping_note"]
    assert [p["raw_width"] for p in result["holdout"]["points"]] == [1800,1700]
    assert all("predicted_width" not in p for p in result["holdout"]["points"])
    assert client.get(api,params={"study_id":result["id"]}).json()["holdout"] == result["holdout"]
    bad = client.post(api,json={**options,"source":{**mapping(file),"acquisition_column":1}})
    assert bad.status_code == 422 and "第 10 行第 1 列" in bad.text


def test_fit_drives_one_axis_through_an_arbitrary_saved_layout(client):
    case = experiment(client, "相机标定交接")
    root = f"/api/cases/{case}"
    file = attach(client, case, "instrument.txt", csv_file())
    fit = client.post(root+"/beam-studies", json={"source":{**mapping(file), "medium_n":1}}).json()
    project = {"title":"透镜与相机", "system":{"mode":"single_pass","wavelength_nm":WAVELENGTH,
               "elements":[{"uid":"lens","name":"L","kind":"lens","focal_mm":70},
                           {"uid":"rail","name":"rail","kind":"space","length_mm":55,"n":1}]},
               "observation_planes":[{"uid":"screen","name":"sensor","target_uid":"rail","depth_mm":40}]}
    base = client.post(root+"/optical-project", json={"project":project,"base_revision_id":None,"change_note":"台架"}).json()["revision_id"]
    request = dict(study_id=fit["id"], revision_id=base, source_z_mm=23, axis="sagittal",
                   entry_uid="lens", exit_uid="rail", registration="镜面在扫描标尺 23 mm，X 对应 S，同向空气段。")
    endpoint = root+"/optical-project/measured-launch"
    response = client.post(endpoint, json=request)
    assert response.status_code == 200, response.text
    model = response.json()
    assert client.post(endpoint, json=request).json()["revision_id"] == model["revision_id"]
    source = model["project"]["incident_paths"][0]
    assert source["tangential"]["radius_mm"] is None and source["launch_ray"]["s_offset_mm"] is None
    origin = source["sagittal"]["fit_origin"]
    assert origin["study_id"] == fit["id"] and origin["curvature_mm"] < 0
    assert origin["radius_mm"] == pytest.approx(width(23))
    # Independent transfer of waist-plane second moments through free space, a thin lens and drift.
    a = 1-40/70
    b = 40+a*(23-45)
    expected = np.hypot(a*WAIST, b*THETA)
    beam = next(b for b in model["analysis"]["sections"]["planes"][0]["beams"] if b["uid"] == source["uid"])
    assert beam["radii_mm"] == {"sagittal":pytest.approx(expected)}
    assert beam["width_basis"]["sagittal"] == "second_moment"
    assert client.get(root+"/optical-project").json()["current"]["revision_id"] == base
    # A second measurement may populate a different axis of the same candidate.
    other = client.post(root+"/beam-studies", json={"source":{**mapping(file),"medium_n":1,"axis":"Y"}}).json()
    second = {k:v for k,v in request.items() if k not in ("entry_uid","exit_uid")}
    second.update(study_id=other["id"], revision_id=model["revision_id"], source_z_mm=45, axis="tangential",
                  path_uid=source["uid"], registration="Y 与 T 同向，该批次 Y 的轴向坐标零点另行登记。")
    completed = client.post(endpoint,json=second)
    assert completed.status_code == 200, completed.text
    two = completed.json()["project"]["incident_paths"][0]
    assert two["tangential"]["wavefront"] == "waist" and two["sagittal"] == source["sagittal"]
    assert completed.json()["base_revision_id"] == base
    for changes,reason in [({"medium_n":N},"空气"), ({"medium_n":None},"空气"),
                           ({"medium_n":1,"wavelength_nm":None},"波长"),
                           ({"medium_n":1,"wavelength_nm":5000},"M²")]:
        candidate = client.post(root+"/beam-studies",json={"source":{**mapping(file),**changes}}).json()
        invalid = client.post(endpoint,json={**request,"study_id":candidate["id"]})
        assert invalid.status_code == 422 and reason in invalid.text
        if changes.get("medium_n") == N:
            registered = client.post(endpoint,json={**request,"study_id":candidate["id"],"normal_exit_to_air":True})
            assert registered.status_code == 200, registered.text
            transmitted = registered.json()["project"]["incident_paths"][0]["sagittal"]
            physical_radius = (23-45) + (WAIST/THETA)**2 / (23-45)
            assert transmitted["fit_origin"]["medium_n"] == N
            assert transmitted["fit_origin"]["curvature_mm"] == pytest.approx(physical_radius)
            assert transmitted["curvature_mm"] == pytest.approx(physical_radius/N)
            assert transmitted["radius_mm"] == pytest.approx(width(23))
            assert transmitted["m2"] == pytest.approx(M2)
    assert client.post(endpoint,json={**request,"entry_uid":"rail","exit_uid":"lens"}).status_code == 422


def test_width_fit_near_ideal_cannot_supply_a_coherent_field():
    from app.workbench.beam_source import SourceAxis
    from app.workbench.wave_study import coherent_axis
    fitted = SourceAxis(radius_mm=.2, m2=1, wavefront="waist",fit_origin=dict(study_id=1,
        measured_axis="X",coordinate_mm=0,registration="Origin",width_basis="gaussian_equivalent",
        wavelength_nm=635,radius_mm=.2,curvature_mm=None,m2=1))
    assert fitted.width_basis() == "equivalent"
    with pytest.raises(ValueError,match="复振幅"):
        coherent_axis(635,"光斑",fitted,None,"t")


def test_main_launch_replaces_one_axis_preserving_other_evidence_and_alignment(client):
    case = experiment(client, "沿主光路接入光束")
    root = f"/api/cases/{case}"
    file = attach(client, case, "axes.csv", csv_file())
    fit = client.post(root+"/beam-studies", json={"source":{**mapping(file), "medium_n":1}}).json()
    system = {"mode":"single_pass", "wavelength_nm":WAVELENGTH,
              "elements":[{"uid":"air", "name":"air", "kind":"space", "length_mm":100}],
              "source":{"tangential":{"radius_mm":.22, "wavefront":"curved", "curvature_mm":-62, "m2":1.7,
                                      "second_moment_radius":True}, "launch_ray":{"t_offset_mm":.02,"s_angle_mrad":None}}}
    monitor = {"uid":"meter", "name":"相机位置", "position_reference":"fixed_z", "z_mm":37}
    saved = client.post(root+"/optical-project", json={"project":{"title":"源面", "system":system, "observation_planes":[monitor]},
                                                       "change_note":"光源已标定指向，两轴初始共用参数"}).json()
    request = {"study_id":fit["id"], "revision_id":saved["revision_id"], "axis":"tangential","path_uid":"primary",
               "source_z_mm":0, "registration":"相机 X 与 T 同向，扫描标尺的原点与源面重合。"}
    endpoint = root+"/optical-project/measured-launch"
    reply = client.post(endpoint, json=request)
    assert reply.status_code == 200, reply.text
    trial = reply.json()
    before = saved["project"]["system"]["source"]
    after = trial["project"]["system"]["source"]
    assert trial["launch_path_uid"] == "primary" and not trial["project"]["incident_paths"]
    assert after["sagittal"] == before["tangential"] and after["launch_ray"] == before["launch_ray"]
    assert after["tangential"]["fit_origin"]["study_id"] == fit["id"]
    assert client.get(root+"/optical-project").json()["current"]["project"]["system"]["source"] == before
    rerun = client.post(endpoint, json={**request,"axis":"sagittal","revision_id":trial["revision_id"]}).json()
    assert rerun["project"]["system"]["source"]["tangential"] == after["tangential"]

    # Portable hardware and its attached station must use the recipient illumination.
    captured = client.post(root+"/component-models",json={"name":"测试台与相机面","project_revision_id":trial["revision_id"],
                                                          "first_uid":"air","last_uid":"air","reference_plane_uids":["meter"]})
    assert captured.is_success, captured.text
    recipient = experiment(client, "不同光源借用相同测试台")
    dest = f"/api/cases/{recipient}"
    other = {"tangential":{"wavefront":"waist", "radius_mm":.41, "m2":1}}
    base = client.post(dest+"/optical-project", json={"project":{"title":"接收端",
        "system":{"mode":"single_pass","wavelength_nm":WAVELENGTH,"source":other,"elements":[]}},
        "change_note":"独立光源"}).json()
    placed = client.post(dest+"/component-models/preview-placement", json={"project_revision_id":base["revision_id"],
        "model_revision_id":captured.json()["model_revision_id"]})
    assert placed.is_success, placed.text
    copy = placed.json()["project"]
    assert copy["system"]["source"] == base["project"]["system"]["source"]
    assert copy["observation_planes"][0]["target_uid"] == copy["system"]["elements"][0]["uid"]
    assert copy["observation_planes"][0]["depth_mm"] == 37

    for replacement in (
        {**system, "wavelength_nm":976},
        {"mode":"standing_wave", "wavelength_nm":WAVELENGTH, "elements":[
            {"kind":"mirror","name":"plane"},{"kind":"space","name":"air","length_mm":20},
            {"kind":"mirror","name":"curved","radius_mm":150}]},
    ):
        head = client.get(root+"/optical-project").json()["current"]["revision_id"]
        new = client.post(root+"/optical-project",json={"base_revision_id":head, "project":{"title":"对照","system":replacement},
                                                       "change_note":"换光路条件"}).json()
        assert client.post(endpoint,json={**request,"revision_id":new["revision_id"]}).status_code == 422


@pytest.mark.parametrize("target", ["branch", "primary"])
def test_registered_fit_inputs_cannot_be_relabelled_as_independent_camera_checks(client, tmp_path, target):
    case = experiment(client, "校准和检验")
    root = f"/api/cases/{case}"
    file = attach(client,case,"camera.csv",csv_file())
    planes = [{"uid":f"p{i}","name":str(z),"target_uid":"rail","depth_mm":z} for i,z in enumerate(STATIONS)]
    project = {"title":"台架","system":{"mode":"single_pass","wavelength_nm":WAVELENGTH,
               "elements":[{"uid":"rail","name":"air","kind":"space","length_mm":100,"n":1}]},
               "observation_planes":planes}
    base = client.post(root+"/optical-project",json={"project":project,"base_revision_id":None,"change_note":"台架"}).json()["revision_id"]
    readings = []
    for i,z in enumerate(STATIONS):
        payload = dict(revision_id=base,plane_uid=f"p{i}",beam_uid="primary",attachment_id=file,
                       locator=f"cell C{i+2}",conditions="共同装夹",wavelength_nm=WAVELENGTH,definition="d4sigma_diameter",
                       axes_aligned=True,unit="um",tangential=2000*width(z))
        readings.append(client.post(root+"/optical-project/beam-readings",json=payload).json()["id"])
    fit = client.post(root+"/beam-studies",json={"source":{"kind":"registered","revision_id":base,
                      "reading_ids":readings,"axis":"tangential"}}).json()
    # Registering the same original in other units creates a separate receipt,
    # but cannot supply a second observation of instrument repeatability.
    def fit_with(addition):
        record = client.post(root+"/optical-project/beam-readings", json={**payload,**addition}).json()["id"]
        result = client.post(root+"/beam-studies", json={"source":{"kind":"registered","revision_id":base,
            "reading_ids":[*readings,record],"axis":"tangential"}}).json()
        return record, result
    same, combined = fit_with({"unit":"mm","tangential":2*width(STATIONS[-1])})
    assert combined["duplicate_readings"] == [{"reading_id":same,"counted_reading_id":readings[-1]}]
    assert combined["fit"]["repeatability"]["degrees_of_freedom"] == 0
    _, conflict = fit_with({"tangential":2100*width(STATIONS[-1])})
    assert conflict["status"] == "needs_input" and str(readings[-1]) in conflict["message"]
    _, repeat_fit = fit_with({"locator":"second frame","tangential":2100*width(STATIONS[-1])})
    assert repeat_fit["fit"]["repeatability"]["degrees_of_freedom"] == 1
    bind = dict(study_id=fit["id"],revision_id=base,axis="tangential",entry_uid="rail",exit_uid="rail",
                source_z_mm=0,registration="同一展开坐标与光束，T 对 T")
    if target == "primary":
        bind.pop("entry_uid")
        bind.pop("exit_uid")
        bind["path_uid"] = "primary"
    endpoint = root+"/optical-project/measured-launch"
    assert client.post(endpoint,json={**bind,"source_z_mm":10}).status_code == 422
    trial = client.post(endpoint,json=bind).json()
    comparison = dict(reading_id=readings[-1],position_reference="fixed_z",registration_note="同一架设，再用实测源重算",
                      target_beam_uid=trial["launch_path_uid"],data_role="held_out")
    url = root+f"/optical-project/{trial['revision_id']}/beam-comparisons"
    trained = client.post(url,json=comparison).json()
    assert trained["declared_data_role"] == "held_out" and trained["data_role"] == "model_input"
    assert trained["fit_input_ids"] == [fit["id"]]
    assert trained["reference_prediction"]["evidence"]["data_role"] == "unspecified"
    assert trained["target_prediction"]["evidence"]["data_role"] == "model_input"
    assert trained["reference_prediction"]["beam_uid"] == "primary"
    assert trained["target_prediction"]["widths"]["tangential"]["residual"] == pytest.approx(0, abs=1e-8)
    # The same acquisition can also be registered on the fitted model. Switching the
    # positions of the models must preserve which one was calibrated on this reading.
    rerecorded = client.post(root+"/optical-project/beam-readings", json={
        **payload, "revision_id": trial["revision_id"], "beam_uid": trial["launch_path_uid"]}).json()["id"]
    reverse = client.post(root+f"/optical-project/{base}/beam-comparisons", json={
        **comparison, "reading_id": rerecorded, "target_beam_uid": "primary", "reference_data_role": "held_out"}).json()
    assert reverse["reference_prediction"]["evidence"] == {
        "declared_data_role": "held_out", "data_role": "model_input", "fit_input_ids": [fit["id"]], "unverified_fit_ids": []}
    assert reverse["target_prediction"]["evidence"]["data_role"] == "held_out"
    assert reverse["reference_prediction"]["widths"]["tangential"]["residual"] == pytest.approx(0, abs=1e-8)
    duplicate = attach(client,case,"copy.csv",csv_file())
    copied = client.post(root+"/optical-project/beam-readings",json={**payload,"attachment_id":duplicate}).json()
    assert client.post(url,json={**comparison,"reading_id":copied["id"]}).json()["data_role"] == "model_input"
    independent = attach(client,case,"check.csv",b"runB,x_d4sigma_um\n2,820\n")
    fresh = client.post(root+"/optical-project/beam-readings",json={**payload,"attachment_id":independent,
                        "locator":"row 2","tangential":820}).json()
    verified = client.post(url,json={**comparison,"reading_id":fresh["id"]}).json()
    assert verified["data_role"] == "held_out" and "fit_input_ids" not in verified
    table = client.post(root+"/beam-studies",json={"source":{**mapping(file),"medium_n":1}}).json()
    other = client.post(endpoint,json={**bind,"study_id":table["id"]}).json()
    unknown = client.post(root+f"/optical-project/{other['revision_id']}/beam-comparisons",
                          json={**comparison,"target_beam_uid":other["launch_path_uid"]}).json()
    assert unknown["data_role"] == "unspecified" and unknown["unverified_fit_ids"] == [table["id"]]

    # Re-open the exported document in its own workspace. A new comparison there
    # must recheck the actual original, fit and axis provenance, not replay a stored answer.
    revision = trial["revision_id"]
    exported = client.get(root + "/project-file", params={"revision_id":revision})
    assert exported.status_code == 200
    package = tmp_path / "fit-study.lclaw"
    package.write_bytes(exported.content)
    folder, _ = open_package(package, tmp_path / "receiver")
    engine = create_engine(URL.create("sqlite", database=str(folder / "laserclaw.db")))
    try:
        with Session(engine) as receiving:
            resumed = compare_reading(receiving, case, CompareBeam(**{**comparison,
                "registration_note":"归档重开后用同一标尺和原始读数独立重算。"}), revision)
            assert resumed["id"] != trained["id"]
            assert resumed["data_role"] == "model_input" and resumed["fit_input_ids"] == [fit["id"]]
            assert resumed["target_prediction"]["widths"]["tangential"] == trained["target_prediction"]["widths"]["tangential"]
            # Pasted project JSON in an unrelated workspace may point at a different
            # fit under the same integer ID even when both use the same wavelength and axis.
            local_fit = receiving.get(GeneratedContent,fit["id"])
            local_fit.content = receiving.get(GeneratedContent,repeat_fit["id"]).content
            receiving.flush()
            collision = compare_reading(receiving,case,CompareBeam(**{**comparison,
                "registration_note":"另一个工作区的同号拟合，须核对独立性。"}),revision)
            assert collision["data_role"] == "unspecified" and collision["unverified_fit_ids"] == [fit["id"]]
    finally:
        engine.dispose()


def test_archived_widths_without_spectral_metadata_can_predict_and_score_geometric_residuals(client):
    case = experiment(client, "geometric fit")
    source = mapping(attach(client,case,"rail.csv",csv_file()))
    source.pop("medium_n")
    source.pop("wavelength_nm")
    api = f"/api/cases/{case}/beam-studies"
    fit = client.post(api,json={"source":source}).json()
    assert fit["status"] == "partial" and fit["fit"]["m2"] is None
    assert fit["fit"]["bpp_mm_mrad"] == pytest.approx(1000*WAIST*THETA)
    forecast = client.post(api,json={"source":{"kind":"prediction","study_id":fit["id"],"positions_mm":[45]}}).json()
    assert forecast["status"] == "completed" and forecast["predictions"][0]["radius_mm"] == pytest.approx(WAIST)
    checked = client.post(api,json={"source":{**source,"wavelength_nm":808},
        "reference_fit":{"study_id":fit["id"],"note":"Identical rail, axis and batch"}}).json()
    assert checked["status"] == "partial"
    assert set(checked["check"]["unverified_conditions"]) == {"真空波长","介质折射率"}
    assert checked["check"]["radius_rms_mm"] is None   # These are the same original cells.


def test_vendor_export_archived_before_format_detection_can_be_inspected_and_fit(client, db):
    from app.models import Attachment
    case = experiment(client, "旧仪器导出")
    fid = attach(client, case, "radii.instrument", csv_file())
    row = db.get(Attachment, fid)
    assert row.file_type == "text/plain"
    row.file_type = "application/octet-stream"
    db.commit()
    view = client.get(f"/api/attachments/{fid}/preview").json()
    assert view["table_candidate"] and view["read_mode"] == "text"
    api = f"/api/cases/{case}"
    data = client.post(api+"/measurement-view", json={"attachment_id":fid}).json()
    assert data["valid"] == len(STATIONS)
    result = client.post(api+"/beam-studies", json={"source":mapping(fid)})
    assert result.status_code == 200, result.text
    assert result.json()["fit"]["waist_z_mm"] == pytest.approx(45)


def test_numerical_fit_and_station_sensitivity():
    fit = fit_caustic(STATIONS, width(STATIONS), WAVELENGTH, N)
    assert fit["quality"] == "completed" and fit["waist_z_mm"] == pytest.approx(45) and fit["m2"] == pytest.approx(M2)
    assert fit["leave_one_position_out_z_mm"] == pytest.approx([45, 45])
    noisy = width(STATIONS)
    noisy[3] *= 1.5
    diagnostic = fit_caustic(STATIONS, noisy, WAVELENGTH, N)
    brute = [fit_caustic(np.delete(STATIONS,i),np.delete(noisy,i),WAVELENGTH,N)["waist_z_mm"] for i in range(len(noisy))]
    assert diagnostic["leave_one_position_out_z_mm"] == pytest.approx([min(brute),max(brute)])
    repeated = np.r_[STATIONS,[STATIONS[3]]*20]
    repeated_widths = np.r_[noisy,[noisy[3]]*20]
    assert fit_caustic(repeated,repeated_widths,WAVELENGTH,N)["waist_z_mm"] == pytest.approx(diagnostic["waist_z_mm"])
    far = STATIONS + 100
    outside = fit_caustic(far,width(far),WAVELENGTH,N)
    assert outside["quality"] == "partial" and not outside["bracketed"] and outside["next_steps"]
    with pytest.raises(ValueError,match="未解析"):
        fit_caustic(STATIONS,[.2]*len(STATIONS),WAVELENGTH)
    with pytest.raises(ValueError,match="3 个不同位置"):
        fit_caustic([0,1,1,1,2],[1]*5,WAVELENGTH)


@pytest.mark.parametrize("loss", ["radius", "squared_radius"])
def test_repeat_scatter_and_station_omissions_follow_measurements(loss):
    z = np.array([-90.,-45.,-25.,0.,25.,45.,90.])
    w = np.hypot(.08,.004*z)
    groups = [value*np.array([.8,1.2]) for value in w]
    groups[2] = w[2] * np.array([.8,1.,1.2])
    levels = np.repeat(z,[len(g) for g in groups])
    observed = np.concatenate(groups)
    fit = fit_caustic(levels,observed,635,1,loss=loss)
    stats = fit["repeatability"]
    assert stats["repeated_positions"] == 7 and stats["degrees_of_freedom"] == 8
    assert stats["pooled_within_sd_mm"] == pytest.approx(np.sqrt(sum(np.sum((g-np.mean(g))**2) for g in groups)/8))
    assert [r["sample_sd_mm"] for r in stats["stations"]] == pytest.approx([np.std(g,ddof=1) for g in groups])
    weights = np.repeat([1/len(g) for g in groups],[len(g) for g in groups])
    center = np.average(observed,weights=weights)
    sse = np.sum(weights*(observed-np.array(fit["data"]["fit_radius_mm"]))**2)
    assert stats["r_squared_individual_radius"] == pytest.approx(1-sse/np.sum(weights*(observed-center)**2))
    assert fit["prediction_warnings"] and fit["quality"] == "partial"
    subfits = [fit_caustic(levels[levels!=x],observed[levels!=x],635,1,loss=loss) for x in z]
    for key,bounds in fit["leave_one_position_out"].items():
        samples = [record[key] for record in subfits]
        assert [bounds["min"],bounds["max"]] == pytest.approx([min(samples),max(samples)])
        assert samples[list(z).index(bounds["omitted_at_max_mm"])] == pytest.approx(max(samples))
    single = fit_caustic(z,w,635)
    assert single["repeatability"]["pooled_within_sd_mm"] is None
    same = fit_caustic(np.repeat(z,17),np.repeat(w,17),635)
    assert same["repeatability"]["pooled_within_sd_mm"] == 0 and not same["prediction_warnings"]


@pytest.mark.parametrize("shift,scale", [(5,1),(0,1.12),(0,1)])
def test_coordinate_and_width_alternatives_are_testable_in_the_valid_interval(shift,scale):
    z = STATIONS[1:-1]
    w = width(z+shift) * scale
    fit = fit_caustic(z,w,WAVELENGTH,N)
    points = [{"z_mm":a,"radius_mm":b,"model_radius_mm":width(a)} for a,b in zip(z,w)]
    metadata = {"interval_mm":[0,100], "medium_n":N, "wavelength_nm":WAVELENGTH}
    result = _compare_to_project(points,metadata,fit)
    if shift:
        assert result["aligned_waist_shift_mm"] == pytest.approx(shift)
        assert result["shift_rms_mm"] < 1e-12 < result["scale_rms_mm"]
    else:
        assert result["width_scale"] == pytest.approx(scale) and result["scale_rms_mm"] < 1e-12
    if shift or scale != 1:
        candidate=result["next_plane"]
        assert 0 < candidate["z_mm"] < 100 and candidate["z_mm"] not in z
        assert candidate["difference_mm"] == pytest.approx(abs(candidate["scale_radius_mm"]-candidate["shift_radius_mm"]))
    else:
        assert "next_plane" not in result
    metadata["interval_mm"]=[float(z[0]),float(z[-1])]
    if shift:
        assert "shift_rms_mm" not in _compare_to_project(points,metadata,fit)


def test_partial_scientific_evidence_remains_citable_and_keeps_its_status():
    raw={"status":"completed","summary":"single-sided measurement","evidence_tool_call_ids":[7]}
    assert validate_finish(raw,{7:"partial"}).status=="partial"


def test_radius_noise_and_squared_radius_noise_lead_to_distinct_estimates():
    stations = np.linspace(0,100,95)
    observed = width(stations) + .004*np.cos(stations*.13)
    fit = fit_caustic(stations,observed,WAVELENGTH,N)
    squared = fit_caustic(stations,observed,WAVELENGTH,N,loss="squared_radius")
    assert fit["radius_rms_mm"] < squared["radius_rms_mm"]
    assert fit["squared_radius_m2"] == pytest.approx(squared["m2"])
    assert 45 <= fit["tested_omissions"] < len(stations)
    assert fit["valid_omissions"] == fit["tested_omissions"]


def test_archived_series_provenance_scopes_and_bad_rows(client):
    case, foreign = experiment(client,"table"), experiment(client,"foreign")
    api = f"/api/cases/{case}/beam-studies"
    source = mapping(attach(client,case,"rail.csv",csv_file()))
    def post(**updates):
        return client.post(api,json={"source":{**source,**updates}})
    record = post().json()
    assert record["status"] == "completed" and record["points"][-1]["row"] == 9
    assert record["metadata"]["width_basis"] == "second_moment" and len(record["metadata"]["files"][0]["sha256"]) == 64
    assert record["fit"]["m2"] == pytest.approx(M2)
    assert post().json()["id"] == record["id"]
    insufficient = post(last_row=5).json()
    assert insufficient["status"] == "needs_input" and "4 个不同位置" in insufficient["message"]
    assert len(client.get(api).json()) == 1
    assert client.get(api,params={"study_id":record["id"]}).json()["points"] == record["points"]
    foreign_api = f"/api/cases/{foreign}/beam-studies"
    assert client.get(foreign_api,params={"study_id":record["id"]}).status_code == 404
    assert client.post(foreign_api,json={"source":source}).status_code == 422
    assert post(position_column=3).status_code == 422
    assert post(last_row=12).status_code == 422
    assert post(homogeneous_interval=False).status_code == 422
    malformed = attach(client,case,"bad.tsv",b"z\tw\n0\t100\n1\tbad\n2\t110\n")
    invalid = post(attachment_id=malformed,delimiter="tab",position_column=1,width_column=2)
    assert invalid.status_code == 422 and "第 3 行" in invalid.text


def test_workbook_selection_keeps_original_row_numbers(client):
    case = experiment(client, "workbook")
    book = Workbook()
    book.active.title = "unrelated"
    sheet = book.create_sheet("camera")
    sheet.append(["metadata"])
    sheet.append(["run", "cm", "diameter"])
    for z in STATIONS:
        sheet.append(["B", z/10, width(z)*2000])
    buf = io.BytesIO()
    book.save(buf)
    source = {**mapping(attach(client,case,"camera.xlsx",buf.getvalue())), "sheet":"camera", "first_row":3}
    api = f"/api/cases/{case}/beam-studies"
    record = client.post(api,json={"source":source}).json()
    assert record["fit"]["waist_radius_mm"] == pytest.approx(WAIST)
    assert [p["row"] for p in record["points"]] == list(range(3,11))
    assert client.post(api,json={"source":{**source,"sheet":"gone"}}).status_code == 422


def test_frozen_fit_predicts_then_scores_external_rows_in_the_instrument_units(client):
    case = experiment(client, "predict before opening the next scan")
    api = f"/api/cases/{case}/beam-studies"
    source = mapping(attach(client,case,"calibration.csv",csv_file()))
    study = client.post(api,json={"source":source}).json()
    predict = api + f"/{study['id']}/predict"
    initial = client.post(predict,json={"positions_mm":[45,110]}).json()
    assert initial["predictions"][0]["diameter_mm"] == pytest.approx(2*WAIST)
    assert [p["extrapolated"] for p in initial["predictions"]] == [False, True]
    assert len(client.get(api).json()) == 1
    raw = f"run;cm;um\nB;4;{2000*WAIST+12}\n".encode()
    test = {**source,"attachment_id":attach(client,case,"later.csv",raw),"conditions":"B"}
    reference = {"study_id":study["id"],"note":"Same axis and medium. New origin is 5 mm downstream.","z_offset_mm":5}
    args = {"source":test,"reference_fit":reference}
    result = client.post(api,json=args).json()
    assert "fit" not in result and result["check"]["reference_study_id"] == study["id"]
    assert result["points"][0]["width_residual"] == pytest.approx(12)
    assert result["check"]["radius_rms_mm"] == pytest.approx(.006)
    assert result["points"][0]["use"] == "scored"
    assert result["points"][0]["model_z_mm"] == 45
    assert client.post(api,json=args).json()["id"] == result["id"]
    assert client.get(api,params={"study_id":study["id"]}).json() == study
    assert client.post(predict,json={"positions_mm":[45,110]}).json() == initial
    for patch in [{"axis":"Y"},{"medium_n":1.5},{"wavelength_nm":780},{"width_definition":"radius_1e2"}]:
        assert client.post(api,json={**args,"source":{**test,**patch}}).status_code == 422
    assert client.post(api,json={**args,"reference_fit":{**reference,"study_id":result["id"]}}).status_code == 422
    assert client.post(api+f"/{result['id']}/predict",json={"positions_mm":[45]}).status_code == 422
    other = experiment(client, "private")
    assert client.post(predict.replace(f"/cases/{case}/",f"/cases/{other}/"),json={"positions_mm":[45]}).status_code == 422
    copy = attach(client,case,"copy.csv",csv_file())
    reuse = client.post(api,json={**args,"source":{**source,"attachment_id":copy,"sheet":"unused for CSV","first_row":3,"last_row":3}}).json()
    assert reuse["status"]=="partial" and reuse["check"]["counts"]=={"fit_input":1}
    assert reuse["check"]["radius_rms_mm"] is None


def test_saved_forecast_pins_values_and_archive_contents_across_requests(client):
    case = experiment(client,"frozen, then revealed")
    api = f"/api/cases/{case}/beam-studies"
    source = mapping(attach(client,case,"base.csv",csv_file()))
    fit = client.post(api,json={"source":source}).json()["id"]
    prior_file = attach(client,case,"already.csv",b"scan;cm;um\nA;2;250\n")
    request = {"source":{"kind":"prediction","study_id":fit,"positions_mm":[20,55]}}
    forecast = client.post(api,json=request).json()
    assert forecast["recorded_at"] and len(forecast["archive_snapshot"])==2
    later_file = attach(client,case,"new.csv",b"scan;cm;um\nB;2;245\n")
    assert client.post(api,json=request).json()==forecast
    basis = {"study_id":forecast["id"],"note":"Axes and datum correspond to same air-free interval."}
    def compare(file, **changes):
        return client.post(api,json={"source":{**source,"attachment_id":file},"reference_fit":{**basis,**changes}})
    result = compare(later_file).json()
    assert result["points"][0]["predicted_radius_mm"] == forecast["predictions"][0]["radius_mm"]
    assert result["check"]["archive_evidence"][0]["state"] == "added_later"
    assert result["check"]["forecast_id"]==forecast["id"]
    assert compare(prior_file).json()["check"]["archive_evidence"][0]["state"] == "already_present"
    copied = attach(client,case,'same-original.csv',b"scan;cm;um\nA;2;250\n")
    assert compare(copied).json()["check"]["archive_evidence"][0]["state"] == "already_present"
    wrong_station = compare(later_file,z_offset_mm=1)
    assert wrong_station.status_code == 422 and "已存预测" in wrong_station.text
    assert client.get(api,params={"study_id":forecast["id"]}).json()==forecast


@pytest.mark.parametrize("legacy", [False, True])
def test_branch_fit_uses_frozen_shared_material_even_if_main_index_is_unknown(client, db, legacy):
    case = experiment(client, "visible branch")
    file = attach(client,case,"camera.csv",csv_file())
    planes = [{"uid":f"p{i}","name":str(z),"target_uid":"glass","depth_mm":float(z)} for i,z in enumerate(STATIONS)]
    sample = {"uid":"glass","name":"sample","kind":"space","length_mm":100,"n":None,
              "index_reference":{"model":"nbk7","wavelength_nm":250}}
    project = {"title":"双波长测量","system":{"mode":"single_pass","input_radius_mm":.25,"wavelength_nm":250,"elements":[sample]},
               "observation_planes":planes,"incident_paths":[{"uid":"arm","name":"测量支路","target_uid":"glass","through_uid":"glass",
                 "wavelength_nm":WAVELENGTH,"tangential":{"radius_mm":.25,"wavefront":"waist","m2":1}}]}
    base = f"/api/cases/{case}/optical-project"
    saved = client.post(base,json={"project":project,"change_note":"保存测量位置"}).json()
    assert saved["project"]["system"]["elements"][0]["n"] is None
    revision = saved["revision_id"]
    if legacy:
        archived = db.get(GeneratedContent, revision)
        content = deepcopy(archived.content)
        content["analysis"]["sections"]["paths"][0]["shared_route"] = [{"element_uid":"glass","n":None,"z_mm":0}]
        archived.content = content
        db.commit()
    ids = []
    for plane,z in zip(planes,STATIONS,strict=True):
        reading = {"revision_id":revision,"plane_uid":plane["uid"],"beam_uid":"arm","attachment_id":file,
                   "locator":plane["uid"],"conditions":"A","wavelength_nm":WAVELENGTH,"definition":"d4sigma_diameter",
                   "axes_aligned":True,"unit":"um","tangential":2000*width(z)}
        ids.append(client.post(base+"/beam-readings",json=reading).json()["id"])
    fit = client.post(f"/api/cases/{case}/beam-studies",json={"source":{"kind":"registered","revision_id":revision,
                      "axis":"tangential","reading_ids":ids}})
    assert fit.status_code == 200, fit.text
    expected_index = None if legacy else pytest.approx(n_real("nbk7",WAVELENGTH))
    assert fit.json()["metadata"]["medium_n"] == expected_index
    assert fit.json()["fit"]["waist_z_mm"] == pytest.approx(45)


def test_versioned_observations_use_measured_width_and_reject_intervening_lens(client):
    case = experiment(client, "registered")
    file = attach(client,case,"camera.csv",csv_file())
    system = {"mode":"single_pass","input_radius_mm":.25,"wavelength_nm":WAVELENGTH,"elements":[
        {"uid":"first","kind":"space","n":N,"length_mm":100,"name":"beam rail"},
        {"uid":"lens","kind":"lens","focal_mm":30,"name":"lens"},
        {"uid":"after","kind":"space","n":N,"length_mm":10,"name":"after"}]}
    planes = [{"uid":f"p{i}","name":f"p{i}","target_uid":"first","depth_mm":float(z)} for i,z in enumerate(STATIONS)]
    planes.append({"uid":"later","name":"after lens","target_uid":"after","depth_mm":2})
    base=f"/api/cases/{case}/optical-project"
    revision=client.post(base,json={"project":{"title":"rail","system":system,"observation_planes":planes},"change_note":"setup"}).json()["revision_id"]
    def read(plane, value, **kwargs):
        args={"revision_id":revision,"plane_uid":plane,"beam_uid":"primary","attachment_id":file,"locator":plane,
              "conditions":"A","wavelength_nm":WAVELENGTH,"definition":"d4sigma_diameter","axes_aligned":True,"unit":"um","tangential":2000*value}
        return client.post(base+"/beam-readings",json={**args,**kwargs}).json()["id"]
    ids = [read(p["uid"],width(z)) for p,z in zip(planes,STATIONS)]
    api = f"/api/cases/{case}/beam-studies"
    def fit(numbers):
        return client.post(api,json={"source":{"kind":"registered","revision_id":revision,"axis":"tangential","reading_ids":numbers}})
    stored=fit(ids).json()
    assert stored["fit"]["waist_z_mm"] == pytest.approx(45)
    assert {p["reading_id"] for p in stored["points"]} == set(ids)
    assert fit(ids[::-1]).json()["id"] == stored["id"]
    blind = [read(p["uid"],width(z),wavelength_nm=None,conditions="unlabelled") for p,z in zip(planes,STATIONS)]
    unsigned = fit(blind).json()
    assert unsigned["fit"]["m2"] is None and unsigned["metadata"]["medium_n"] is None
    assert unsigned["fit"]["waist_z_mm"] == pytest.approx(45)
    assert "model" not in unsigned["comparison"]
    assert "光学元件" in fit(ids+[read("later",.2)]).text
    assert fit(ids+[read("p0",.2,conditions="B")]).status_code == 422
    assert fit(ids+[read("p0",.2,definition="radius_1e2")]).status_code == 422
    check_source = {"kind":"registered","revision_id":revision,"axis":"tangential","reading_ids":[read("p1",.2,conditions="B")]}
    params = {"source":check_source,"reference_fit":{"study_id":stored["id"],"note":"Same rail and mounted direction; second acquisition."}}
    # Both records point to the same archived bytes and locator; changing the run label alone cannot create new evidence.
    assert client.post(api,json=params).json()["check"]["counts"]=={"fit_input":1}
    beyond = {**check_source,"reading_ids":[read("later",.2,conditions="B")]}
    assert client.post(api,json={**params,"source":beyond}).status_code == 422
    assert client.post(api+f"/{stored['id']}/predict",json={"positions_mm":[105]}).status_code == 422

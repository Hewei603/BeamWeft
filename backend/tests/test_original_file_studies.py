from io import BytesIO
import hashlib
import struct
import zlib

import numpy as np
from PIL import Image
from pydantic import RootModel
import pytest

from app.study_api import OriginalFile, StrictInput, Study
from app.workbench import tools
from app.workbench.discovery import CapabilityDirectory, CapabilitySearch


def case(client):
    cid = client.post("/api/cases", json={"title":"仪器原件计算"}).json()["id"]
    return f"/api/cases/{cid}"


def archive(client, root, name, raw):
    response = client.post(root+"/attachments", files={"file":(name,raw,"application/octet-stream")})
    assert response.status_code in (200,201), response.text
    return {"attachment_id":response.json()["id"], "sha256":hashlib.sha256(raw).hexdigest()}


def run(client, root, name, inputs):
    study, provider = tools.STUDIES[name]
    return client.post(root+"/studies/run/"+name,json={"version":study.version,"provider":provider,"inputs":inputs})


def tiff(*frames):
    images = [Image.fromarray(frame) for frame in frames]
    out = BytesIO()
    images[0].save(out, format="TIFF", save_all=True, append_images=images[1:])
    return out.getvalue()


def npy_frame(values):
    stream = BytesIO()
    np.save(stream,values,allow_pickle=False)
    return stream.getvalue()


@pytest.mark.parametrize("array_signal", [True,False])
def test_cross_format_camera_and_dark_preserve_signed_values_and_real_pixel_coordinates(client,array_signal):
    root = case(client)
    image = np.full((9,11),60,dtype=np.uint16)
    dark = np.full_like(image,60)
    for row,col,count in [(2,4,9),(4,4,9),(3,3,9),(3,5,9),(3,4,5),(2,3,-1)]:
        image[row,col] += np.int32(count)
    # The direct NPY values keep their original endian, stride order and detector indexing.
    def source(a, array, stem):
        if array:
            return archive(client,root,stem+".npy",npy_frame(np.asfortranarray(a.astype(">u2"))))
        return archive(client,root,stem+".tif",tiff(a))
    signal = source(image,array_signal,"signal")
    blank = source(dark,not array_signal,"dark")
    args = {"image":signal,"dark":blank,"window":[2,1,6,6],"pitch_x_um":2.5,"pitch_y_um":4,
            "response":"linear","basis":"同一相机、像元、增益与读数单位的信号和暗帧。"}
    response = run(client,root,"measure_image_moments",args)
    assert response.is_success,response.text
    result = response.json()
    correct = (image.astype(float)-dark)[1:7,2:8]
    np.testing.assert_array_equal(result["intensity_map"]["values"],correct)
    assert result["negative_pixels"] == 1 and result["sum"] == 40 and result["background"]["assumed_same_units"]
    assert result["intensity_map"]["x_edges_mm"][0] == pytest.approx((2-.5)*.0025)
    assert result["center_y_px"] == pytest.approx(np.arange(1,7)@correct.sum(axis=1)/40)
    original = result["source"] if array_signal else result["background"]["source"]
    assert original["format"] == "NPY" and original["size"] == [11,9] and "encoded_max" not in original
    assert {f["sha256"] for f in result["study_record"]["originals"]} == {signal["sha256"],blank["sha256"]}
    with_note = archive(client,root,"dark-float.npy",npy_frame(dark.astype("float32")))
    unverified = run(client,root,"measure_image_moments",{**args,"dark":with_note})
    assert unverified.status_code == 422 and "读数单位" in unverified.text
    registered = run(client,root,"measure_image_moments",{**args,"dark":with_note,"dark_units_note":"同次导出原始 DN，仅更改浮点存储。"})
    assert registered.is_success, registered.text
    assert registered.json()["background"]["unit_registration"] and registered.json()["sum"] == 40


def test_float_camera_roi_keeps_noise_without_inferring_adc_or_a_missing_frame(client):
    root = case(client)
    data = np.array([[np.nan,0,0],[0,-.125,.75],[0,.25,1.5]],dtype=">f4")
    ref = archive(client,root,"capture.bin",npy_frame(data))
    args = {"image":ref,"window":[1,1,2,2],"background_level":0,"basis":"线性浮点帧，仅中央窗口有效。",
            "response":"linear","pitch_x_um":3,"pitch_y_um":3}
    reply = run(client,root,"measure_image_moments",args)
    assert reply.is_success,reply.text
    record = reply.json()
    np.testing.assert_allclose(record["intensity_map"]["values"],data[1:,1:])
    assert "encoding_ceiling_pixels" not in record and "saturated_pixels" not in record
    assert record["negative_pixels"] == 1 and "encoded_max" not in record["source"]
    for response in ("nonlinear","unverified"):
        uncalibrated = run(client,root,"measure_image_moments",{**args,"response":response}).json()
        assert "intensity_map" not in uncalibrated and not uncalibrated["study_record"]["available_outputs"]
    assert run(client,root,"measure_image_moments",{**args,"frame":2}).status_code == 422
    assert run(client,root,"measure_image_moments",{**args,"window":None}).status_code == 422
    for array in (np.array([[1j]]), np.zeros((2,4,6)), np.array([["angle"]])):
        file = archive(client,root,"non-camera.npy",npy_frame(array))
        found = client.post(root+"/capabilities/search",json={"attachment_id":file["attachment_id"],"limit":20}).json()
        assert "measure_image_moments" not in {item["name"] for item in found["items"]}
        assert run(client,root,"measure_image_moments",{**args,"image":file,"window":None}).status_code == 422


def test_attachment_search_distinguishes_column_mapping_from_whole_file_readers(client):
    root = case(client)
    def methods(filename, raw, **search):
        source = archive(client, root, filename, raw)
        response = client.post(root+"/capabilities/search", json={"attachment_id": source["attachment_id"],
                               "query": "fit_beam_caustic", **search})
        assert response.status_code == 200
        return response.json()

    page = methods("readings.csv", b"station,label\n125,A\n200,B\n")
    fit = next(item for item in page["items"] if item["name"] == "fit_beam_caustic")
    assert fit["original_match"] == "table_columns" and "original_inputs" not in fit
    assert fit["table_column_inputs"]["acquisition_ids"]["type"] == "text"
    assert fit["table_column_inputs"]["z_mm"] == {"type": "numeric", "label": "轴向位置", "unit": "mm"}
    assert page["table_inspection_action"] == "inspect_measurement_table"
    assert page["connection_action"] == "run_connected_study"
    assert any("table_column_inputs" in item for item in methods("labels.txt", b"lot\tlabel\nA\tleft\nB\tright\n")["items"])
    assert all("table_column_inputs" not in item for item in methods("notes.txt", b"no readings were taken today")["items"])
    whole = methods("whole.csv", b"frame\na\nb\n", original_files=True)
    assert all("table_column_inputs" not in item for item in whole["items"])
    image = BytesIO()
    Image.new("L", (3, 3)).save(image, format="PNG")
    for name in ("image.csv", "figure.xlsx"):
        assert all("table_column_inputs" not in item for item in methods(name, image.getvalue())["items"])


def test_discover_readers_by_case_bytes_with_ambiguous_and_unknown_roles(client, db):
    root = case(client)
    other = case(client)
    calls = []

    class Acquisition(OriginalFile):
        @classmethod
        def recognizes(cls, prefix, *, complete):
            calls.append((len(prefix), complete))
            return prefix.startswith(b"ACQ1")

    class Calibration(OriginalFile):
        @classmethod
        def recognizes(cls, prefix, *, complete):
            return prefix.startswith(b"CAL1")

    class BrokenReader(OriginalFile):
        @classmethod
        def recognizes(cls, prefix, *, complete):
            raise ValueError("unavailable")

    class Block(StrictInput):
        samples: list[Acquisition]
    class Pair(StrictInput):
        input: Block
        reference: Acquisition | Calibration | None = None
    class Gain(StrictInput):
        gain: Calibration
    class Generic(StrictInput):
        input: OriginalFile
    class Broken(StrictInput):
        input: BrokenReader

    studies = {key: (Study(key,key,"1","test",model,lambda _: {}),"test")
               for key,model in (("pair",Pair),("gain",Gain),("legacy",Generic),("broken",Broken))}
    directory = CapabilityDirectory({k:(s.inputs,s.scope) for k,(s,_) in studies.items()}, studies, {})
    item = archive(client,root,"unhelpful.png",b"ACQ1"+bytes(70000))
    q = CapabilitySearch(attachment_id=item["attachment_id"],limit=20)
    found = directory.search_case(db,int(root.split("/")[-1]),q)["items"]
    assert [(r["name"],r["original_match"]) for r in found] == [
        ("pair","recognized"), ("broken","unverified"), ("legacy","unverified")]
    assert found[0]["original_inputs"] == {"input":"recognized","reference":"recognized"}
    assert calls == [(65536, False)]
    assert client.post(other+"/capabilities/search",json=q.model_dump()).status_code == 422


def test_recognized_image_header_preserves_full_measurement_checks(client):
    root=case(client)
    out=BytesIO()
    Image.new("RGB",(8,8)).save(out,format="PNG")
    ref=archive(client,root,"renamed.dat",out.getvalue())
    found=client.post(root+"/capabilities/search",json={"attachment_id":ref["attachment_id"]}).json()["items"]
    method=next(m for m in found if m["name"]=="measure_image_moments")
    assert method["original_inputs"] == {"image":"recognized", "dark":"recognized"}
    result=run(client,root,"measure_image_moments", {"image":ref, "basis":"uncalibrated color record"})
    assert result.status_code==422 and "单通道" in result.json()["detail"]


def test_original_paths_address_the_serialized_inputs_for_maps_and_root_models(client, monkeypatch):
    class Scan(RootModel[list[OriginalFile]]):
        pass
    class Series(StrictInput):
        stages: dict[int, Scan]
    study=Study("repeats","repeats","1","test",Series,lambda data: {"n":len(data.stages[5].root)})
    monkeypatch.setitem(tools.STUDIES,study.name,(study,"lab"))
    root=case(client)
    sample=archive(client,root,"sample.bin",b"example")
    seen=run(client,root,study.name,{"stages":{"5":[sample,sample]}}).json()["study_record"]
    assert seen["originals"][0]["input_paths"] == [["stages","5",0],["stages","5",1]]
    frozen=client.get(root+"/studies/runs/"+str(seen["id"])).json()["inputs"]
    for path in seen["originals"][0]["input_paths"]:
        ref=frozen
        for key in path:
            ref=ref[key]
        assert ref==sample


def test_original_frame_dark_and_anisotropic_measurement_plane(client):
    root = case(client)
    base = np.full((13,18), 4100, dtype=np.uint16)
    signal = base.copy()
    for x,y,w in [(2,1,4),(-2,-1,4),(-1,2,1),(1,-2,1)]:
        signal[6+y,9+x] += 100*w
    img=archive(client,root,"signal.tif",tiff(base,signal))
    dark=archive(client,root,"reference.tif",tiff(base))
    args={"image":img,"frame":2,"dark":dark,"window":[5,2,10,9],"pitch_x_um":3,"pitch_y_um":5,
          "response":"linear", "basis":"合成线性 16 位图，两帧采集和独立暗场。","saturation_level":4500}
    res=run(client,root,"measure_image_moments",args)
    assert res.status_code==200,res.text
    data=res.json()
    assert [data["center_x_px"],data["center_y_px"]]==[9,6]
    assert data["sum"]==1000 and data["saturated_pixels"]==2
    assert data["axes_px"]==pytest.approx({"major":8,"minor":4,"angle_deg":26.5650511771})
    lo, hi = np.linalg.eigvalsh([[30.6,18],[18,40]])
    assert [data["axes_um"]["major"],data["axes_um"]["minor"]]==pytest.approx(4*np.sqrt([hi,lo]))
    assert data["grid"]["x_edges"][0]==4.5 and data["grid"]["y_edges"][-1]==10.5
    receipt=data["study_record"]
    assert set(receipt["available_outputs"])=={"axes_um.major","axes_um.minor","diameter_x_um","diameter_y_um","intensity_map","centroid"}
    centroid = data["centroid"]
    assert [centroid["x_mm"], centroid["y_mm"]] == pytest.approx([.027, .030])
    assert np.array(centroid["covariance_mm2"]) == pytest.approx(np.array([[30.6,18],[18,40]]) / 1e6)
    assert centroid["quality"] == "limited" and centroid["window"] == args["window"]
    calibrated = data["intensity_map"]
    assert calibrated['quality'] == 'limited'
    assert calibrated["x_edges_mm"] == pytest.approx((np.arange(5,16)-.5)*.003)
    assert calibrated["y_edges_mm"] == pytest.approx((np.arange(2,12)-.5)*.005)
    assert np.asarray(calibrated["values"]) == pytest.approx(signal[2:11,5:15] - base[2:11,5:15])
    assert calibrated['y_down'] and any('饱和' in warning for warning in calibrated['warnings'])
    assert {f["sha256"] for f in receipt["originals"]}=={img["sha256"],dark["sha256"]}
    assert run(client,root,"measure_image_moments",{**args,"image":{"attachment_id":img["attachment_id"]}}).json()["study_record"]["id"]==receipt["id"]
    empty=run(client,root,"measure_image_moments",{**args,"frame":1}).json()
    assert empty["status"]=="partial" and set(empty["study_record"]["available_outputs"])=={'intensity_map'}
    assert not np.count_nonzero(empty['intensity_map']['values']) and 'center_x_px' not in empty
    single_axis = run(client, root, "measure_image_moments", {**args,"pitch_x_um":None}).json()
    assert single_axis['diameter_y_um'] == data['diameter_y_um'] and 'axes_um' not in single_axis
    assert set(single_axis['study_record']['available_outputs']) == {'diameter_y_um'}
    for changes in ({"frame":3},{"window":[12,0,9,2]},{"dark":None,"dark_frame":2},{"background_level":0}):
        assert run(client,root,"measure_image_moments",{**args,**changes}).status_code==422
    rgb=archive(client,root,"color.tif",tiff(np.zeros((3,4,3),dtype=np.uint8)))
    assert "单通道" in run(client,root,"measure_image_moments",{**args,"image":rgb,"frame":1}).text


def test_signed_background_samples_contribute_to_moments(client):
    root=case(client)
    image=archive(client,root,"signed.tiff",tiff(np.array([[12,9,12],[12,12,12],[12,9,12]],dtype=np.uint16)))
    data=run(client,root,"measure_image_moments",{"image":image,"background_level":10,"basis":"固定窗，暗电平均值 10"}).json()
    assert data["negative_pixels"]==2 and data["sum"]==12
    assert np.asarray(data["covariance_px2"])==pytest.approx(np.diag([1.,.5]))
    assert not data["study_record"]["available_outputs"]


@pytest.mark.parametrize("old_version", ["1", "2"])
def test_archive_replay_reads_original_and_keeps_unknown_response(client, db, old_version):
    from app.models import GeneratedContent
    root = case(client)
    image = archive(client,root,'archived.tif',tiff(np.array([[2,4],[4,2]], dtype=np.uint16)))
    args = {'image':image, 'background_level':0, 'pitch_x_um':8, 'pitch_y_um':8, 'basis':'旧采集记录，未核实响应'}
    study, provider = tools.STUDIES['measure_image_moments']
    old = GeneratedContent(case_id=int(root.rsplit('/',1)[1]), content_type='scientific_study', content={
        'definition':{'name':study.name,'version':old_version,'provider':provider,'title':study.title},
        'inputs':args,'connections':{},'output':{'sum':12,'status':'completed'},'available_outputs':{}})
    db.add(old)
    db.commit()
    request = {'version':study.version,'provider':provider,'inputs':{},'reuse_inputs_from':old.id}
    revived = client.post(root+'/studies/run/'+study.name,json=request)
    assert revived.status_code == 200, revived.text
    result = revived.json()
    assert result['sum'] == 12 and result['response'] == 'unverified' and not result['study_record']['available_outputs']
    assert result['study_record']['input_origin']['run_id'] == old.id
    assert result['study_record']['originals'][0]['sha256'] == image['sha256']
    historical = client.get(root+f'/studies/runs/{old.id}').json()
    assert historical['inputs'] == args and historical['output'] == {'sum':12, 'status':'completed'}


def test_signed_first_moment_and_width_have_independent_limits(client):
    root = case(client)
    # Oversubtraction at the outer pixels can invalidate the second moment while keeping the centroid in the window.
    source = archive(client, root, "negative-wings.tif", tiff(np.array([[9, 15, 9]], dtype=np.uint16)))
    args = {"image": source, "background_level": 10, "saturation_level": 65535, "response": "linear",
            "basis": "实验室暗电平", "pitch_x_um": 10, "pitch_y_um": 20}
    result = run(client, root, "measure_image_moments", args)
    assert result.is_success, result.text
    data = result.json()
    assert data["status"] == "partial" and data["center_x_px"] == 1.0 and data["center_y_px"] == 0.0
    assert data["centroid"]["quality"] == "limited" and "covariance_mm2" not in data["centroid"]
    assert set(data["study_record"]["available_outputs"]) == {"intensity_map", "centroid"}
    # A positive net signal can still give a signed centroid outside the physical window.
    source = archive(client, root, "outside.tif", tiff(np.array([[8, 13]], dtype=np.uint16)))
    limited = run(client, root, "measure_image_moments", {**args, "image": source})
    assert limited.is_success, limited.text
    output = limited.json()
    assert output["sum"] == 1 and "centroid" not in output
    assert "选区外" in output["output_assessments"]["centroid"]["message"]


@pytest.mark.parametrize('plain', [True, False])
def test_native_pgm_samples_and_range_survive_archive_preview_and_dark_subtraction(client, plain):
    root = case(client)
    counts = np.array([[101,99,480],[1023,4095,300]], dtype='>u2')
    header = (b'P2' if plain else b'P5') + b'\n# linear 12 bit detector\n3 2\n4095\n'
    payload = (b'101\t99 480 # split line\n1023 4095 300\n' if plain else counts.tobytes())
    second = b'P5\n3 2\n4095\n' + np.full((2,3),100,dtype='>u2').tobytes()
    original = header + payload + (b'' if plain else second)
    img = archive(client, root, 'camera.raw' if plain else 'sequence.pgm', original)
    path = f"/api/attachments/{img['attachment_id']}"
    assert client.get(path).content == original
    assert client.get(path+'/preview').json()['image_url'].startswith('data:image/png;base64,')
    args = {'image':img, 'basis':'线性传感器，标定面 4 µm × 5 µm。', 'response':'linear', 'saturation_level':4000,
            'pitch_x_um':4, 'pitch_y_um':5}
    if plain:
        args['background_level'] = 100
    else:
        args.update(dark=img, dark_frame=2)
    data = run(client, root, 'measure_image_moments', args).json()
    assert data['source']['encoded_max'] == 4095 and data['saturated_pixels'] == 1
    assert np.asarray(data['intensity_map']['values']) == pytest.approx(counts.astype(float)-100)
    assert data['negative_pixels'] == 1
    assert data['intensity_map']['x_edges_mm'] == pytest.approx([-.002,.002,.006,.010])
    assert data['sum'] == counts.sum()-600
    record = client.get(root+f"/studies/runs/{data['study_record']['id']}").json()
    assert record['originals'][0]['sha256'] == img['sha256']
    assert record['output']['intensity_map'] == data['intensity_map']


def test_large_map_conserves_signed_edge_bins_and_requires_explicit_response(client):
    root = case(client)
    counts = np.full((11,520),100,dtype=np.uint16)
    counts[1:10,3:517] += 2
    counts[4,3] = 40
    counts[4,515] = 30000
    args = {'image':archive(client,root,'frame.tif',tiff(counts)), 'basis':'unknown response, calibrated detector pitch',
            'pitch_x_um':4,'pitch_y_um':6,'window':[3,1,514,9],'background_level':100}
    for value in ['unverified','nonlinear']:
        data = run(client,root,'measure_image_moments',{**args,'response':value}).json()
        assert data['status']=='partial' and data['study_record']['available_outputs']=={}
        assert data['output_assessments']['intensity_map']['state']=='limited'
    data = run(client,root,'measure_image_moments',{**args,'response':'assumed_linear'}).json()
    state = data['intensity_map']
    net = counts[1:10,3:517].astype(float) - 100
    analytic = np.column_stack([net[:,:513].reshape(9,171,3).sum(axis=2),net[:,-1]])
    assert np.asarray(state['values']) == pytest.approx(analytic)
    assert state['warnings'] and any('暂按' in note for note in state['warnings'])
    assert state['quality'] == 'conditional' and state['values'][0][-1] == net[0,-1]
    assert [state['x_edges_mm'][0],state['x_edges_mm'][-1]] == pytest.approx([2.5*.004,516.5*.004])


def test_tiff_orientation_is_undone_before_sensor_roi_and_dark_registration(client):
    root = case(client)
    dark = np.arange(30, dtype=np.uint16).reshape(5,6)
    counts = dark + np.arange(30,60,dtype=np.uint16).reshape(5,6)
    def instrument_frame(array, orientation):
        buffer=BytesIO()
        Image.fromarray(array).save(buffer, format='TIFF', tiffinfo={274:orientation})
        return archive(client,root,f'camera-{orientation}.tif',buffer.getvalue())
    args = {'image':instrument_frame(counts,6), 'dark':instrument_frame(dark,2), 'response':'linear',
            'pitch_x_um':4,'pitch_y_um':7,'basis':'native sensor columns/rows, independently oriented display metadata',
            'window':[2,1,3,2], 'wavelength_nm':780, 'plane':'sensor behind stop'}
    data = run(client,root,'measure_image_moments',args).json()
    state = data['intensity_map']
    assert np.asarray(state['values']) == pytest.approx((counts-dark)[1:3,2:5])
    assert state['x_edges_mm'] == pytest.approx([.006,.010,.014,.018])
    assert state['y_edges_mm'] == pytest.approx([.0035,.0105,.0175])
    assert state['wavelength_nm'] == 780 and state['plane'] == 'sensor behind stop'
    assert data['source']['display_orientation_tag'] == 6


def test_grayscale_png_packed_codes_keep_camera_units(client):
    def chunk(tag, data):
        return struct.pack('>I',len(data))+tag+data+struct.pack('>I',zlib.crc32(tag+data))
    # Two rows of 2-bit grayscale values, stored as four codes per byte.
    png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR',struct.pack('>IIBBBBB',4,2,2,0,0,0,0)) +
           chunk(b'IDAT', zlib.compress(b'\0\x1b\0\xe4')) + chunk(b'IEND',b''))
    root = case(client)
    args = {'image':archive(client,root,'packed.png',png), 'background_level':1, 'response':'linear',
            'pitch_x_um':8, 'pitch_y_um':8, 'basis':'test of packed native codes', 'saturation_level':3}
    data = run(client,root,'measure_image_moments',args).json()
    assert data['saturated_pixels'] == 2
    assert data['source']['encoded_max'] == 3
    assert data['intensity_map']['values'] == [[-1,0,1,2],[2,1,0,-1]]
    impossible = run(client,root,'measure_image_moments',{**args,'saturation_level':255}).json()
    assert impossible['saturation_threshold_out_of_range'] and impossible['encoding_ceiling_pixels'] == 2
    assert impossible['intensity_map']['quality'] == 'limited'


def test_external_binary_reader_uses_nested_case_files_and_validates_historical_identity(client, monkeypatch):
    class Sequence(StrictInput):
        blocks: list[OriginalFile]

    streams=[]
    def compute(args):
        streams.extend(b.stream for b in args.blocks)
        assert all(b.relative_path.endswith(".bin") for b in args.blocks)
        return {"counts":[struct.unpack("<I",b.stream.read(4))[0] for b in args.blocks]}

    study=Study("count_records","独立计数器","1","每块为 little-endian uint32。",Sequence,compute)
    monkeypatch.setitem(tools.STUDIES,study.name,(study,"test-lab"))
    root,other=case(client),case(client)
    file=archive(client,root,"data.bin",struct.pack("<I",98201))
    second=archive(client,root,"other.bin",struct.pack("<I",42))
    response=run(client,root,study.name,{"blocks":[file,second,file]})
    data=response.json()
    assert data["counts"]==[98201,42,98201] and len(data["study_record"]["originals"])==2
    assert streams[0] is not streams[2] and streams[0].name == streams[2].name
    assert [f["input_paths"] for f in data["study_record"]["originals"]] == [
        [["blocks",0],["blocks",2]], [["blocks",1]]]
    assert all(stream.closed for stream in streams)
    assert run(client,other,study.name,{"blocks":[file]}).status_code==422
    assert run(client,root,study.name,{"blocks":[{**file,"sha256":"0"*64}]}).status_code==422
    stored=client.get(root+"/studies/runs/"+str(data["study_record"]["id"])).json()
    assert stored["inputs"]["blocks"]==[file,second,file] and stored["output"]=={"counts":[98201,42,98201]}
    short=archive(client,root,"truncated.bin",b"\x00")
    failed=run(client,root,study.name,{"blocks":[short]})
    assert failed.status_code==500 and study.title in failed.text and "test-lab" in failed.text
    assert all(stream.closed for stream in streams)
    assert len(client.get(root+"/studies/runs").json()["items"])==1

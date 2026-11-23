"""Analytic aperture and tilted-wave references, using the registered optical workflow."""
from io import BytesIO
import hashlib
import json
import math

import numpy as np
import pytest

from app.physics.focal_plane import focal_plane
from app.workbench.tools import STUDIES


@pytest.mark.parametrize("shape,padding", [((13,17),3),((10,12),2)])
def test_rectangular_pupil_matches_separable_finite_aperture_and_coordinate_quadrature(shape,padding):
    h, w = shape
    pupil = np.zeros(shape,dtype=complex)
    pupil[2:7,4:11] = 1.45*np.exp(.312j)
    dx, dy, lam, f = .012, .017, .00059/1.4, 85
    plane = focal_plane(pupil,dx_mm=dx,dy_mm=dy,wavelength_mm=lam,focal_mm=f,padding=padding)
    # A sampled rectangular aperture has a geometric-series transform; its continuum limit is sinc.
    def squared_sum(n, occupied):
        frequencies = (np.arange(n)-n//2)/n
        return (occupied*np.sinc(occupied*frequencies)/np.sinc(frequencies))**2
    ny, nx = h*padding, w*padding
    expected = 1.45**2*dx*dy/(ny*nx)*np.outer(squared_sum(ny,5),squared_sum(nx,7))
    np.testing.assert_allclose(plane.cell_integrals,expected,rtol=4e-13,atol=2e-18)
    ux = (plane.x_edges_mm[1:]+plane.x_edges_mm[:-1])/2
    uy = (plane.y_edges_mm[1:]+plane.y_edges_mm[:-1])/2
    np.testing.assert_allclose(ux,(np.arange(nx)-nx//2)*lam*f/(nx*dx), atol=2e-15)
    np.testing.assert_allclose(uy,(np.arange(ny)-ny//2)*lam*f/(ny*dy), atol=2e-15)
    assert plane.metrics["dc_fraction"] == pytest.approx(35/(nx*ny))
    assert plane.cell_integrals.sum() == pytest.approx(35*1.45**2*dx*dy)
    assert plane.metrics["parseval_relative_error"] < 1e-14
    assert plane.metrics["peak_x_mm"] == plane.metrics["peak_y_mm"] == 0


def _upload(client, root, name, values):
    file = BytesIO()
    np.save(file,values,allow_pickle=False)
    data = file.getvalue()
    response = client.post(root+"/attachments",files={"file":(name,data,"application/octet-stream")})
    assert response.is_success, response.text
    return {"attachment_id":response.json()["id"],"sha256":hashlib.sha256(data).hexdigest()}


def _case(client):
    case = client.post("/api/cases",json={"title":"入瞳与焦面配准"}).json()["id"]
    root = f"/api/cases/{case}"
    parameters = {"wavelength_nm":650,"focal_length_mm":80,"pitch_x_um":12,"pitch_y_um":15,
                  "row_direction":"positive_y","basis":"入瞳同面复场，两个像素方向和间距分别标定。"}
    study, provider = STUDIES["lens_focal_intensity"]
    def run(**args):
        return client.post(root+"/studies/run/"+study.name,json={"version":study.version,"provider":provider,
                                                                   "inputs":{**parameters,**args}})
    return root, run


def test_registered_complex_and_separately_encoded_layers_land_in_the_correct_quadrant(client):
    root, run = _case(client)
    ny,nx = 12,32
    row, col = np.indices((ny,nx))
    # A signed, travelling wave in +x and a distinct phase ramp in decreasing row direction.
    base = np.exp(2j*np.pi*3*col/nx)
    complex_input = _upload(client,root,"field.npy",base.astype(">c16"))
    encoded = (4-row)%ny
    phase = _upload(client,root,"phase.npy",encoded.astype("int16"))
    r = run(complex_field=complex_input,phase=phase,phase_scale_rad_per_unit=2*np.pi/ny,
            row_direction="negative_y",pupil_power_w=2.3)
    assert r.is_success, r.text
    result = r.json()
    assert result["peak_x_mm"] == pytest.approx(3*.000650*80/(nx*.012))
    assert result["peak_y_mm"] == pytest.approx(.000650*80/(ny*.015))
    assert result["dc_fraction"] < 1e-25 and result["peak_bin_count"] == 1
    assert result["output_integral"] == pytest.approx(2.3) and result["parseval_relative_error"] < 1e-14
    assert result["signal_unit"] == "W" and "W/mm²" in result["grid"]["density_label"]
    # Give the same pupil as linear intensity and independently calibrated phase originals.
    illumination = _upload(client,root,"intensity.npy",np.full((ny,nx),1.7**2))
    x_phase = _upload(client,root,"sampled-code.npy",np.asarray((3*col)%nx,dtype=np.uint16))
    second = run(amplitude=illumination,amplitude_kind="intensity",phase=x_phase,phase_scale_rad_per_unit=2*np.pi/nx,
                 phase_layers=[{"image":phase,"scale_rad_per_unit":2*np.pi/ny}],row_direction="negative_y",pupil_power_w=2.3)
    assert second.is_success,second.text
    np.testing.assert_allclose(second.json()["intensity_map"]["values"],result["intensity_map"]["values"],atol=1e-14)
    rec = client.get(root+f"/studies/runs/{second.json()['study_record']['id']}").json()
    assert {x["sha256"] for x in rec["originals"]} == {illumination["sha256"],x_phase["sha256"],phase["sha256"]}
    # Input amplitude-only data require a stated phase assumption. Conflicting phase origins are rejected.
    assert run(amplitude=illumination).status_code == 422
    assert run(complex_field=complex_input,zero_phase=True).status_code == 422
    assert run(uniform_amplitude=1,phase=phase).status_code == 422
    flat = run(amplitude=illumination,amplitude_kind="intensity",zero_phase=True).json()
    assert flat["dc_fraction"] == pytest.approx(1)
    assert flat["input_integral"] == pytest.approx(ny*nx*.012*.015*1.7**2)


def test_native_export_retains_bins_lost_to_display_binning(client):
    root, run = _case(client)
    ny, nx = 103,603
    row,col = np.indices((ny,nx))
    field = np.exp(2j*np.pi*(19*col/nx - 13*row/ny))
    original = _upload(client,root,"pupil.npy",field)
    reply = run(complex_field=original)
    assert reply.is_success, reply.text
    record = reply.json()
    assert record["fft_shape"] == [ny,nx] and record["preview_shape"] == [52,87]
    assert record["published_shape"] == [ny,302]
    integral = np.sum(record["intensity_map"]["values"])
    grid = record["grid"]
    display_integral = np.sum(np.asarray(grid["density"])*np.outer(np.diff(grid["y_edges_mm"]),np.diff(grid["x_edges_mm"])))
    assert integral == pytest.approx(ny*nx*.012*.015) and display_integral == pytest.approx(integral)
    native = client.get(root+f"/studies/runs/{record['study_record']['id']}/native-grid")
    assert native.is_success, native.text[:180] if not native.is_success else ""
    with np.load(BytesIO(native.content),allow_pickle=False) as package:
        values = package["cell_integrals"]
        assert values.shape == (ny,nx) and np.unravel_index(values.argmax(),values.shape) == (ny//2-13,nx//2+19)
        assert values.max() == pytest.approx(integral)
        assert package["grid_sha256"].item() == record["native_grid_sha256"]
        assert json.loads(package["provenance_json"].item())


def test_zero_field_and_unusable_grid_preserve_scientific_boundaries():
    kwargs = {"dx_mm":.01,"dy_mm":.015,"wavelength_mm":.0005,"focal_mm":55}
    zero = focal_plane(np.zeros((4,5)),**kwargs)
    assert zero.metrics["output_integral"] == 0 and zero.metrics["dc_fraction"] is None and "peak_x_mm" not in zero.metrics
    with pytest.raises(ValueError, match="零场"):
        focal_plane(np.zeros((3,4)),**kwargs,pupil_power_w=1)
    with pytest.raises(ValueError, match="补零后网格"):
        focal_plane(np.zeros((400,400)),**kwargs,padding=8)
    edge = np.broadcast_to((-1.)**np.arange(32),(31,32))
    result = focal_plane(edge,**{**kwargs,"dx_mm":.001})
    assert math.isclose(result.metrics["high_angle_fraction"],1) and result.warnings


def test_two_aperture_fringe_prediction_compares_to_a_separate_camera_npy(client):
    root, run = _case(client)
    ny, nx = 12,16
    pupil = np.zeros((ny,nx))
    pupil[4,[3,7]] = 1
    simulated = run(amplitude=_upload(client,root,"slits.npy",pupil),zero_phase=True).json()
    # Independent two-point interference gives |1+exp(-i 2pi k separation/N)|².
    fringes = 300 * (2+2*np.cos(2*np.pi*4*(np.arange(nx)-nx//2)/nx))
    data = np.tile(fringes+6.5,(ny,1))
    camera = _upload(client,root,"detector.npy",data)
    sx, sy = .000650*80/(nx*.012), .000650*80/(ny*.015)
    series, provider = STUDIES["measure_image_moments"]
    response = client.post(root+"/studies/run/"+series.name,json={"version":series.version,"provider":provider,"inputs":{
        "image":camera,"background_level":6.5,"response":"linear","pitch_x_um":sx*1000,"pitch_y_um":sy*1000,
        "wavelength_nm":650,"basis":"独立双孔解析干涉图，线性探测器读数。"}})
    assert response.is_success,response.text
    measured = response.json()
    method, provider = STUDIES["compare_intensity_maps"]
    match = "同一焦面。探测器的行列分别顺着焦面 +y 与 +x，中心像素在光轴上。"
    refs = {key:{"run_id":data["study_record"]["id"],"port":"intensity_map","registration":match}
            for key,data in (("reference",simulated),("observed",measured))}
    r = client.post(root+"/studies/run/"+method.name,json={"version":method.version,"provider":provider,"connections":refs,
        "inputs":{"offset_x_mm":-(nx//2)*sx,"offset_y_mm":-(ny//2)*sy,"rotation_deg":0,"flip_y":False,
                  "registration_basis":match}})
    assert r.is_success,r.text
    assert r.json()["rmse_density_mm2"] < 1e-12

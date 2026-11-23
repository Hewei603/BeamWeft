import math

import numpy as np
import pytest

from app.workbench.optics import analyze_system


def rod(cycles, gradient=.2):
    return {"kind":"grin","uid":"rod","name":"GRIN","n":1.5,"gradient_per_mm":gradient,
            "length_mm":cycles*math.pi/gradient}


def system(element, radius=.1, curvature=None, count=16):
    return {"mode":"single_pass","wavelength_nm":1064,"samples_per_segment":count,
            "input_radius_mm":radius,"input_curvature_mm":curvature,"elements":[element]}


def radius_at(z, radius, curvature, n=1.5, gradient=.2):
    # Propagate the second moments of position and reduced angle.
    a, b = math.cos(gradient*z), math.sin(gradient*z)/(n*gradient)
    return math.hypot(a*radius + b*radius*(1/curvature if curvature else 0), b*.001064/(math.pi*radius))


@pytest.mark.parametrize("curvature,count", [(None, 16),(-12, 2),(25,40)])
def test_resolve_periodic_width_and_off_grid_minima(curvature, count):
    element = rod(15)
    plane = analyze_system(system(element,curvature=curvature,count=count))["planes"]["tangential"]
    samples = plane["samples"]
    zs, widths = zip(*[(p["z_mm"],p["radius_mm"]) for p in samples],strict=True)
    assert not plane.get("profile_breaks") and not plane.get("profile_omissions")
    assert len(plane["waists"])==15 and len(zs)<=1024
    assert widths == pytest.approx([radius_at(z,.1,curvature) for z in zs],rel=1e-12)
    test_z = np.linspace(0,element["length_mm"],3031)
    exact = np.array([radius_at(z,.1,curvature) for z in test_z])
    assert np.max(abs(np.interp(test_z,zs,widths) / exact-1)) < .04
    for waist in plane["waists"]:
        z, r = waist["z_mm"], waist["radius_mm"]
        assert radius_at(z-.01,.1,curvature) > r < radius_at(z+.01,.1,curvature)


def test_dense_interior_keeps_faces_bounds_and_translated_branch_domain(client):
    element = rod(400)
    tail = {"uid":"sample","name":"sensor region","kind":"space","n":1,"length_mm":2}
    cfg = system(element)
    cfg["elements"].append(tail)
    side = {"uid":"ref","name":"reference","target_uid":"sample","target_n":1,"wavelength_nm":1064,
            "tangential":{"radius_mm":.2,"wavefront":"waist","m2":1},"elements":[dict(element, uid="other", length_mm=1.1*element["length_mm"])]}
    shared = {**side, "uid":"shared", "name":"shared path", "target_uid":"rod", "through_uid":"sample",
              "target_n":1.5, "elements":[], "tangential":{"radius_mm":.08,"wavefront":"waist","m2":1}}
    cid = client.post("/api/cases",json={"title":"profile sampling"}).json()["id"]
    root = f"/api/cases/{cid}/optical-project"
    layout = {"system":cfg,"incident_paths":[side,shared]}
    preview = client.post(root+"/preview",json=layout)
    saved = client.post(root,json={"project":{"title":"dense",**layout},"change_note":"verify both physical ranges"})
    assert preview.status_code == saved.status_code == 200, (preview.text,saved.text)
    for record in [preview.json(),saved.json()["analysis"]]:
        assert record["profile_sampling_version"]>=2
        main = record["planes"]["tangential"]
        branch, common = [row["axes"]["tangential"] for row in record["sections"]["paths"]]
        for axis, radius, start in ((main,.1,0),(branch,.2,-.1*element["length_mm"]),(common,.08,0)):
            gap, = axis["profile_breaks"]
            coordinates = ([p["z_mm"] for p in axis["samples"]] if "samples" in axis
                           else [p[0] for p in axis["profile"]])
            assert coordinates[gap-1] == pytest.approx(start)
            assert coordinates[gap] == pytest.approx(element["length_mm"])
            skipped, = axis["profile_omissions"]
            assert skipped["start_mm"] == pytest.approx(start)
            assert skipped["end_mm"] == pytest.approx(element["length_mm"])
            assert skipped["radius_min_mm"] == pytest.approx(radius_at(math.pi/.4,radius,None))
            assert skipped["radius_max_mm"] == pytest.approx(radius)
            assert skipped["first_min_z_mm"] == pytest.approx(start+math.pi/.4)
            assert skipped["period_mm"] == pytest.approx(math.pi/.2)


def test_constant_width_in_long_matched_rod_and_zero_gradient_limit():
    matched = math.sqrt(.001064/(math.pi*1.5*.2))
    axis = analyze_system(system(rod(1000),radius=matched))["planes"]["tangential"]
    assert not axis.get("profile_breaks") and not axis["waists"]
    assert [p["radius_mm"] for p in axis["samples"]] == pytest.approx([matched,matched])
    flat = dict(rod(1), gradient_per_mm=0)
    for kind in ("grin","thick_lens"):
        homogeneous = analyze_system(system(dict(flat,kind=kind),curvature=-4.,count=3))["planes"]["tangential"]
        waist, = homogeneous["waists"]
        half_rayleigh = waist["z_mm"]+.5*waist["rayleigh_length_mm"]
        sample = next(p for p in homogeneous["samples"] if p["z_mm"]==pytest.approx(half_rayleigh))
        assert sample["radius_mm"] == pytest.approx(math.hypot(1,.5)*waist["radius_mm"])
    weak = analyze_system(system(dict(flat,gradient_per_mm=1e-20),curvature=-4.,count=3))["planes"]["tangential"]
    assert weak["waists"][0]["z_mm"] == pytest.approx(waist["z_mm"])
    assert weak["waists"][0]["radius_mm"] == pytest.approx(waist["radius_mm"])

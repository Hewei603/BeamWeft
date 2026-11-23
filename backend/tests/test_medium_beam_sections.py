import math
import random

import pytest

from app.workbench.beam_source import MEDIUM_BEAM, MediumBeamState, SourceAxis
from app.workbench.medium_beam import MaterialPropagation, propagate_sections


def run(client, root, name, inputs, connections=None):
    definition = client.get(root + "/studies/catalog/" + name).json()
    return client.post(root + "/studies/run/" + name, json={"version":definition["version"], "provider":definition["provider"],
                       "inputs":inputs, "connections":connections or {}})


def test_legacy_equivalent_width_at_unit_m2_reconnects_through_air_with_its_original_basis():
    # Previous material-side receipts only carried the width convention on the state.
    axis = {"radius_mm": .21, "m2": 1., "wavefront": "unknown"}
    origin = {"axis": axis, "medium_n": 1., "offset_mm": 0., "angle_mrad": 0., "matrix": [[1, 0], [0, 1]]}
    record = {"frame": MEDIUM_BEAM.unit, "wavelength_nm": 920., "medium_n": 1., "tangential": axis,
              "sagittal": {"radius_mm": .18, "m2": 1, "wavefront": "waist", "second_moment_radius": True},
              "width_basis": {"tangential": "equivalent", "sagittal": "second_moment"},
              "origins": {"tangential": origin}}
    source = MediumBeamState.model_validate(record)
    assert source.tangential.equivalent_radius and source.origins["tangential"].axis.equivalent_radius
    assert "equivalent_radius" not in axis
    continued = propagate_sections(MaterialPropagation(input_beam=source, sections=[{"length_mm": 3}]))
    assert continued["exit_air"]["tangential"]["equivalent_radius"]
    assert continued["exit_air"]["origins"]["tangential"]["axis"]["equivalent_radius"]
    assert continued["exit_air"]["sagittal"]["second_moment_radius"]
    with pytest.raises(ValueError, match="一种口径"):
        SourceAxis(second_moment_radius=True, equivalent_radius=True)


def test_material_side_reference_inside_a_saved_window(client):
    cid = client.post("/api/cases", json={"title":"window"}).json()["id"]
    root = f"/api/cases/{cid}"
    waist, wavelength = .18, .001
    z_rayleigh = math.pi * waist**2 / wavelength
    project = {"title":"有斜率的窗口", "system":{"mode":"single_pass","wavelength_nm":1000,
        "source":{"tangential":{"radius_mm":waist,"m2":1,"wavefront":"waist"},"launch_ray":{"t_offset_mm":.1,"t_angle_mrad":3}},
        "elements":[{"uid":"w","name":"窗口","kind":"space","length_mm":18,"n":1.5},
                    {"uid":"a","name":"空气","kind":"space","length_mm":1}]},
        "observation_planes":[{"uid":"inside","name":"窗口内","target_uid":"w","depth_mm":6},
                              {"uid":"air","name":"空气侧","target_uid":"a","depth_mm":0}]}
    saved = client.post(root+"/optical-project", json={"project":project,"change_note":"test"}).json()
    rev = saved["revision_id"]
    references = [row["beams"][0]["continuation_port"] for row in saved["analysis"]["sections"]["planes"]]
    ref = dict(port=references[0], revision_id=rev)
    material = client.get(root+"/studies/project-outputs", params=ref).json()["outputs"][ref["port"]]["value"]
    assert material["medium_n"] == 1.5 and material["launch_ray"]["t_angle_mrad"] == pytest.approx(2)
    assert material["launch_ray"]["t_offset_mm"] == pytest.approx(.112)
    assert material["tangential"]["curvature_mm"] == pytest.approx(6 + (1.5*z_rayleigh)**2/6)
    legs = {"sections":[{"length_mm":12,"next_medium_n":1}]}
    assert run(client,root,"propagate_paraxial_sections",legs,{"input_beam":ref}).status_code == 422
    ref["registration"] = "窗口内 6 mm，同一坐标和 T/S 轴，顺光束方向。"
    assert run(client,root,"propagate_paraxial_sections",legs,{"input_air":ref}).status_code == 422
    result = run(client,root,"propagate_paraxial_sections",legs,{"input_beam":ref})
    assert result.status_code == 200, result.text
    output = result.json()
    assert output["exit_beam"]["launch_ray"]["t_offset_mm"] == pytest.approx(.136)
    assert output["exit_air"]["launch_ray"]["t_angle_mrad"] == pytest.approx(3)
    assert output["exit_beam"]["tangential"]["radius_mm"] == pytest.approx(waist * math.hypot(1.,12/z_rayleigh))
    assert output["exit_air"]["tangential"]["curvature_mm"] == pytest.approx(12+z_rayleigh**2/12)
    airref = {"revision_id":rev, "port":references[1], "registration":"窗口后端空气侧"}
    air_input = run(client,root,"propagate_paraxial_sections",{"sections":[{"length_mm":0}]},{"input_air":airref})
    assert air_input.json()["exit_air"]["tangential"]["radius_mm"] == pytest.approx(output["exit_air"]["tangential"]["radius_mm"])
    legacy = run(client, root,"analyze_optical_system",{"mode":"single_pass","elements":[{"kind":"space","name":"air","length_mm":5}]},
                 {"input_beam":{"port":"exit_air","run_id":output["study_record"]["id"],"registration":"平面空气端同轴"}})
    assert legacy.json()["exit_beam"]["launch_ray"]["t_offset_mm"] == pytest.approx(.151)


def test_unresolved_origin_can_be_recovered_across_a_spherical_boundary():
    axis = {"radius_mm":.3, "wavefront":"unknown", "m2":2}
    original = {"frame":MEDIUM_BEAM.unit, "medium_n":1.5, "wavelength_nm":940, "tangential":axis, "sagittal":axis,
                "launch_ray":{"t_offset_mm":.45,"t_angle_mrad":None}}
    def forward(beam, sections):
        return propagate_sections(MaterialPropagation(input_beam=beam,sections=sections))
    first = forward(original, [{"length_mm":30, "surface_radius_mm":5, "next_medium_n":2}])["exit_beam"]
    assert first["tangential"]["radius_mm"] is None and first["origins"]["tangential"]["medium_n"] == 1.5
    image = forward(first,[{"length_mm":40,"next_medium_n":1}])
    assert image["exit_air"]["tangential"]["radius_mm"] == pytest.approx(.3)
    assert image["exit_beam"]["tangential"]["radius_mm"] == pytest.approx(.3)
    assert image["exit_beam"]["launch_ray"]["t_offset_mm"] == pytest.approx(-.45)
    assert image["exit_beam"]["launch_ray"]["t_angle_mrad"] is None
    off_image = forward(first,[{"length_mm":39.99,"next_medium_n":1}])
    assert off_image["exit_beam"]["tangential"]["radius_mm"] is None and "exit_air" not in off_image
    with pytest.raises(ValueError):
        forward(first,[{"length_mm":0,"surface_radius_mm":20}])


def test_general_astigmatic_sections_against_physical_q_and_snell_refraction():
    generator = random.Random(50731)
    for _ in range(48):
        wavelength = generator.uniform(.0004, .0017)
        first_index = generator.uniform(1.2, 2.3)
        index = first_index
        segments = []
        state = {"frame":MEDIUM_BEAM.unit, "wavelength_nm":wavelength*1e6, "medium_n":index, "launch_ray":{}}
        physical = {}
        for axis, prefix in (("tangential","t"), ("sagittal","s")):
            w, m2 = generator.uniform(.06,.9), generator.uniform(1.,4.)
            radius = generator.choice([-1,1])*generator.uniform(6.,250.)
            x, theta = generator.uniform(-.75,.75), generator.uniform(-.006,.006)
            state[axis] = {"radius_mm":w, "m2":m2, "wavefront":"curved","curvature_mm":radius,"second_moment_radius":True}
            state["launch_ray"].update({prefix+"_offset_mm":x, prefix+"_angle_mrad":theta*1000})
            physical[axis] = (1 / complex(1/radius, -m2*wavelength/(math.pi*index*w*w)), x, theta)
        for _ in range(5):
            distance = generator.uniform(2, 180)
            new_index = generator.uniform(1.1,2.3)
            radius = generator.choice([-1,1])*generator.uniform(15.,300.)
            segments.append({"length_mm":distance, "next_medium_n":new_index, "surface_radius_mm":radius})
            for axis,(q,x,theta) in physical.items():
                q += distance
                x += distance*theta
                # Wavefront vergence conservation and Snell's law linearized around the local surface normal.
                physical[axis] = (new_index/(index/q+(index-new_index)/radius),
                                  x, (index*theta+(index-new_index)*x/radius)/new_index)
            index = new_index
        actual = propagate_sections(MaterialPropagation(input_beam=state,sections=segments))["exit_beam"]
        for axis,(q,x,theta) in physical.items():
            prefix = "t" if axis == "tangential" else "s"
            expected_width = math.sqrt(-state[axis]["m2"]*wavelength/(math.pi*index*(1/q).imag))
            assert actual[axis]["radius_mm"] == pytest.approx(expected_width, rel=1e-10)
            assert actual[axis]["curvature_mm"] == pytest.approx(1/(1/q).real, rel=1e-10)
            assert actual["launch_ray"][prefix+"_offset_mm"] == pytest.approx(x,rel=1e-10,abs=1e-13)
            assert actual["launch_ray"][prefix+"_angle_mrad"] == pytest.approx(1000*theta,rel=1e-10,abs=1e-11)


@pytest.mark.parametrize("basis,factor", [("second_moment", 1.7), ("equivalent", 1.)])
def test_registered_measured_caustic_continues_in_its_own_medium(client, basis, factor):
    cid = client.post("/api/cases", json={"title":"液池"}).json()["id"]
    root = f"/api/cases/{cid}"
    n, wavelength, waist = 1.33, .00082, .06
    theta = factor * wavelength / (math.pi*n*waist)
    def width(z):
        return math.hypot(waist, theta*z)
    positions = [-70,-40,-18,-6,0,8,20,35,60]
    fit = run(client,root,"fit_beam_caustic",{"z_mm":positions,"radius_mm":[width(z) for z in positions],
        "wavelength_nm":wavelength*1e6,"medium_n":n,"data_source":"水中焦散测量",
        "register_plane":{"z_mm":8,"axis":"sagittal","width_basis":basis,"note":"Y 对应 S；在同一液池内向前。"}})
    assert fit.status_code == 200, fit.text
    value = fit.json()["registered_beam"]
    assert value["medium_n"] == n and value["sagittal"]["m2"] == pytest.approx(factor)
    assert SourceAxis.model_validate(value["sagittal"]).width_basis() == basis
    assert value["sagittal"]["curvature_mm"] == pytest.approx(8 + (waist/theta)**2/8)
    ref = {"run_id":fit.json()["study_record"]["id"],"port":"registered_beam","registration":"同一液池与相机横轴"}
    continued = run(client,root,"propagate_paraxial_sections",{"sections":[{"length_mm":20}]},{"input_beam":ref})
    assert continued.status_code == 200, continued.text
    beam = continued.json()["exit_beam"]
    assert beam["sagittal"]["radius_mm"] == pytest.approx(width(28))
    assert beam["launch_ray"]["s_angle_mrad"] is None and beam["tangential"]["radius_mm"] is None
    assert beam["width_basis"] == {"sagittal":basis}

"""Contract checks independent of the packaged instrument examples."""
import pytest
from pydantic import Field

from app.study_api import ElementLaw, StrictInput, Study
from app.workbench import tools
from app.workbench.device_laws import Prescription, device_catalog
from app.workbench.network import PortNetwork, evaluate_network
from app.workbench.observation import OpticalLayout, analyze_layout
from app.workbench.optics import analyze_system, scan_system, scan_system_2d


class ScaleInputs(StrictInput):
    wavelength: float = Field(gt=0)
    distance: float = Field(7, ge=0)
    reference: float = Field(500, gt=0)


@pytest.fixture
def relay(monkeypatch):
    def transfer(p):
        scale = p.wavelength / p.reference
        return {"mapping": {"matrix_t": [scale, 0, 0, 1/scale], "matrix_s": [1, 0, 0, 1]},
                "warnings": ["Calibrated boundary map only."]}
    study = Study("calibrated_relay", "Calibration relay", "3", "Synthetic boundary transfer in air.",
                  ScaleInputs, transfer, element_law=ElementLaw(representation="ray_matrix",
                      wavelength_input="wavelength", length_input="distance", output="mapping"))
    monkeypatch.setitem(tools.STUDIES, study.name, (study, "test-lab"))
    return {"uid": "relay", "name": "Relay", "kind": "registered", "length_mm": 7,
            "prescription": {"study":study.name, "provider":"test-lab", "version":"3"}}


def space(length=10):
    return {"uid":"air", "name":"Air", "kind":"space", "length_mm":length}


def test_custom_transfer_true_wavelength_default_capture_and_sweep(relay):
    system = {"mode":"single_pass", "wavelength_nm":1000, "beam_quality_m2":2.1,
              "input_radius_mm":0.3, "elements":[relay]}
    result = analyze_system(system)
    record, = result["evaluated_devices"]
    assert record["inputs"] == {"wavelength":1000, "distance":7, "reference":500}
    assert record["warnings"][0] in result["model_warnings"][0]
    for axis, expected in [("tangential", 0.6), ("sagittal", 0.3)]:
        plane = result["planes"][axis]
        assert plane["output"]["radius_mm"] == pytest.approx(expected)
        assert [p["z_mm"] for p in plane["samples"]] == [0, 7]
        assert plane["profile_breaks"] == [1]
        assert plane["center_profile"] == [[0,0], [7,0]]
        assert plane["center_profile_breaks"] == [1]
    scan = scan_system({"system":system, "element_index":0, "parameter":"parameters.reference",
                        "start":250, "stop":750, "count":3})
    assert [r["tangential"]["output"]["radius_mm"] for r in scan["points"]] == pytest.approx([1.2,.6,.4])
    assert all(r["sagittal"]["output"]["radius_mm"] == pytest.approx(.3) for r in scan["points"])
    recipe = Prescription(**relay["prescription"], parameters={"wavelength":2100})
    with pytest.raises(ValueError, match="波长"):
        recipe.evaluate("ray_matrix", 1000, 7)


def test_missing_provider_is_local_to_its_incident_path(relay, monkeypatch):
    system = {"mode":"single_pass", "wavelength_nm":700, "input_radius_mm":.3, "elements":[space()]}
    branch = {"uid":"probe", "name":"Probe", "target_uid":"air", "wavelength_nm":800,
              "tangential":{"radius_mm":.2, "wavefront":"waist", "m2":1}, "target_n":1,
              "launch_ray":{"t_offset_mm":.1}, "elements":[relay]}
    layout = OpticalLayout.model_validate({"system":system, "incident_paths":[branch],
               "observation_planes":[{"uid":"face","name":"Face","target_uid":"air","depth_mm":0}]})
    reference = analyze_layout(layout)["sections"]
    assert reference["paths"][0]["evaluated_devices"][0]["inputs"]["wavelength"] == 800
    assert reference["planes"][0]["beams"][1]["field_axes"]["tangential"]["center_mm"] == pytest.approx(.16)
    monkeypatch.delitem(tools.STUDIES, "calibrated_relay")
    received = analyze_layout(layout)["sections"]
    assert received["status"] == "partial"
    main, missing = received["planes"][0]["beams"]
    assert main["radii_mm"]["tangential"] == pytest.approx(.3)
    assert not missing["radii_mm"] and "test-lab" in missing["notes"]["tangential"]


def test_unavailable_builtin_displays_product_name_and_preserves_saved_provider():
    saved = device_catalog()[0]["aligned_lens_power"]["prescription"]
    recipe = Prescription(**{**saved, "version": "missing"})
    with pytest.raises(ValueError, match="BeamWeft 中的 aligned_lens_power vmissing"):
        recipe.definition("ray_matrix")
    assert recipe.model_dump()["provider"] == saved["provider"] == "LaserClaw"


def test_new_resonator_scan_matches_native_thin_lens_for_each_axis():
    devices, _ = device_catalog()
    lens = {"name":"axis lens","kind":"registered","prescription":devices["aligned_lens_power"]["prescription"]}
    mirror = {"name":"M", "kind":"mirror", "radius_mm":250}
    train = [mirror,space(30),lens,space(70),mirror]
    system = {"mode":"standing_wave","wavelength_nm":632.8,"elements":train}
    result = scan_system_2d({"system":system,
        "x":{"element_index":2,"parameter":"parameters.tangential_dpt","start":4,"stop":8,"count":3},
        "y":{"element_index":2,"parameter":"parameters.sagittal_dpt","start":2,"stop":6,"count":3}})
    assert result["invalid_count"] == 0
    for axis in ("tangential","sagittal"):
        for i,y in enumerate(result["y"]):
            for j,x in enumerate(result["x"]):
                ordinary = [*train[:2], {"kind":"lens","name":"thin","focal_mm":1000/(x if axis=="tangential" else y)}, *train[3:]]
                expected = analyze_system({**system,"elements":ordinary}, profile=False)["planes"][axis]["m"]
                assert result["maps"][axis][i][j] == pytest.approx(expected)


@pytest.mark.parametrize(("input_port","output_port","delay","fraction"),[
    ("L-H","R-V",500,1), ("R-V","L-H",500,1), ("L-H","R-V",250,.5)])
def test_registered_jones_transmission_in_both_directions(input_port, output_port, delay, fraction):
    device = device_catalog()[0]["principal_axis_transmission"]
    recipe = {**device["prescription"], "parameters":{"angle_deg":45, "second_transmission":1, "delay_nm":delay}}
    component = {"uid":"sample","name":"sample","model":{"kind":"registered","prescription":recipe,"ports":device["ports"]}}
    network = PortNetwork(components=[component], drives=[{"at":{"component":"sample","port":input_port},"power_w":.004}])
    result = evaluate_network(network, 1000)
    assert result["status"] == "completed"
    assert next(p["outgoing_w"] for p in result["ports"] if p["port"]==output_port) == pytest.approx(.004*fraction)
    assert result["islands"][0]["external_w"] == pytest.approx(.004)


@pytest.mark.parametrize(("representation","payload","message"),[
    ("ray_matrix", {"matrix_t":[2,0,0,1]}, "AD"),
    ("scattering", {"ports":["a"], "matrix":[[[1.2,0]]]}, "增益"),
    ("scattering", {"ports":["other"], "matrix":[[[1,0]]]}, "端口顺序"),
])
def test_host_validates_numerical_return_and_channel_identity(monkeypatch, representation, payload, message):
    name = "invalid_" + representation
    study = Study(name, "Invalid device", message, "Synthetic.", ScaleInputs, lambda _:{"value":payload},
                  element_law=ElementLaw(representation=representation, wavelength_input="wavelength",
                                         ports=("a",) if representation=="scattering" else (), output="value"))
    monkeypatch.setitem(tools.STUDIES, name, (study, "test-lab"))
    with pytest.raises(ValueError, match=message):
        Prescription(study=name, version=message, provider="test-lab").evaluate(representation, 1000)

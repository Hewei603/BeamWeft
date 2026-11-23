import math

import pytest
from pydantic import ValidationError

from app.workbench.authoring import EditableLayout, analyze_editable
from app.workbench.observation import analyze_sections
from app.workbench.optics import OpticalSystem, PropagationStudySystem, analyze_system, scan_system
from app.workbench.prediction_comparisons import PredictionComparison, _sample_model
from app.workbench.studies import _propagate


def layout(source, elements=None):
    elements = elements or [{"uid": "gap", "kind": "space", "length_mm": 100, "name": "slab", "n": 1.5}]
    return EditableLayout(system={"mode": "single_pass", "wavelength_nm": 1000, "source": source, "elements": elements},
                          observation_planes=[{"uid": name, "name": name, "target_uid": "gap", "depth_mm": depth}
                                              for name, depth in (("entrance", 0), ("detector", 60))])


def test_astigmatic_main_train_matches_ray_and_width_transfer_at_independent_stations():
    t = {"radius_mm": .2, "m2": 1, "wavefront": "curved", "curvature_mm": -200}
    s = {"radius_mm": .48, "m2": 2.5, "wavefront": "waist", "second_moment_radius": True}
    model = layout({"tangential": t, "sagittal": s,
                    "launch_ray": {"t_offset_mm": .4, "t_angle_mrad": -2, "s_offset_mm": -.2, "s_angle_mrad": 3}},
                   [{"uid": "lens", "kind": "lens", "name": "lens", "focal_mm": 100},
                    {"uid": "gap", "kind": "space", "name": "glass", "length_mm": 100, "n": 1.5}])
    result = analyze_editable(model)
    assert result["effective_wavelength_nm"] is None and result["intensity_model"] == "width_only"
    sections = analyze_sections(model, result, include_q=True)["planes"]
    assert result["status"] == "completed"
    for axis, source, center, angle in (("tangential", t, .4, -.002), ("sagittal", s, -.2, .003)):
        info = result["planes"][axis]
        assert info["effective_wavelength_nm"] == source["m2"] * 1000
        for section in sections:
            reduced_distance = section["depth_mm"] / 1.5
            a, b = 1 - reduced_distance / 100, reduced_distance
            initial_vergence = 1 / source["curvature_mm"] if source["wavefront"] == "curved" else 0
            width = source["radius_mm"]
            expected = width * math.hypot(a + b * initial_vergence, b * source["m2"] * .001 / (math.pi * width**2))
            beam = section["beams"][0]
            assert beam["radii_mm"][axis] == pytest.approx(expected)
            field = beam["field_axes"][axis]
            assert field["center_mm"] == pytest.approx(a * center + b * angle)
            assert field["reduced_angle"] == pytest.approx(angle - center / 100)
            assert len(field["q_mm"]) == 2
            assert info["width_basis"] == beam["width_basis"][axis] == ("gaussian" if axis == "tangential" else "second_moment")
    assert "exit_field" not in _propagate(PropagationStudySystem(**model.system.model_dump()))


def test_unknown_wavefront_preserves_single_axis_radius_only_at_the_source():
    source = {"tangential": {"m2": 1, "radius_mm": .25, "wavefront": "waist"},
              "sagittal": {"m2": 1, "radius_mm": .6},
              "launch_ray": {"s_angle_mrad": None}}
    model = layout(source)
    result = analyze_editable(model, include_sections=True)
    assert result["status"] == "partial"
    assert "output" in result["planes"]["tangential"]
    incomplete = result["planes"]["sagittal"]
    assert "波前" in incomplete["message"]
    assert "input" not in incomplete and "output" not in incomplete
    assert incomplete["samples"] == [{"z_mm": 0, "radius_mm": .6}]
    start, stop = (s["beams"][0] for s in result["sections"]["planes"])
    assert start["radii_mm"]["sagittal"] == .6 and start["radius_origin"]["sagittal"] == "source_input"
    assert start["field_axes"]["sagittal"] == {"center_mm": 0, "reduced_angle": None}
    assert "sagittal" not in stop["radii_mm"] and "波前" in stop["notes"]["sagittal"]
    assert stop["field_axes"]["sagittal"]["center_mm"] is None
    assert "exit_field" not in _propagate(PropagationStudySystem(**model.system.model_dump()))
    scan = scan_system({"system": model.system.model_dump(), "element_index": 0, "parameter": "length_mm",
                        "start": 60, "stop": 80, "count": 3})
    assert scan["status"] == "partial"
    assert all(row["status"] == "partial" and "output" in row["tangential"] and "波前" in row["sagittal"]["message"]
               for row in scan["points"])


def test_shared_axes_preserve_their_common_quality_through_the_existing_field_port():
    model = layout({"tangential": {"radius_mm": .35, "m2": 2.4, "wavefront": "waist"}})
    config = model.system.model_dump()
    result = _propagate(PropagationStudySystem(**config))
    assert result["exit_field"]["beam_quality_m2"] == 2.4
    receiver = {k: v for k, v in config.items() if k not in {"source", "beam_quality_m2"}}
    continued = _propagate(PropagationStudySystem(**receiver, input_field=result["exit_field"]))
    together = analyze_system({**config, "elements": config["elements"] * 2})
    for axis in ("tangential", "sagittal"):
        assert continued["planes"][axis]["output"]["radius_mm"] == pytest.approx(together["planes"][axis]["output"]["radius_mm"])
    unknown = analyze_system({**config, "source": {**config["source"], "sagittal": {}}})
    assert unknown["status"] == "partial" and unknown["planes"]["sagittal"]["samples"] == []
    with pytest.raises(ValidationError):
        PropagationStudySystem(**config, input_field=result["exit_field"])
    for conflicting in ({"input_radius_mm": .4}, {"input_curvature_mm": 50}, {"beam_quality_m2": 1.4}, {"mode": "ring"}):
        with pytest.raises(ValidationError):
            OpticalSystem.model_validate({**config, **conflicting})


def test_sources_and_receivers_keep_their_declared_reference_frames():
    source = {"tangential": {"radius_mm": .25, "m2": 1, "wavefront": "waist"}, "launch_ray": {"s_angle_mrad": None}}
    train = [{"kind": "space", "name": "object to lens", "length_mm": 40},
             {"kind": "lens", "name": "imaging", "focal_mm": 20},
             {"kind": "space", "uid": "gap", "name": "image space", "length_mm": 60}]
    model = layout(source, train)
    model.observation_planes[1].depth_mm = 40
    result = analyze_editable(model, include_sections=True)
    # Across a conjugate pair B=0: an unknown launch angle does not obscure the imaged center.
    image = result["sections"]["planes"][1]["beams"][0]["field_axes"]["sagittal"]
    assert image["center_mm"] == pytest.approx(0) and image["reduced_angle"] is None
    assert "exit_field" not in _propagate(PropagationStudySystem(**model.system.model_dump()))


@pytest.mark.parametrize("focus", [25.4, 70.8])
def test_uncertain_axis_uses_product_roundoff_at_conjugates_without_zeroing_small_physical_gaps(focus):
    source = {"tangential": {"radius_mm": .39, "m2": 1},
              "launch_ray": {"t_offset_mm": .21, "t_angle_mrad": None}}
    setup = {"mode": "single_pass", "wavelength_nm": 850, "source": source}
    gap = {"kind": "space", "name": "conjugate arm", "length_mm": 2*focus}
    lens = {"kind": "lens", "name": "relay", "focal_mm": focus}
    def run(elements, **inputs):
        return _propagate(PropagationStudySystem(elements=elements, **inputs))
    for split in (1, 2):
        train = [gap, lens, gap]
        mid = run(train[:split], **setup)
        end = run(train[split:], mode="single_pass", input_beam=mid["exit_beam"])
        whole = run(train, **setup)
        for result in (end, whole):
            assert result["exit_beam"]["tangential"]["radius_mm"] == pytest.approx(.39)
            assert result["exit_beam"]["launch_ray"]["t_offset_mm"] == pytest.approx(-.21)
            assert result["planes"]["tangential"]["output"]["radius_mm"] == pytest.approx(.39)
        near = run(train[split:-1]+[{**gap, "length_mm": 2*focus+1e-9}], mode="single_pass", input_beam=mid["exit_beam"])
        assert near["exit_beam"]["tangential"]["radius_mm"] is None
        assert near["exit_beam"]["launch_ray"]["t_offset_mm"] is None
        assert "output" not in near["planes"]["tangential"]
    miniature = run([{**gap, "length_mm": 1e-16}], **setup)
    assert miniature["planes"]["tangential"]["matrix"][0][1] == 1e-16
    assert miniature["exit_beam"]["tangential"]["radius_mm"] is None
    assert miniature["exit_beam"]["launch_ray"]["t_offset_mm"] is None


def test_unmeasured_wavelength_keeps_source_width_but_cannot_generate_a_prediction():
    model = layout({"tangential": {"radius_mm": .3, "m2": 1, "wavefront": "waist"},
                    "sagittal": {"radius_mm": .5, "m2": 2, "second_moment_radius": True}})
    model.system.wavelength_nm = None
    result = analyze_editable(model, include_sections=True)
    assert result["status"] == "partial"
    for axis, radius, basis in (("tangential", .3, "gaussian"), ("sagittal", .5, "second_moment")):
        plane = result["planes"][axis]
        assert plane["width_basis"] == basis and plane["effective_wavelength_nm"] is None
        assert plane["samples"] == [{"z_mm": 0, "radius_mm": radius}] and "output" not in plane
        assert "真空波长" in plane["message"]
        assert ("波前" in plane["message"]) == (axis == "sagittal")
    start, stop = [plane["beams"][0] for plane in result["sections"]["planes"]]
    assert start["radii_mm"] == {"tangential": .3, "sagittal": .5} and not stop["radii_mm"]
    revision = {"project": {"title": "unknown wavelength", **model.model_dump()}, "analysis": result, "revision_id": 1}
    request = PredictionComparison(reference_revision_id=1, axis="sagittal", definition="d4sigma_diameter",
                                   unit="mm", positions_mm=[0, 60], registration_note="common coordinate")
    _, samples = _sample_model(revision, request)
    assert samples[0]["supplied_radius_mm"] == .5 and "width" not in samples[0] and "独立" in samples[0]["issue"]
    assert "width" not in samples[1] and "波长" in samples[1]["issue"]


def test_legacy_conflicting_curvature_remains_editable_and_has_no_propagated_q_on_either_path():
    source = {"tangential": {"radius_mm": .31, "m2": 1, "wavefront": "waist", "curvature_mm": 80}}
    project = layout(source).model_dump()
    project["incident_paths"] = [{"uid": "arm", "target_uid": "gap", "name": "branch", "target_n": 1.5,
                                  "wavelength_nm": 1000, **source}]
    result = analyze_editable(EditableLayout.model_validate(project), include_sections=True)
    assert result["status"] == "partial"
    assert "有限曲率" in result["planes"]["tangential"]["message"] and "output" not in result["planes"]["tangential"]
    for beam in result["sections"]["planes"][1]["beams"]:
        assert not beam["radii_mm"] and "有限曲率" in beam["notes"]["sagittal"]

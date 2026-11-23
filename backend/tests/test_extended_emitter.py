import math

import numpy as np
import pytest

from app.workbench.ray_source import ExtendedEmitter, RayBundle, RaySupport, sample_emitter


def emitter(**changes):
    return ExtendedEmitter(
        **{
            **{
                "plane": "emitting face",
                "basis": "measured mechanical aperture and manufacturer NA",
                "medium_n": 1.33,
                "position_shape": "ellipse",
                "width_t_mm": 0.4,
                "angular_shape": "ellipse",
                "na_t": 0.25,
            },
            **changes,
        }
    )


def direction_coordinates(rows, n, steer=(0, 0)):
    p = rows[:, 2:4] - steer
    return p / np.sqrt(1 + (p**2).sum(axis=1)[:, None] / n**2)


def test_bounds_are_usable_without_an_invented_fill(client):
    cid = client.post("/api/cases", json={"title": "Emission bounds"}).json()["id"]
    got = client.post(
        f"/api/cases/{cid}/studies/run/sample_phase_space_source",
        json={"provider": "BeamWeft", "version": "1", "inputs": emitter().model_dump()},
    )
    assert got.status_code == 200, got.text
    data = got.json()
    assert data["status"] == "completed" and "ray_bundle" not in data and data["fill"] == "unspecified"
    assert {"ray_support", "ray_bundle"} & data["study_record"]["available_outputs"].keys() == {"ray_support"}
    reference = data["study_record"]["available_outputs"]["ray_support"]["condition_values"]
    assert reference["ray.plane"] == "emitting face"
    assert reference["ray.medium_n"] == 1.33 and reference["ray.quality"] == "documented"
    assert "optics.vacuum-wavelength.nm" not in reference
    assert data["ray_support"]["p_t"] == pytest.approx([-0.25, 0.25] / np.sqrt(1 - (0.25 / 1.33) ** 2))
    assert data["ray_support"]["x_mm"] == pytest.approx([-0.2, 0.2])
    assert data["etendue_limit_mm2_sr"] == pytest.approx(math.pi**2 * 0.2**2 * 0.25**2)
    assert data["ray_support"]["wavelength_nm"] is None


def test_uniform_radiance_has_expected_independent_disk_marginals_and_joint_fraction():
    params = emitter(fill="uniform_radiance", samples=8192, offset_t_mm=0.12, na_s=0.04)
    result = sample_emitter(params)
    sample = result["ray_bundle"]
    rows = np.asarray(sample["rays"])
    assert sample_emitter(params)["ray_bundle"] == sample
    assert sample["replicate_split"] == 4096 and sample["quality"] == "conditional"
    assert rows[:4096, 4].sum() == 0.5 and rows[4096:, 4].sum() == 0.5
    assert not np.array_equal(rows[:4096], rows[4096:])
    near = ((rows[:, 0] - 0.12) / 0.2) ** 2 + (rows[:, 1] / 0.2) ** 2
    na = direction_coordinates(rows, 1.33)
    far = (na[:, 0] / 0.25) ** 2 + (na[:, 1] / 0.04) ** 2
    assert max(near.max(), far.max()) <= 1 and min(far.min(), near.min()) >= 0
    assert np.mean(near < 0.64) == pytest.approx(0.64, abs=0.002)
    assert np.mean(far < 0.25) == pytest.approx(0.25, abs=0.002)
    assert np.mean((near < 0.64) & (far < 0.25)) == pytest.approx(0.16, abs=0.002)
    assert result["ray_support"]["p_s"][1] == pytest.approx(0.04 / math.sqrt(1 - (0.04 / 1.33) ** 2))


def test_rectangular_divergence_support_uses_forward_longitudinal_direction_and_signed_steering():
    params = emitter(
        fill="uniform_radiance",
        position_shape="rectangle",
        width_s_mm=0.8,
        angular_shape="rectangle",
        na_s=0.08,
        steer_t_mrad=12.0,
        samples=1024,
    )
    result = sample_emitter(params)
    pmax = 0.25 / math.sqrt(1 - (0.25**2 + 0.08**2) / 1.33**2)
    steering = 1.33 * math.tan(0.012)
    assert result["ray_support"]["p_t"] == pytest.approx([steering - pmax, steering + pmax])
    assert result["ray_support"]["y_mm"] == pytest.approx([-0.4, 0.4])
    rays = np.asarray(result["ray_bundle"]["rays"])
    assert np.all(np.abs(direction_coordinates(rays, 1.33, (steering, 0))) <= np.array([0.25, 0.08]))
    assert np.mean(abs(rays[:, 0]) < 0.1) == pytest.approx(0.5, abs=0.002)
    assert "etendue_limit_mm2_sr" not in result
    with pytest.raises(ValueError, match="前向半球"):
        emitter(medium_n=1.0, angular_shape="rectangle", na_t=0.8, na_s=0.8)
    with pytest.raises(ValueError, match="整数次幂"):
        emitter(samples=300)


def optical_map(**changes):
    return {
        **{
            "x_edges_mm": [-0.5, 0.5, 4.5],
            "y_edges_mm": [0, 2, 3],
            "values": [[0, 3.0], [1.0, 0]],
            "signal_unit": "net DN",
            "x_label": "column",
            "y_label": "row",
            "basis": "independent calibration; detector positions",
            "y_down": True,
            "quality": "usable",
            "wavelength_nm": 905.0,
        },
        **changes,
    }


def mapped(**changes):
    return emitter(
        **{
            **{
                "position_shape": None,
                "width_t_mm": None,
                "position_map": optical_map(),
                "map_rotation_deg": 90,
                "map_flip_y": True,
                "offset_t_mm": 0.1,
                "offset_s_mm": 0.2,
                "fill": "map_independent_angle",
                "samples": 2048,
            },
            **changes,
        }
    )


def test_registered_map_draws_cell_integrals_even_for_unequal_cells_and_keeps_the_window_as_budget():
    result = sample_emitter(mapped())
    bundle = result["ray_bundle"]
    rows = np.asarray(bundle["rays"])
    # In this registered orientation T = detector y + .1, S = detector x + .2.
    x, y = rows[:, 1] - 0.2, rows[:, 0] - 0.1
    upper = (x >= 0.5) & (y < 2)
    lower = (x < 0.5) & (y > 2)
    assert (upper | lower).all()
    assert rows[upper, 4].sum() == pytest.approx(0.75, abs=0.002)
    assert np.mean(x[upper]) == pytest.approx(2.5, abs=0.003)
    assert result["source_area_mm2"] == pytest.approx(15.0)
    assert result["ray_support"]["x_mm"] == pytest.approx([0.1, 3.1])
    assert result["ray_support"]["y_mm"] == pytest.approx([-0.3, 4.7])
    assert bundle["wavelength_nm"] == 905 and any("窗外" in s for s in bundle["warnings"])
    with pytest.raises(ValueError, match="波长"):
        mapped(wavelength_nm=532.0)
    with pytest.raises(ValueError, match="转角和镜像"):
        mapped(map_flip_y=None)


@pytest.mark.parametrize(
    "image",
    [optical_map(values=[[0, 3], [1, -0.125]]), optical_map(quality="limited"), optical_map(values=[[0, 0], [0, 0]])],
)
def test_biased_or_signed_measurement_does_not_generate_negative_or_clipped_light(image):
    result = sample_emitter(mapped(position_map=image))
    assert "ray_bundle" not in result and "ray_support" in result and result["status"] == "partial"


def test_continuation_validates_power_relative_to_the_original_ray_list():
    frame = dict(plane="exit", basis="calibrated near-field distribution", medium_n=1)
    point = [0.0, 0.0, 0.0, 0.0, 0.25]
    assert RayBundle(**frame, quality="documented", rays=[], replicate_split=0).rays == []
    assert RayBundle(**frame, quality="conditional", rays=[point], replicate_split=0).replicate_split == 0
    with pytest.raises(ValueError, match="一半"):
        RayBundle(**frame, quality="conditional", rays=[point] * 3, replicate_split=3)
    with pytest.raises(ValueError, match="超过"):
        RayBundle(**frame, quality="conditional", rays=[point] * 5)
    with pytest.raises(ValueError, match="下界"):
        RaySupport(**frame, x_mm=(1, 0), y_mm=(0, 0), p_t=(0, 0), p_s=(0, 0))

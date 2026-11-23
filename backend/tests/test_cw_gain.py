import math

import pytest
from pydantic import ValidationError

from app.physics.cw_gain import four_level_gain, measured_gain, roundtrip_balance
from app.workbench.gain_studies import FourLevel, ProbeGain, Roundtrip
from app.workbench.gain_studies import SpatialOverlap, spatial_overlap


def curve(*ratios):
    return measured_gain(ProbeGain(pump_nm=808, signal_nm=1064, basis="同一弱探针与参考面",
                                   absorbed_w=list(range(len(ratios))), power_ratio=ratios))["curve"]


def budget(law, survival, *, traversals=1, watts=None):
    return roundtrip_balance(Roundtrip(curve=law, signal_nm=1064, traversals=traversals,
                             survival=survival, operating_w=watts, basis="探针损耗已扣除"))


def test_four_level_from_absorbed_photon_count_and_uniform_excited_volume():
    fixed = dict(pump_nm=808, signal_nm=1064, emission_cm2=2.8e-19, lifetime_us=220, pump_yield=.72,
                 area_mm2=.15, max_absorbed_w=12, basis="同一横向均匀泵浦和信号，自定试算参数")
    law = four_level_gain(FourLevel(**fixed))
    # In a 5-mm rod explicitly solve dN/dt = R - N/tau at 1 W, then integrate sigma*N over length.
    energy_j = 6.62607015e-34 * 299792458 / (808e-9)
    density_m3 = .72 / energy_j * 220e-6 / (.15e-6 * .005)
    exponent = 2.8e-23 * density_m3 * .005
    assert law["log_gain_per_w"] == pytest.approx(exponent)
    for passes in (1, 2):
        result = budget(law["curve"], [.8, .8], traversals=passes)
        point = result["balance_bands"][0]
        assert point["start_w"] == point["stop_w"]
        assert .8 * math.exp(exponent * point["start_w"])**passes == pytest.approx(1)
    assert four_level_gain(FourLevel(**{**fixed,"pump_yield":0}))["curve"]["log_gain"] == [0, 0]


def test_rollover_plateau_uncertain_loss_and_bounded_domain():
    law = curve(1, 2, 2, 4, 1)
    assert budget(law, [.5,.5])["balance_bands"] == [{"start_w":1, "stop_w":2}, {"start_w":3.5, "stop_w":3.5}]
    assert budget(law,[.5,.5],watts=[1,2])["operating_state"] == "balanced"
    assert budget(law,[.4,.6],watts=[1,2])["operating_state"] == "conditional"
    assert budget(law,[.3,.4],watts=[0,4])["operating_state"] == "conditional"
    assert budget(law,[.3,.4],watts=[3,3])["operating_state"] == "above"
    assert budget(law,[.3,.4],watts=[4,4])["operating_state"] == "below"
    outside = budget(law,[.5,.5],watts=[3,5])
    assert outside["status"] == "partial" and outside["operating_state"] == "outside_domain"
    assert "operating_margin" not in outside
    assert budget(law,[0,0],watts=[2,3])["balance_bands"] == []
    assert budget(law,[0,.5],watts=[2,3])["balance_bands"] == [{"start_w":1,"stop_w":3.5}]
    assert budget(law,[1,1],watts=[0,0])["operating_state"] == "balanced"
    with pytest.raises(ValidationError,match="同一信号波长"):
        Roundtrip(curve=law, signal_nm=532, traversals=1, survival=[.5,.5], basis="另一信号")


def test_unsorted_paired_measurements_rejected():
    with pytest.raises(ValidationError,match="严格递增"):
        ProbeGain(pump_nm=980,signal_nm=1030,basis="同一试样",absorbed_w=[0,1,0],power_ratio=[1,2,1.2])


def test_overlapping_waists_along_absorbing_sample_against_independent_axial_sum():
    import numpy as np

    length = 20.
    sample = {"kind":"space","name":"样品","uid":"sample","n":1.55,"length_mm":length}
    law = {"mode":"bulk", "entry":{"low":1},"exit":{"low":1},"alpha_per_mm":{"low":.09}}
    pump = {"uid":"pump","name":"离轴测试束","target_uid":"sample","target_n":1.7,"wavelength_nm":815,
            "tangential":{"radius_mm":.035,"wavefront":"waist","m2":1}, "sagittal":{"radius_mm":.08,"wavefront":"waist","m2":1},
            "launch_ray":{"t_offset_mm":.022,"s_angle_mrad":1.4},
            "power_budget":{"wavelength_nm":815,"stages":[{"scope":"host","element_uid":"sample","law":law}]}}
    layout = {"system":{"mode":"single_pass","wavelength_nm":1050,"input_radius_mm":.09,"elements":[sample]}, "incident_paths":[pump]}
    def calc():
        return spatial_overlap(SpatialOverlap(layout=layout))
    result = calc()
    assert result["status"] == "completed", result
    z = (np.arange(100000) + .5) * length / 100000
    signal = .09**2 * (1 + (z * .00105 / (math.pi * .09**2 * 1.55))**2)
    wt = .035**2 * (1 + (z * .000815 / (math.pi * .035**2 * 1.7))**2) + signal
    ws = .08**2 * (1 + (z * .000815 / (math.pi * .08**2 * 1.7))**2) + signal
    density = 2 / math.pi / np.sqrt(wt * ws) * np.exp(-2*(.022**2/wt + (z*.0014/1.7)**2/ws))
    expected = 1 / np.average(density,weights=np.exp(-.09*z))
    assert result["rows"][0]["area_mm2"] == pytest.approx(expected, rel=1e-9)
    pump["tangential"]["m2"] = 2
    assert calc()["status"] == "needs_input"
    pump["tangential"]["m2"] = 1
    law["alpha_per_mm"]["high"] = .18
    partial = calc()["rows"][0]
    assert "area_mm2" not in partial and "吸收" in partial["message"]


def test_registered_rays_traverse_a_clipped_pump_relay_and_keep_sample_identity():
    import numpy as np

    law = {"mode":"bulk", "alpha_per_mm":{"low":.04}, "entry":{"low":1}, "exit":{"low":1}}
    cells = [{"uid":name, "kind":"space", "name":name, "length_mm":12, "n":1.5} for name in ("first", "second")]
    arm = {"uid":"illum", "name":"illumination", "target_uid":"first", "through_uid":"second", "target_n":1.6,
           "downstream":[{"target_uid":"second", "n":1.6}], "wavelength_nm":780,
           "elements":[{"uid":"relay", "name":"entrance lens", "kind":"lens", "focal_mm":60,
                        "clear_aperture":{"shape":"ellipse", "width_mm":.3}}],
           "power_budget":{"wavelength_nm":780, "stages":[{"scope":"host", "element_uid":p["uid"], "law":law} for p in cells]}}
    layout = {"system":{"mode":"single_pass", "wavelength_nm":1000, "input_radius_mm":.12, "elements":cells},
              "incident_paths":[arm]}
    source = {"plane":"upstream lens face", "basis":"power weighted, +T/S", "wavelength_nm":780,
              "medium_n":1, "quality":"documented", "rays":[[.08,0,0,0,.25],[-.1,0,0,0,.5],[.21,0,0,0,.25]]}

    def compute(**options):
        return spatial_overlap(SpatialOverlap(layout=layout, pump_uid="illum", pump_bundle=source, **options))

    # At the input lens only the first two rays pass. Track them analytically in the two shared cells.
    first, second = compute()["rows"]
    for i, row in enumerate((first, second)):
        assert row["status"] == "completed", row
        assert row["element_uid"] == cells[i]["uid"]
        assert row["entrance_weight"] == .75 and row["positive_ray_count"] == 2
        assert row["source_basis"] == source["basis"] and row["quality"] == "documented"
        depth = (np.arange(40000) + .5) * 12 / 40000
        z = i * 12 + depth
        w2 = .12**2 * (1 + (z * .001 / (math.pi * 1.5 * .12**2))**2)
        ray_density = [2 / (math.pi * w2) * np.exp(-2 * (x * (1-z/(60*1.6)))**2 / w2) for x in (.08, -.1)]
        truth = 1 / np.average((ray_density[0] + 2*ray_density[1])/3, weights=np.exp(-.04*depth))
        assert row["area_mm2"] == pytest.approx(truth, rel=1e-9)

    # The known first segment remains comparable when only the second absorption is unknown.
    arm["power_budget"]["stages"][1]["law"] = {**law, "alpha_per_mm":{"low":.02, "high":.1}}
    partial = compute()
    assert partial["status"] == "partial" and partial["rows"][0]["area_mm2"] == first["area_mm2"]
    assert "area_mm2" not in partial["rows"][1]
    with pytest.raises(ValidationError, match="请选择"):
        SpatialOverlap(layout=layout, pump_bundle=source)
    with pytest.raises(ValidationError, match="独立"):
        SpatialOverlap(layout=layout, pump_uid="primary", pump_bundle=source)
    source["wavelength_nm"] = 900
    assert "780" in compute()["rows"][0]["message"] and "area_mm2" not in compute()["rows"][0]
    source.update(wavelength_nm=780,quality="limited")
    assert "适用条件" in compute()["rows"][0]["message"]
    source.update(quality="conditional",warnings=["Assumed emitter profile"])
    conditional = compute()
    assert conditional["rows"][0]["quality"] == "conditional"
    assert "Assumed emitter profile" in conditional["warnings"]

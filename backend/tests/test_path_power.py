import itertools
import math
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.workbench.observation import OpticalLayout, analyze_sections, power_for_layout
from app.workbench.optics import analyze_system


def span(uid, length, n=1):
    return {"uid":uid,"kind":"space","name":uid,"length_mm":length,"n":n}


def band(low, high=None):
    return {"low":low,"high":high}


def setup():
    components=[span("lead",12),span("front",2,1.45),span("back",3,1.5),span("exit",4)]
    stages=[{"scope":"arm","element_uid":"coupler","law":{"mode":"net","retained":band(.8)}},
            {"scope":"host","element_uid":"front","law":{"mode":"bulk","entry":band(.9),"exit":band(.95),"alpha_per_mm":band(math.log(2)/2)}},
            {"scope":"host","element_uid":"back","law":{"mode":"bulk","entry":band(1),"exit":band(.98),"internal_t":band(.8),"reference_mm":1,
                                                       "internal_loss_is_absorption":True}}]
    path={"uid":"pump","name":"test beam","target_uid":"front","target_n":1.56,"wavelength_nm":808,
          "tangential":{"wavefront":"waist","radius_mm":.12,"m2":1},"elements":[span("coupler",8)],
          "downstream":[{"target_uid":"back","n":1.68}],
          "power_budget":{"wavelength_nm":808,"input_w":band(10),"stages":stages}}
    return {"system":{"mode":"single_pass","input_radius_mm":.2,"elements":components,"wavelength_nm":1064},
            "incident_paths":[path],"observation_planes":[{"uid":"p1","name":"front exit","target_uid":"front","depth_mm":2},
                                                         {"uid":"p2","name":"inside back","target_uid":"back","depth_mm":1}]}


def solve(raw):
    layout=OpticalLayout.model_validate(raw)
    return analyze_sections(layout,analyze_system(layout.system,profile=False))


def test_geometry_power_and_absorption_share_multiple_physical_segments():
    result=solve(setup())
    assert result["status"]=="completed"
    inside=result["planes"][1]
    assert inside["z_mm"]==15
    reduced_distance=8+2/1.56+1/1.68
    expected=.12*math.hypot(1, reduced_distance*.000808/(math.pi*.12**2))
    assert inside["beams"][1]["radii_mm"]["tangential"]==pytest.approx(expected)
    budget=result["power_budgets"][0]
    assert budget["status"]=="completed"
    a,b,c=budget["rows"]
    assert a["unassigned"]["watts"]==pytest.approx([2,2]) and a["bulk_absorption"] is None
    assert [row["z_mm"] for row in budget["rows"]]==[4,12,14]
    assert b["outgoing"]["watts"]==pytest.approx([3.42,3.42])
    output=3.42*.8**3*.98
    absorbed=3.6+3.42*(1-.8**3)
    assert c["outgoing"]["watts"]==pytest.approx([output]*2)
    assert budget["summary"]["modeled_bulk_absorption"]["watts"]==pytest.approx([absorbed]*2)
    assert budget["summary"]["unassigned"]["watts"]==pytest.approx([10-absorbed-output]*2)
    assert budget["planes"][0]["forward"]["watts"]==pytest.approx([3.6]*2)
    assert budget["planes"][1]["forward"]["watts"]==pytest.approx([3.42*.8]*2)
    assert budget["planes"][1]["absorbed_in_segment"]["watts"]==pytest.approx([3.42*.2]*2)
    assert power_for_layout(OpticalLayout.model_validate(setup()))==[budget]


@pytest.mark.parametrize("attribute_front,attribute_back",itertools.product((True,False),repeat=2))
def test_interval_envelopes_match_exhaustive_corner_scenarios(attribute_front,attribute_back):
    raw=setup()
    budget=raw["incident_paths"][0]["power_budget"]
    budget["input_w"]=band(8,10)
    a,b,c=[s["law"] for s in budget["stages"]]
    a["retained"]=band(.7,.82)
    b.update(entry=band(.8,.92),alpha_per_mm=band(.11,.217),exit=band(.90,.99))
    c.update(entry=band(.95,1),internal_t=band(.75,.9),exit=band(.97,1))
    if not attribute_front:
        del b["alpha_per_mm"]
        b.update(internal_t=band(math.exp(-.217),math.exp(-.11)),reference_mm=1)
    c["internal_loss_is_absorption"]=attribute_back
    actual=solve(raw)["power_budgets"][0]["summary"]
    exhaustive=[]
    for p,t,s1,alpha,e1,s2,tau,e2 in itertools.product((8,10),(.7,.82),(.8,.92),(.11,.217),(.90,.99),(.95,1),(.75,.9),(.97,1)):
        q=p*t
        a1=q*s1*(1-math.exp(-alpha*2))
        q=q*s1*math.exp(-alpha*2)*e1
        a2=q*s2*(1-tau**3)
        q=q*s2*tau**3*e2
        modeled_absorption=(a1 if attribute_front else 0)+(a2 if attribute_back else 0)
        exhaustive.append((q,modeled_absorption,p-q-modeled_absorption))
    for i, key in enumerate(("outgoing","modeled_bulk_absorption","unassigned")):
        sample=[v[i] for v in exhaustive]
        assert actual[key]["watts"]==pytest.approx([min(sample),max(sample)],abs=1e-12)


def test_missing_inputs_disconnected_route_and_wavelength_are_explicit():
    raw=setup()
    path=raw["incident_paths"][0]
    del path["power_budget"]["stages"][0]
    path["target_n"]=None
    path["power_budget"]["input_w"]=None
    out=solve(raw)
    assert "上游" in out["planes"][1]["beams"][1]["notes"]["sagittal"]
    result=out["power_budgets"][0]
    assert result["status"]=="partial" and result["summary"]["outgoing"]["watts"] is None
    assert result["rows"][0]["outgoing"]["fraction"]==[0,1]
    assert result["summary"]["outgoing"]["fraction"][1] > 0
    raw["system"]["elements"].insert(2,{"uid":"lens","name":"intervening lens","kind":"lens","focal_mm":45})
    out=solve(raw)
    b=out["power_budgets"][0]
    assert not b["route_complete"] and [row["uid"] for row in b["rows"]]==["coupler","front"]
    assert b["unused_stages"]==[{"scope":"host","element_uid":"back"}]
    assert not out["planes"][1]["beams"][1]["radii_mm"]
    path["wavelength_nm"]=880
    b=solve(raw)["power_budgets"][0]
    assert b["status"]=="needs_input" and "summary" not in b


def test_independent_primary_budget_and_vendor_internal_transmission():
    raw={"system":{"mode":"single_pass","input_radius_mm":.2,"elements":[span("glass",2,1.505)],"wavelength_nm":546},
         "single_pass_power":{"wavelength_nm":546,"input_w":band(1),"stages":[{"scope":"host","element_uid":"glass","law":{
             "mode":"bulk","entry":band(1),"exit":band(1),"internal_t":{"low":.28,"high":.34,"basis":"datasheet",
             "source":"SCHOTT NG4, guaranteed internal transmittance at 546 nm for 1 mm"},"reference_mm":1}}]}}
    b=solve(raw)["power_budgets"][0]
    assert b["path_uid"]=="primary" and b["status"]=="completed"
    assert b["summary"]["outgoing"]["watts"]==pytest.approx([.28**2,.34**2])
    assert b["rows"][0]["law"]["internal_t"]["basis"]=="datasheet"
    assert b["rows"][0]["bulk_absorption"] is None
    assert b["summary"]["modeled_bulk_absorption"]["watts"]==[0,0]
    assert b["summary"]["unassigned"]["watts"]==pytest.approx([1-.34**2,1-.28**2])
    # Surface losses are separate. Blocking the entrance leaves no bulk absorption.
    raw["single_pass_power"]["stages"][0]["law"]["entry"]=band(0)
    b=solve(raw)["power_budgets"][0]
    assert b["summary"]["modeled_bulk_absorption"]["watts"]==[0,0]
    assert b["summary"]["unassigned"]["watts"]==[1,1]


def test_power_before_beam_measurement_and_outside_valid_geometry():
    raw=setup()
    del raw["system"]["input_radius_mm"]
    assert "半径" in solve(raw)["planes"][0]["beams"][0]["notes"]["tangential"]
    assert solve(raw)["power_budgets"][0]["status"]=="completed"
    raw["incident_paths"][0].update(target_uid="removed",elements=[])
    b=solve(raw)["power_budgets"][0]
    assert b["status"]=="needs_input" and "summary" not in b
    raw=setup()
    raw["system"]["elements"].append({"kind":"lens","name":"lens","focal_mm":50,"uid":"lens"})
    raw["single_pass_power"]={"wavelength_nm":1064}
    raw["observation_planes"]=[{"uid":"invalid","target_uid":"lens","depth_mm":0,"name":"invalid"}]
    assert not solve(raw)["power_budgets"][0]["planes"]
    law=raw["incident_paths"][0]["power_budget"]["stages"][-1]["law"]
    law.update(internal_t=band(1), reference_mm=1e-320)
    assert solve(raw)["power_budgets"][1]["rows"][-1]["bulk_absorption"]["watts"]==[0,0]


def test_spectral_provenance_and_weak_absorption():
    raw=setup()
    law=raw["incident_paths"][0]["power_budget"]["stages"][1]["law"]
    law["alpha_per_mm"]=band(1e-18)
    b=solve(raw)["power_budgets"][0]
    assert b["rows"][1]["bulk_absorption"]["watts"][0]==pytest.approx(8*.9*2e-18,abs=1e-29)
    for change in ({"internal_t":band(.8),"reference_mm":1},{"exit":band(1.01)},{"entry":band(.8,.4)},
                   {"entry":{"low":.8,"basis":"measured"}}):
        invalid=deepcopy(raw)
        invalid["incident_paths"][0]["power_budget"]["stages"][1]["law"].update(change)
        with pytest.raises(ValidationError):
            OpticalLayout.model_validate(invalid)


def test_power_save_preview_and_historical_geometry(client):
    case=client.post("/api/cases",json={"title":"coupled path"}).json()["id"]
    base=f"/api/cases/{case}/optical-project"
    raw=setup()
    preview=client.post(base+"/preview",json=raw).json()
    saved=client.post(base,json={"project":{"title":"serial train",**raw},"change_note":"source estimates"}).json()
    assert saved["analysis"]["sections"]["power_budgets"]==preview["sections"]["power_budgets"]
    old=saved["revision_id"]
    raw["system"]["elements"][2]["length_mm"]=5
    new=client.post(base,json={"project":{"title":"serial train",**raw},"base_revision_id":old,"change_note":"longer absorber"}).json()
    previous=client.get(base,params={"revision_id":old}).json()["current"]
    assert previous["analysis"]["sections"]["power_budgets"]==preview["sections"]["power_budgets"]
    assert new["analysis"]["sections"]["power_budgets"][0]["summary"]["outgoing"]["watts"][0] < preview["sections"]["power_budgets"][0]["summary"]["outgoing"]["watts"][0]

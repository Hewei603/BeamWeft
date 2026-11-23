import math

import numpy as np
import pytest

from app.workbench.network import PortNetwork, evaluate_network


def at(uid, port):
    return {"component": uid, "port": str(port)}


def splitter(uid):
    return {"uid": uid, "name": uid, "model": {"kind": "coupler", "through_fraction": .5}}


def link(a, b, length=0):
    return {"a": a, "b": b, "length_mm": length}


def solve(components, links, drives, wavelength=1000):
    n = PortNetwork(components=components, links=links, drives=drives)
    return evaluate_network(n, wavelength)


def watts(result, component, port):
    return next(p["outgoing_w"] for p in result["ports"] if p["component"] == component and p["port"] == str(port))


@pytest.mark.parametrize("difference_nm", [0, 125, 250, 500, 1000])
def test_interferometer_closed_form_and_power_balance(difference_nm):
    output = solve([splitter("A"), splitter("B")], [link(at("A",3), at("B",1), 25),
                   link(at("A",4), at("B",2), 25+difference_nm/1e6)], [{"at":at("A",1), "power_w":3}])
    assert output["status"] == "completed"
    expected = 3 * math.sin(math.pi*difference_nm/1000)**2
    assert watts(output,"B",3) == pytest.approx(expected, abs=1e-9)
    assert watts(output,"B",4) == pytest.approx(3-expected, abs=1e-9)
    assert output["islands"][0]["external_w"] == pytest.approx(3)


def test_mutual_coherence_is_explicit_and_labels_do_not_control_physics():
    drives = [{"at":at("Coupler/a",1),"power_w":1}, {"at":at("Coupler/a",2),"power_w":1}]
    def study():
        return solve([splitter("Coupler/a")], [], drives)

    assert watts(study(),"Coupler/a",3) == pytest.approx(1)
    for d,phase in zip(drives,(0,90)):
        d.update(coherence_group="共同频率基准",phase_deg=phase)
    assert watts(study(),"Coupler/a",3) == pytest.approx(0,abs=1e-24)
    assert watts(study(),"Coupler/a",4) == pytest.approx(2)
    drives[0]["phase_deg"]=None
    assert study()["status"]=="needs_input"


def test_exact_quadrature_and_small_departures_from_a_dark_fringe():
    arms=[link(at("A",3),at("B",1)),link(at("A",4),at("B",2))]
    def dark(phase):
        arms[1]["phase_deg"]=phase
        return watts(solve([splitter("A"),splitter("B")],arms,[{"at":at("A",1),"power_w":1}]),"B",4)
    assert dark(180)==0
    shift=1e-6
    assert dark(180+shift)==pytest.approx(math.sin(math.radians(shift)/2)**2,rel=1e-7,abs=0)


@pytest.mark.parametrize("length,resonant",[(0.00025,True),(0,False)])
def test_custom_bidirectional_mirrors_match_fabry_perot_airy(length,resonant):
    reflectance=.81
    r, t = [0,math.sqrt(reflectance)], [math.sqrt(1-reflectance),0]
    model={"kind":"matrix","ports":["outer","inner"],"matrix":[[r,t],[t,r]]}
    result=solve([{"uid":k,"name":k,"model":model} for k in "AB"],[link(at("A","inner"),at("B","inner"),length)],
                 [{"at":at("A","outer"),"power_w":1}])
    expected=1 if resonant else ((1-reflectance)/(1+reflectance))**2
    assert watts(result,"B","outer")==pytest.approx(expected)
    assert watts(result,"A","outer")==pytest.approx(1-expected,abs=1e-12)
    assert result["islands"][0]["removed_w"]==pytest.approx(0,abs=1e-12)


def test_invalid_wiring_and_passivity_fail_before_saving():
    left,right=splitter("left"),splitter("right")
    for links in ([link(at("left",1),at("right",5))], [link(at("left",1),at("left",1))]):
        with pytest.raises(ValueError,match="端口"):
            solve([left,right],links,[])
    active={"kind":"matrix","ports":["x","y"],"matrix":[[[.8,0],[.8,0]],[[.8,0],[.8,0]]]}
    with pytest.raises(ValueError,match="净增益"):
        solve([{"uid":"box","name":"外部规格","model":active}],[],[])


def test_an_underdetermined_loop_and_calibration_mismatch_are_local_to_their_island():
    model={"kind":"matrix","ports":["ext","a","b"],"matrix":[[[float(i==j),0] for j in range(3)] for i in range(3)]}
    components=[{"uid":"S","name":"未知闭环","model":model},splitter("free")]
    links=[link(at("S","a"),at("S","b"))]
    drives=[{"at":at("S","ext"),"power_w":2},{"at":at("free",1),"power_w":1}]
    result=solve(components,links,drives)
    assert result["status"]=="partial" and "奇异" in result["warnings"][0]
    assert watts(result,"free",3)==pytest.approx(.5)
    model["reference_wavelength_nm"]=850
    wrong=solve(components,[],drives)
    assert wrong["status"]=="partial" and "850 nm" in wrong["warnings"][0]
    assert not any(row["component"]=="S" for row in wrong["ports"])


def test_same_ordered_flux_cannot_be_claimed_twice():
    source={"path_uid":"primary","scope":"host","element_uid":"end"}
    with pytest.raises(ValueError,match="注入网络一次"):
        solve([splitter("A")],[],[{"at":at("A",i),"path_output":source} for i in (1,2)])


@pytest.mark.parametrize("seed", [7, 21, 44, 101])
def test_arbitrary_lossy_networks_match_independent_path_summation(seed):
    """Accumulate successive optical traversals instead of inverting the network equations."""
    rng = np.random.default_rng(seed)
    components, matrices, ends = [], {}, []
    for k in range(5):
        uid, n = f"custom/{seed}-{k}", int(rng.integers(3, 6))
        names = [f"p {i}" for i in range(n)]
        unitary, _ = np.linalg.qr(rng.normal(size=(n,n)) + 1j*rng.normal(size=(n,n)))
        matrix = unitary @ np.diag(rng.uniform(.25, .72, size=n))
        models = [[[float(v.real), float(v.imag)] for v in row] for row in matrix]
        components.append({"uid":uid, "name":uid, "model":{"kind":"matrix", "ports":names, "matrix":models}})
        matrices[uid] = matrix
        ends.extend((uid,i) for i in range(n))
    rng.shuffle(ends)
    external, wired = ends[:3], ends[3:]
    links, factors = [], []
    for a,b in zip(wired[::2], wired[1::2]):
        length, n, transmission = rng.uniform(0, .01), rng.uniform(1,2), rng.uniform(.5,1)
        links.append({**link(at(a[0],f"p {a[1]}"),at(b[0],f"p {b[1]}"),length), "n":n,"power_transmission":transmission})
        factors.append((a,b,np.sqrt(transmission)*np.exp(2j*np.pi*length*n*1e6/780)))
    a,b = external[:2]
    drives = [{"at":at(a[0],f"p {a[1]}"),"power_w":1,"coherence_group":"G","phase_deg":0},
              {"at":at(b[0],f"p {b[1]}"),"power_w":.36,"coherence_group":"G","phase_deg":35}]
    solution = solve(components[::-1],links[::-1],drives,780)
    current = {k:np.zeros(len(m),complex) for k,m in matrices.items()}
    current[a[0]][a[1]], current[b[0]][b[1]] = 1, .6*np.exp(1j*math.radians(35))
    total = {k:np.zeros_like(v) for k,v in current.items()}
    for _ in range(140):
        next_fields = {k:m@current[k] for k,m in matrices.items()}
        for k in next_fields:
            total[k] += next_fields[k]
            current[k].fill(0)
        for left,right,gain in factors:
            current[left[0]][left[1]] += gain*next_fields[right[0]][right[1]]
            current[right[0]][right[1]] += gain*next_fields[left[0]][left[1]]
    assert all(np.linalg.norm(v)<1e-14 for v in current.values())
    assert solution["ports"]
    for row in solution["ports"]:
        expected=abs(total[row["component"]][int(row["port"][2:])])**2
        assert row["outgoing_w"]==pytest.approx(expected,abs=5e-12)


def test_network_and_ordered_path_share_version_preview_and_typed_sources(client):
    cid=client.post("/api/cases",json={"title":"端口接单程"}).json()["id"]
    case=f"/api/cases/{cid}"
    project={"title":"支路在不同于主路的波长入射","system":{"mode":"single_pass","wavelength_nm":1064,"elements":[
        {"uid":"cell","kind":"space","name":"样品","length_mm":10}]},
        "incident_paths":[{"uid":"probe","name":"探针","target_uid":"cell","wavelength_nm":635,"power_budget":{
            "wavelength_nm":635,"input_w":{"low":4},"stages":[{"scope":"host","element_uid":"cell","law":{"mode":"net","retained":{"low":.5}}}]}}],
        "network":{"wavelength_nm":635,"components":[splitter("unit/a")],"drives":[{"at":at("unit/a",1),
            "path_output":{"path_uid":"probe","scope":"host","element_uid":"cell"}}]}}
    base=case+"/optical-project"
    preview=client.post(base+"/preview",json={k:v for k,v in project.items() if k!="title"})
    assert preview.status_code==200,preview.text
    first=client.post(base,json={"project":project,"change_note":"比对两种表示"}).json()
    assert first["analysis"]["network"]==preview.json()["network"]
    assert watts(first["analysis"]["network"],"unit/a",3)==pytest.approx(1)
    assert first["analysis"]["network"]["origins"][0]["watts"]==[2.,2.]
    ports=client.get(case+"/studies/project-outputs",params={"revision_id":first["revision_id"]}).json()["outputs"]
    assert any("unit%2Fa" in key and v["scope"]=="network" and v["value"]==pytest.approx(1) for key,v in ports.items())
    project["incident_paths"][0]["power_budget"]["input_w"]["high"]=8
    newer=client.post(base,json={"project":project,"base_revision_id":first["revision_id"],"change_note":"区间输入"}).json()
    assert newer["analysis"]["network"]["status"]=="needs_input"
    assert not newer["analysis"]["network"]["ports"]
    del project["network"]
    resaved=client.post(base,json={"project":project,"base_revision_id":newer["revision_id"],"change_note":"从顺序编辑器保存"}).json()
    assert resaved["project"]["network"]==newer["project"]["network"]
    removed=client.post(base,json={"project":{**project,"network":None},"base_revision_id":resaved["revision_id"],"change_note":"明确移除网络"}).json()
    assert removed["project"]["network"] is None and "network" not in removed["analysis"]
    isolated=solve([splitter("driven"),splitter("undriven")],[],[{"at":at("driven",2),"power_w":2}])
    assert isolated["status"]=="partial" and watts(isolated,"driven",4)==pytest.approx(1)

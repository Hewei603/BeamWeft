from copy import deepcopy
from itertools import combinations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from app.auth.security import Principal
from app.open_case import open_package
from app.workbench.component_models import LibraryQuery, PlaceModel, library, place_model


def at(uid, port):
    return {"component": uid, "port": str(port)}


def seed(client, title, layout):
    cid = client.post("/api/cases", json={"title": title}).json()["id"]
    root = f"/api/cases/{cid}"
    row = client.post(root + "/optical-project", json={"change_note": "setup", "project": {**layout, "title": title}})
    assert row.is_success, row.text
    return root, row.json()


def test_port_module_is_portable_but_its_excitation_and_external_wiring_are_case_local(client, tmp_path):
    def split(uid):
        return {"uid": uid, "name": "same name", "model": {"kind": "coupler", "through_fraction": 0.42}}

    nodes = [split(uid) for uid in ("upstream", "B/4", "A☉", "external")]
    nodes[1].update(diagram_xy=[300, 400], transverse_action="matched_mode")
    inside = {
        "a": at("A☉", 3),
        "b": at("B/4", 1),
        "length_mm": 0.125,
        "n": 1.46,
        "phase_deg": 13,
        "power_transmission": 0.94,
        "shift_t_mm": 0.29,
        "shift_s_mm": -0.4,
    }
    outside = {"a": at("B/4", 4), "b": at("external", 1), "length_mm": 7}
    net = {
        "wavelength_nm": 780,
        "components": nodes,
        "links": [inside, outside],
        "drives": [{"at": at("A☉", 1), "power_w": 5}],
    }
    system = {"mode": "single_pass", "wavelength_nm": 1064, "elements": []}
    origin, source = seed(client, "source", {"system": system, "network": net})
    capture = {
        "project_revision_id": source["revision_id"],
        "network_component_uids": ["A☉", "B/4"],
        "name": "module",
        "note": "780 nm calibration",
    }
    new = client.post(origin + "/component-models", json=capture)
    assert new.is_success, new.text
    entry = new.json()
    assert len(entry["boundary_ports"]) == 6 and entry["internal_links"] == 1
    capture["network_component_uids"].reverse()
    assert client.post(origin + "/component-models", json=capture).json() == entry
    snap = client.get(origin + "/component-models", params={"model_revision_id": entry["model_revision_id"]}).json()[
        "snapshot"
    ]
    assert "drives" not in snap["network"] and all("diagram_xy" not in c for c in snap["network"]["components"])

    cid = client.post("/api/cases", json={"title": "fresh"}).json()["id"]
    root = f"/api/cases/{cid}"
    req = {"model_revision_id": entry["model_revision_id"], "draft": {"title": "unsaved", "system": system}}

    def preview(request):
        resp = client.post(root + "/component-models/preview-placement", json=request)
        assert resp.is_success, resp.text
        return resp.json()

    first = preview(req)
    assert client.get(root + "/optical-project").json()["current"] is None
    second = preview({**req, "draft": first["project"]})
    result = second["project"]["network"]
    assert result["wavelength_nm"] == 780 and result["drives"] == []
    assert len(result["components"]) == 4 and len(result["links"]) == 2
    assert len({c["uid"] for c in result["components"]}) == 4
    for i, link in enumerate(result["links"]):
        b, a = result["components"][i * 2 : i * 2 + 2]
        assert b["transverse_action"] == "matched_mode"
        assert link["a"] == at(a["uid"], 3) and link["b"] == at(b["uid"], 1)
        assert {k: v for k, v in link.items() if k not in ("a", "b")} == {
            k: v for k, v in inside.items() if k not in ("a", "b")
        }
        assert a["component_origin"]["instance_uid"] == b["component_origin"]["instance_uid"]
    assert first["instance_uid"] != second["instance_uid"]
    saved = client.post(root + "/optical-project", json={"project": second["project"], "change_note": "assembly"})
    assert saved.is_success, saved.text
    assert client.post(root + "/component-models/preview-placement", json=req).status_code == 409
    bad = deepcopy(saved.json()["project"])
    bad["network"]["wavelength_nm"] = 532
    mismatch = client.post(
        root + "/component-models/preview-placement",
        json={**req, "draft": bad, "project_revision_id": saved.json()["revision_id"]},
    )
    assert mismatch.status_code == 422 and "780" in mismatch.text
    assert client.get(origin + "/optical-project").json()["current"]["project"] == source["project"]
    bundle = client.get(root + "/bundle")
    assert bundle.is_success, bundle.text if not bundle.is_success else ""
    file = tmp_path / "bench.zip"
    file.write_bytes(bundle.content)
    folder, _ = open_package(file, tmp_path / "received")
    engine = create_engine(URL.create("sqlite", database=str(folder / "laserclaw.db")))
    try:
        with Session(engine) as peer:
            owner = Principal(actor="anonymous", role="user")
            assert library(peer, owner, LibraryQuery())["models"] == [entry]
            third = place_model(
                peer,
                owner,
                cid,
                PlaceModel(
                    model_revision_id=entry["model_revision_id"], project_revision_id=saved.json()["revision_id"]
                ),
                preview=True,
            )
            assert len(third["project"]["network"]["components"]) == 6
    finally:
        engine.dispose()


def test_library_preserves_the_cut_for_every_selection_of_a_branched_loop(client):
    # Use only measured-matrix prescriptions with user-defined port names. The
    # expected cut depends on graph incidence, independent of any device catalog.
    ports = {
        "arm/A": ["外侧", "return"],
        "B": ["光轴", "feed", "spare"],
        "C": ["α", "β", "γ", "reference"],
        "D": ["single"],
    }
    components = []
    for uid, labels in ports.items():
        matrix = [[[0, 0.1] if i == j else [0, 0] for j in range(len(labels))] for i in range(len(labels))]
        components.append({"uid": uid, "name": "sample",
                           "model": {"kind": "matrix", "ports": labels, "matrix": matrix}})
    edges = [(("arm/A", "外侧"), ("B", "光轴")), (("B", "feed"), ("C", "α")),
             (("C", "β"), ("arm/A", "return")), (("C", "γ"), ("D", "single"))]
    wave = 633
    system = {"mode": "single_pass", "wavelength_nm": wave, "elements": []}
    network = {"components": components, "links": [{"a": at(*a), "b": at(*b), "length_mm": 3} for a, b in edges],
               "drives": [{"at": at("C", "reference"), "power_w": 1}]}
    root, saved = seed(client, "arbitrary multiport optics", {"system": system, "network": network})
    recipient, blank = seed(client, "fresh fixture", {"system": system})
    for count in range(1, len(ports) + 1):
        for selection in combinations(ports, count):
            reply = client.post(root + "/component-models", json={
                "project_revision_id": saved["revision_id"], "network_component_uids": list(reversed(selection)),
                "name": "test selection",
            })
            assert reply.is_success, reply.text
            model = reply.json()
            detail = client.get(root + "/component-models", params={"model_revision_id": model["model_revision_id"]})
            snapshot = detail.json()["snapshot"]
            originals = snapshot["source_element_uids"]
            assert set(originals) == set(selection)
            inner = {(a, b) for a, b in edges if a[0] in selection and b[0] in selection}
            all_ports = {(uid, port) for uid in selection for port in ports[uid]}
            expected_boundary = all_ports - {end for edge in inner for end in edge}
            assert {(originals[int(t["component"]) - 1], t["port"]) for t in model["boundary_ports"]} == expected_boundary
            placed = client.post(recipient + "/component-models/preview-placement", json={
                "project_revision_id": blank["revision_id"], "model_revision_id": model["model_revision_id"],
            })
            assert placed.is_success, placed.text
            graph = placed.json()["project"]["network"]
            to_original = {c["uid"]: originals[c["component_origin"]["ordinal"] - 1] for c in graph["components"]}
            restored = set()
            for link in graph["links"]:
                a, b = link["a"], link["b"]
                restored.add(((to_original[a["component"]], a["port"]), (to_original[b["component"]], b["port"])))
            assert restored == inner
            assert not graph["drives"]


def test_build_from_no_geometry_then_a_coincident_reference_plane(client):
    system = {"mode": "single_pass", "wavelength_nm": 850, "elements": [], "input_radius_mm": 0.45}
    root, blank = seed(client, "from scratch", {"system": system})
    assert blank["analysis"]["status"] == "needs_input" and not blank["analysis"]["planes"]
    outputs = client.get(root + "/studies/project-outputs", params={"revision_id": blank["revision_id"]}).json()[
        "outputs"
    ]
    assert outputs["v1/layout"]["port"]["shape"] == "state"
    assert (
        client.get(root + "/optical-project/model-input", params={"revision_id": blank["revision_id"]}).json()[
            "project"
        ]["system"]["elements"]
        == []
    )
    lens = {"uid": "only", "kind": "lens", "name": "first surface", "focal_mm": 50}
    reply = client.post(
        root + "/optical-project",
        json={
            "base_revision_id": blank["revision_id"],
            "change_note": "add surface",
            "project": {"title": "one surface", "system": {**system, "elements": [lens]}},
        },
    )
    assert reply.is_success, reply.text
    row = reply.json()["analysis"]["planes"]["tangential"]
    assert row["output"]["radius_mm"] == pytest.approx(0.45)
    assert row["output"]["curvature_mm"] == pytest.approx(-50)
    assert row["output"]["z_mm"] == 0

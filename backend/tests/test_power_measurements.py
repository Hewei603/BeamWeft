import hashlib
import io
import math

import pytest
from openpyxl import Workbook

from app.workbench.power_measurements import MeasurePower
from app.workbench.tools import compact


def test_large_meter_trace_keeps_late_global_extreme_in_agent_context():
    samples=[{"watts":float(i%2),"row":i+1} for i in range(311)]
    samples[263]["watts"]=100.
    samples[267]["watts"]=-4.
    summary=compact(samples)
    assert {263,267} <= set(summary["sampled_indices"])
    assert not summary["all_detected_extrema_retained"]


def case(client):
    return client.post("/api/cases", json={"title": "Meter validation"}).json()["id"]


def equipment(client, case_id, source_w=0.1, first_known=True):
    def span(uid, length):
        return {"uid": uid, "kind": "space", "name": uid, "length_mm": length}

    def stage(uid, law):
        return {"scope": "host", "element_uid": uid, "law": law}

    project = {
        "title": "reference channel",
        "system": {"mode": "single_pass", "wavelength_nm": 808, "elements": [span("arm", 4), span("sample", 2)]},
        "observation_planes": [
            {"uid": uid, "name": uid, "target_uid": "sample", "depth_mm": depth}
            for uid, depth in (("near", 0.5), ("far", 1.5), ("end", 2))
        ],
        "single_pass_power": {
            "wavelength_nm": 808,
            "input_w": {"low": source_w} if source_w is not None else None,
            "stages": [
                stage("arm", {"mode": "net", "retained": {"low": 1} if first_known else None}),
                stage(
                    "sample",
                    {
                        "mode": "bulk",
                        "entry": {"low": 0.8},
                        "exit": {"low": 0.9},
                        "internal_t": {"low": 0.8, "high": 0.9},
                        "reference_mm": 1,
                    },
                ),
            ],
        },
    }
    url = f"/api/cases/{case_id}/optical-project"
    obj = client.post(url, json={"project": project, "change_note": "fixture"})
    assert obj.status_code == 200, obj.text
    return url, obj.json()["revision_id"]


def attach(client, cid, name, raw):
    return client.post(f"/api/cases/{cid}/attachments", files={"file": (name, raw, "application/octet-stream")}).json()[
        "id"
    ]


def place(kind="after", uid="sample"):
    return {"kind": kind, "uid": uid, **({"scope": "host"} if kind != "plane" else {})}


def example(revision, fid):
    return {
        "revision_id": revision,
        "path_uid": "primary",
        "target": place(),
        "conditions": "bench A, matched acquisition",
        "source": {"kind": "table", "attachment_id": fid, "first_row": 2, "signal_column": 2, "reference_column": 3},
        "signal_unit": "mW",
        "reference_unit": "mW",
        "signal_bound_w": 0.001,
        "reference_bound_w": 0.001,
        "wavelength_nm": 808,
        "comparable_conditions": True,
        "independence": "held_out",
    }


def test_source_drift_and_versioned_rows(client):
    cid = case(client)
    url, rev = equipment(client, cid)
    data = b"time,post mW,source mW\n0,50,100\n1,100,200\n2,150,300\n"
    fid = attach(client, cid, "dual.csv", data)
    args = example(rev, fid)
    api = url + "/power-measurements"
    record = client.post(api, json=args).json()
    assert record["status"] == "completed"
    assert record["source_file"]["sha256"] == hashlib.sha256(data).hexdigest()
    assert [p["row"] for p in record["points"]] == [2, 3, 4]
    assert record["absolute"]["above"] == 2 and record["absolute"]["disjoint"] == 2
    assert record["normalized"]["above"] == record["normalized"]["below"] == 0
    assert record["normalized"]["mean"] == pytest.approx(0.5)
    assert record["transmission_model"] == pytest.approx([0.8**3 * 0.9, 0.8 * 0.9**3])
    assert record["points"][2]["prediction_at_reference_w"] == pytest.approx([0.3 * 0.8**3 * 0.9, 0.3 * 0.8 * 0.9**3])
    assert record["points"][0]["reading_bounds_fraction"] == pytest.approx([0.049 / 0.101, 0.051 / 0.099])
    assert client.post(api, json=args).json()["id"] == record["id"]
    history = url + f"/{rev}/power-measurements"
    assert len(client.get(history).json()["items"]) == 1
    assert client.get(history, params={"measurement_id": record["id"]}).json() == record
    previous = client.get(url).json()["current"]["project"]
    previous["single_pass_power"]["input_w"] = {"low": 0.2}
    newer = client.post(
        url, json={"project": previous, "base_revision_id": rev, "change_note": "changed source"}
    ).json()["revision_id"]
    assert client.get(url + f"/{newer}/power-measurements").json()["items"] == []
    assert (
        client.get(history, params={"measurement_id": record["id"]}).json()["location"]["model"]["watts"]
        == record["location"]["model"]["watts"]
    )


def test_conditional_subpath_cancels_unknowns_and_shared_surfaces(client):
    cid = case(client)
    url, rev = equipment(client, cid, None, False)
    f = attach(client, cid, "bench.txt", b"At position B: 82 mW. Simultaneous point A: 100 mW.")
    base = {
        **example(rev, f),
        "source": {
            "kind": "transcribed",
            "attachment_id": f,
            "locator": "single entry",
            "signal": 82,
            "reference": 100,
        },
    }

    def read(start, end):
        response = client.post(url + "/power-measurements", json={**base, "reference_target": start, "target": end})
        assert response.status_code == 200, response.text
        return response.json()

    study = read(place("plane", "near"), place("plane", "far"))
    assert study["absolute"]["compared"] == 0 and study["normalized"]["compared"] == 1
    assert study["transmission_model"] == pytest.approx([0.8, 0.9])
    assert study["points"][0]["normalized"]["side"] == "inside"
    assert read(place("plane", "end"), place())["transmission_model"] == pytest.approx([0.9, 0.9])
    assert read(place("after", "arm"), place("before", "sample"))["transmission_model"] == [1, 1]
    assert read(place("before", "sample"), place("plane", "near"))["transmission_model"] == pytest.approx(
        [0.8 * math.sqrt(0.8), 0.8 * math.sqrt(0.9)]
    )
    assert (
        client.post(
            url + "/power-measurements", json={**base, "reference_target": place(), "target": place("plane", "end")}
        ).status_code
        == 422
    )


def test_measurements_survive_missing_and_mismatched_conditions(client):
    cid = case(client)
    url, rev = equipment(client, cid)
    fid = attach(client, cid, "meter.csv", b"time,w,ref\n0,80,100\n1,-2,0\n2,3,0.2")
    args = example(rev, fid)
    def store(payload):
        return client.post(url + "/power-measurements", json=payload)
    result = store(args).json()
    assert result["status"] == "partial" and result["points"][1]["watts"] == -0.002
    assert result["points"][1]["normalized"] is None and result["points"][2]["reading_bounds_fraction"] is None
    assert result["normalized"]["count"] == 2
    for edit in ({"wavelength_nm": 1064}, {"wavelength_nm": None}, {"comparable_conditions": False}):
        r = store({**args, **edit}).json()
        assert r["status"] == "partial" and r["absolute"]["compared"] == r["normalized"]["compared"] == 0
        assert r["absolute"]["count"] == 3
    foreign = case(client)
    foreign_url, foreign_rev = equipment(client, foreign)
    assert store({**args, "revision_id": foreign_rev}).status_code == 422
    assert (
        client.post(foreign_url + "/power-measurements", json={**args, "revision_id": foreign_rev}).status_code == 422
    )
    assert (
        client.get(
            foreign_url + f"/{foreign_rev}/power-measurements", params={"measurement_id": result["id"]}
        ).status_code
        == 404
    )
    r = store({**args, "target": place(uid="obsolete")})
    assert r.status_code == 422 and "指定的测点" in r.text


def test_unit_conversion_workbook_locale_and_bad_cells(client):
    cid = case(client)
    url, rev = equipment(client, cid)
    book = Workbook()
    s = book.active
    s.title = "instrument"
    s.append(["sample", "dBm", "nW"])
    s.append(["A", -10, 200000])
    s.append(["B", 0, 2000000])
    buf = io.BytesIO()
    book.save(buf)
    fid = attach(client, cid, "meter.xlsx", buf.getvalue())
    args = example(rev, fid)
    args.update(signal_unit="dBm", reference_unit="nW")
    args["source"]["sheet"] = "instrument"
    api = url + "/power-measurements"
    result = client.post(api, json=args).json()
    assert [p["watts"] for p in result["points"]] == pytest.approx([0.0001, 0.001])
    assert result["normalized"]["mean"] == pytest.approx(0.5)
    fid = attach(client, cid, "eu.txt", "t;value;ref\n0;50,25;100,5".encode("utf-16"))
    args = example(rev, fid)
    args["source"].update(delimiter="semicolon", decimal_mark=",")
    assert client.post(api, json=args).json()["normalized"]["mean"] == pytest.approx(0.5)
    for changes in ({"last_row": 7}, {"signal_column": 3}, {"decimal_mark": "."}):
        assert client.post(api, json={**args, "source": {**args["source"], **changes}}).status_code == 422
    with pytest.raises(ValueError):
        MeasurePower.model_validate(
            {
                **args,
                "source": {"kind": "transcribed", "attachment_id": fid, "locator": " ", "signal": 2, "reference": 8},
            }
        )


def test_tap_calibration_after_log_unit_conversion_and_archive_integrity(client, db):
    from app.models import Attachment

    cid = case(client)
    url, rev = equipment(client, cid)
    fid = attach(client, cid, "tap.csv", b"time,dBm,mW\n0,-10,100\n")
    args = {
        **example(rev, fid),
        "signal_unit": "dBm",
        "signal_correction": {"dark_w": 2e-5, "scale": 500, "source": "fixture: signed zero and beam tap"},
        "signal_bound_w": 0.002,
        "reference_bound_w": 0,
    }
    api = url + "/power-measurements"
    result = client.post(api, json=args).json()
    p = result["points"][0]
    assert p["raw_signal"] == -10 and p["meter_w"] == pytest.approx(0.0001)
    assert p["watts"] == pytest.approx(0.04) and p["fraction"] == pytest.approx(0.4)
    assert p["reading_bounds_fraction"] == pytest.approx([0.38, 0.42])
    assert p["normalized"]["disjoint_with_bound"]
    db.get(Attachment, fid).content_hash = "f" * 64
    db.commit()
    rejected = client.post(api, json=args)
    assert rejected.status_code == 422 and "校验" in rejected.text
    assert (
        client.get(url + f"/{rev}/power-measurements", params={"measurement_id": result["id"]}).json()["points"]
        == result["points"]
    )


@pytest.mark.parametrize("model_wavelength",[850,None])
def test_meter_on_a_reference_plane_before_lengths_and_beam_radius_are_known(client,model_wavelength):
    cid = case(client)
    base = f"/api/cases/{cid}/optical-project"
    project = {
        "title": "lumped component",
        "system": {
            "mode": "single_pass",
            "wavelength_nm": model_wavelength,
            "elements": [{"kind": "space", "uid": "device", "name": "reference face", "length_mm": 0}],
        },
    }
    saved = client.post(base, json={"project": project, "change_note": "geometry unknown"})
    assert saved.status_code == 200, saved.text
    rev = saved.json()["revision_id"]
    fid = attach(client, cid, "meter.txt", b"published reading: 0.91 W")
    args = {
        "revision_id": rev,
        "path_uid": "primary",
        "target": place(uid="device"),
        "conditions": "filter transmission",
        "source": {"kind": "transcribed", "attachment_id": fid, "locator": "line 1", "signal": 0.91},
        "signal_unit": "W",
        "wavelength_nm": 850,
        "comparable_conditions": True,
    }
    item = client.post(base + "/power-measurements", json=args).json()
    assert item["status"] == "partial" and item["location"]["model"] is None
    assert item["absolute"]["mean"] == 0.91 and item["absolute"]["compared"] == 0

"""An instrument export can be inspected before an optical project or an Agent exists."""

from io import BytesIO
from hashlib import sha256
from types import SimpleNamespace

import pytest
from openpyxl import Workbook


def case(client):
    return "/api/cases/" + str(client.post("/api/cases", json={"title": "原件优先"}).json()["id"])


def upload(client, root, name, data):
    response = client.post(root + "/attachments", files={"file": (name, data, "application/octet-stream")})
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.mark.parametrize(("selection", "counts", "expected", "irrelevant"), [
    ({"last_row":3,"x_lower":10,"x_upper":20}, (2,2,0,0), ["2 行全部", "10.0 ≤ 横轴 ≤ 20.0", "修改或清除"], ["数值格式","纵轴"]),
    ({"last_row":3,"x_lower":10}, (2,2,0,0), ["横轴 ≥ 10.0", "全部"], ["横轴 ≤","纵轴"]),
    ({"last_row":3,"x_upper":0}, (2,2,0,0), ["横轴 ≤ 0.0", "全部"], ["横轴 ≥","纵轴"]),
    ({"first_row":5,"last_row":6}, (2,0,2,2), ["2 行未能读取数值横坐标"], ["窗口","纵轴"]),
    ({"first_row":4,"last_row":4}, (1,0,1,0), ["1 行有可用横坐标", "纵轴单元格"], ["窗口","横轴列"]),
    ({"last_row":6,"x_lower":3,"x_upper":3}, (5,2,3,2), ["2 行在所选横轴窗口", "2 行未能读取", "1 行有可用横坐标"], ["全部"]),
    ({"first_row":100}, (0,0,0,0), ["第 100 行", "没有读到记录", "数据首行"], ["窗口","数值格式"]),
])
def test_empty_read_uses_actual_window_and_row_diagnostics(client, selection, counts, expected, irrelevant):
    from app.workbench.handoff import LIMIT, build_handoff

    root = case(client)
    fid = upload(client, root, "signal.csv", b"x,y\n1,2\n2,4\n3,ERR\nERR,3\nERR,ERR\n6,5\n")
    response = client.post(root+"/measurement-view", json={
        "attachment_id": fid, "x_column":1, "y_column":2, "first_row":2, "delimiter":"comma", **selection,
    })
    assert response.status_code == 200
    out = response.json()
    assert out["status"] == "needs_input" and out["valid"] == 0
    assert tuple(out[key] for key in ("scanned","outside","skipped","missing_x")) == counts
    assert all(text in out["message"] for text in expected)
    assert all(text not in out["message"] for text in irrelevant)
    call = SimpleNamespace(id=5, tool_name="inspect_measurement_table", status="needs_input", output_json=out)
    handoff = build_handoff(SimpleNamespace(tool_calls=[call]), LIMIT)
    assert handoff["diagnostics"][0]["tool_call_id"] == 5
    assert expected[0] in handoff["diagnostics"][0]["summary"]


def test_content_based_table_and_complete_extrema_outside_display_sample(client):
    root = case(client)
    cells = ["Scope: test", "time_us\tv1_mV\tv2_mV"]
    # A backwards sweep and a repeated x make point identity depend on the original record number.
    for i in range(11000):
        x = i if i < 7000 else 13999 - i
        y = 22 if i in (8125, 8126) else -7 if i == 10004 else (i % 7)
        cells.append(f"{x}\t{y if i != 7500 else 'overrange'}\t{float(-i)}")
    data = ("\n".join(cells) + "\n").encode()
    fid = upload(client, root, "session.InstrumentExport", data)
    api = root + "/measurement-view"
    assert client.get(f"/api/attachments/{fid}/preview").json()["table_candidate"]
    r = client.post(api, json={"attachment_id": fid})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["selection"]["delimiter"] == "tab" and out["selection"]["first_row"] == 3
    assert out["labels"]["1"] == "time_us" and out["labels"]["2"] == "v1_mV"
    assert out["minimum"] == [10007, 3995, -7] and out["maximum"] == [8128, 5874, 22]
    assert out["sampled"] and len(out["points"]) <= 1536
    assert out["skipped_examples"] == [7503] and out["scanned"] == 11000 and out["valid"] == 10999
    assert out["points"] == sorted(out["points"]) and out["maximum"] in out["points"]
    assert out["source"]["sha256"] == sha256(data).hexdigest()
    roi = client.post(api, json={"attachment_id": fid, "x_lower": 5874, "x_upper": 5874}).json()
    assert roi["points"] == [[5877, 5874, 5874 % 7], [8128, 5874, 22]]
    assert roi["valid"] + roi["outside"] + roi["skipped"] == roi["scanned"]
    assert client.post(api, json={"attachment_id": fid, "x_lower": 1, "x_upper": 0}).status_code == 422
    chosen = {"attachment_id": fid, "x_column": None, "y_column": 3, "first_row": 8000, "last_row": 8002}
    assert client.post(api, json=chosen).json()["points"] == [[n, n, -(n - 3)] for n in range(8000, 8003)]
    assert client.post(api, json={**chosen, "last_row": 12000}).status_code == 422
    assert client.post(case(client) + "/measurement-view", json=chosen).status_code == 422
    assert client.get(root + "/optical-project").json()["current"] is None
    assert client.get(f"/api/attachments/{fid}").content == data


def test_workbook_sheets_gap_and_text_decimal_conventions(client):
    root = case(client)
    buf = BytesIO()
    book = Workbook()
    book.active.title = "Notes"
    book.active.append(["unlabelled acquisition"])
    tab = book.create_sheet("Scope A")
    for row in [["time", "volts"], [0.1, None], [0.2, 2], [0.3, -1], [0.4, True], [0.5, "=1+2"]]:
        tab.append(row)
    book.save(buf)
    fid = upload(client, root, "two.xlsx", buf.getvalue())
    api = root + "/measurement-view"
    options = client.post(api, json={"attachment_id": fid}).json()
    assert options["sheets"] == ["Notes", "Scope A"] and options["status"] == "needs_input"
    manual = {"attachment_id": fid, "sheet": "Scope A", "first_row": 2, "x_column": 1, "y_column": 2}
    data = client.post(api, json=manual).json()
    assert data["points"] == [[3, 0.2, 2], [4, 0.3, -1]] and data["skipped_examples"] == [2, 5, 6]
    assert client.post(api, json={**manual, "first_row": 7}).json()["status"] == "needs_input"
    assert client.post(api, json={**manual, "sheet": "Missing"}).status_code == 422
    text = '"epoch";"v"\r\n"cycle\nA";1,5\r\nB;-2,5\r\nC;N/A\r\n'
    fid = upload(client, root, "locale.csv", text.encode("utf-16"))
    req = {
        "attachment_id": fid,
        "delimiter": "semicolon",
        "decimal_mark": ",",
        "x_column": None,
        "y_column": 2,
        "first_row": 2,
    }
    series = client.post(api, json=req).json()
    assert series["points"] == [[2, 2, 1.5], [3, 3, -2.5]] and series["skipped_examples"] == [4]
    fid = upload(client, root, "rail.dat", b"# exported table\n10   0.123\n11   0.125\n")
    series = client.post(api, json={"attachment_id": fid}).json()
    assert series["selection"]["delimiter"] == "whitespace" and series["points"] == [[2, 10, 0.123], [3, 11, 0.125]]
    fid = upload(client, root, "unreadable.wfm", b"\x02\x00\x03\x00\x00\x01\x02")
    assert client.get(f"/api/attachments/{fid}/preview").json()["status"] == "unsupported"
    assert client.post(api, json={"attachment_id": fid}).status_code == 422


def test_locale_detection_and_equally_plausible_single_channel(client):
    root = case(client)
    api = root + "/measurement-view"
    fid = upload(client, root, "europe.csv", b"seconds;volts\n0,5;1,0\n0,6;1,2\n")
    result = client.post(api, json={"attachment_id": fid}).json()
    assert result["selection"]["delimiter"] == "semicolon" and result["selection"]["decimal_mark"] == ","
    assert result["points"] == [[2, 0.5, 1], [3, 0.6, 1.2]]
    fid = upload(client, root, "ambiguous.csv", b"1,5\n2,7\n")
    result = client.post(api, json={"attachment_id": fid}).json()
    assert result["status"] == "needs_input" and "points" not in result
    for delimiter, mark, expected in [
        ("single", ",", [[1, 1, 1.5], [2, 2, 2.7]]),
        ("comma", ".", [[1, 1, 5], [2, 2, 7]]),
    ]:
        result = client.post(api, json={"attachment_id": fid, "delimiter": delimiter, "decimal_mark": mark}).json()
        assert result["points"] == expected

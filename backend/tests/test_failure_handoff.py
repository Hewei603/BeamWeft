from types import SimpleNamespace

import pytest

from app.providers.base import ModelTurnError
from app.providers.codex_exec import _failure
from app.workbench import service
from app.workbench.handoff import CANCELLED, LIMIT, UNKNOWN_OWNER, exception_cause


def call(number, name, payload, status="completed"):
    return SimpleNamespace(id=number, tool_name=name, status=status, output_json=payload)


def task(*calls, status="failed"):
    return SimpleNamespace(tool_calls=list(calls), status=status)


def test_handoff_short_summary_prioritizes_actual_science_and_retains_chronology():
    records = [
        call(1, "search_capabilities", {"items":[{"name":"a"}]}),
        call(2, "inspect_tool", {"name":"read_attachment"}),
        call(3, "inspect_tool", {"name":"begin_optical_study"}),
        call(4, "begin_optical_study", {"model_basis":{"revision_id":22,"kind":"draft"}}),
        call(5, "inspect_measurement_table", {"valid":3,"maximum":[7,532.1,2.176],
             "source":{"path":"bench.csv"},"selection":{"x_column":1,"y_column":2},
             "labels":{"1":"wavelength (nm)","2":"power (mW)"}}),
        call(6, "synthetic_plugin", {"signal":{"power":0.002340000000000001},"study_record":{"id":28,"title":"独立光功率",
             "available_outputs":{"signal.power":{"shape":"scalar","label":"功率","unit":"W"}}}}),
    ]
    result = service.interrupted_result(task(*records), LIMIT)
    handoff = result["failure_handoff"]
    assert result["status"] == "partial" and result["evidence_tool_call_ids"] == list(range(1,7))
    assert [r["kind"] for r in handoff["progress"]] == ["discovery"]*3+["snapshot","source","calculation"]
    assert handoff["progress"][1]["summary"] != handoff["progress"][2]["summary"]
    assert "约为 0.00234 W" in result["summary"] and "2.176" in result["summary"] and "#22" in result["summary"]
    assert records[-1].output_json["signal"]["power"] == 0.002340000000000001
    assert "532.1" in handoff["progress"][4]["summary"] and "power (mW)" in handoff["progress"][4]["summary"]
    assert "记录 #6" in handoff["progress_summary"] and "记录 #1" not in handoff["progress_summary"]
    discovery = service.interrupted_result(task(*records[:3]), LIMIT)["failure_handoff"]
    assert "尚无本轮科学计算结果" in discovery["remaining"]


def test_failed_and_partial_rows_offer_bounded_specific_diagnostics():
    rows = [{"status":"needs_input","message":f"请核对第 {i} 个观测面的列号。"} for i in range(7)]
    run = task(call(9,"plugin_rows",{"status":"partial","rows":rows},"partial"),
               call(10,"inspect_measurement_table",{"status":"needs_input","rows":[{"message":"缺少数值列。"}]},"needs_input"),
               call(11,"plugin",{"failure_code":"invalid_tool_arguments","invalid_fields":["settings.gain"]},"failed"))
    result = service.interrupted_result(run, LIMIT)
    assert result["evidence_tool_call_ids"] == [9]
    assert result["diagnostic_tool_call_ids"] == [9,10,11]
    diagnostic = result["failure_handoff"]["diagnostics"]
    assert "第 0 个" in diagnostic[0]["summary"] and "其余 5 条" in diagnostic[0]["summary"]
    assert "缺少数值列" in diagnostic[1]["summary"] and "settings.gain" in diagnostic[2]["summary"]
    assert any(a["actor"] == "Agent" for a in result["failure_handoff"]["next_steps"])


@pytest.mark.parametrize(("cause", "state"), [(UNKNOWN_OWNER,"unverified"), (CANCELLED,"cancelled")])
def test_pending_without_saved_output_never_claims_completion(cause, state):
    result = service.interrupted_result(task(call(8,"read_attachment",None,"running"),status=state),cause)
    assert result["status"] == state and not result["evidence_tool_call_ids"]
    assert result["diagnostic_tool_call_ids"] == [8]
    if state == "unverified":
        assert [action["kind"] for action in result["failure_handoff"]["next_steps"]] == ["check_owner"]


def test_cli_contradictory_events_are_evidence_not_a_diagnosis_of_local_network():
    exc = _failure("stream disconnected / private fixture text",0,completed=True,reported_error=True)
    cause = exception_cause(exc)
    assert cause.category == "connection" and "退出码 0" in cause.basis
    assert "错误和完成事件" in cause.basis and "不足以确定" in cause.basis
    assert "private fixture" not in str(exc)
    assert exception_cause(ModelTurnError("模型拒绝了本轮请求。",refused=True)).category == "model_refusal"

"""Read-only handoff from recorded host signals and tool outputs, even when the model is offline."""
from dataclasses import dataclass
import math

from .contracts import FailureHandoff
from .tools import capability_title
from .studies import _output_value


@dataclass(frozen=True)
class Cause:
    category: str
    reason: str
    basis: str
    actor: str
    action: str


def exception_cause(exc):
    kind = type(exc).__name__
    if kind == "CodexExecError":
        return Cause(exc.category, exc.reason, exc.basis, "后端运行者", exc.action)
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    error = body.get("error", body) if isinstance(body, dict) else {}
    error = error if isinstance(error, dict) else {}
    basis = f"供应商接口返回 HTTP {status}。"
    parameter = getattr(exc, "param", None) or error.get("param")
    if status in (400, 422) and parameter in ("reasoning", "reasoning.effort", "reasoning_effort"):
        return Cause("model_rejected", "模型接口拒绝了推理档位。", basis+"拒绝项为推理参数。", "配置负责人",
                     "请在 Agent 配置中选择自动或该模型支持的档位，保存后继续任务。")
    if status in (400, 404, 422) and (parameter == "model" or error.get("code") == "model_not_found"):
        return Cause("model_rejected", "当前接口无法使用指定模型。", basis+"错误指向模型选择。", "配置负责人",
                     "核对该供应商的模型 ID、部署名称和账户权限，再从原目标继续。")
    if status == 401:
        return Cause("authentication", "模型 API 认证失败（HTTP 401）。", basis, "配置负责人",
                     "请在 Agent 配置核对服务商和对应的 API Key。若使用自定义接口，也请核对地址，保存后继续任务。")
    if status == 403:
        return Cause("permission", "模型 API 拒绝访问（HTTP 403）。", basis, "账户管理员",
                     "请核对供应商项目和所选模型的访问权限，或在 Agent 配置选择已获授权的模型。")
    if status == 402:
        return Cause("payment", "模型 API 要求付款（HTTP 402）。", basis, "账户管理员",
                     "请在供应商账户核对计费状态、余额及用量上限，恢复服务后继续任务。")
    if status == 429 or kind == "RateLimitError":
        fields = [getattr(exc, "code", None), error.get("code"), error.get("type")]
        codes = {v for v in fields if isinstance(v, str)}
        if codes & {"credit_balance_exhausted", "insufficient_quota", "billing_hard_limit_reached"}:
            return Cause("quota", "模型 API 账户额度不足（HTTP 429）。", "接口返回了明确的额度错误码。", "账户管理员",
                         "请在供应商账户核对余额和项目预算，恢复可用额度后继续任务。")
        if codes & {"rate_limit_exceeded", "rate_limit_error", "requests", "tokens"}:
            return Cause("rate_limit", "模型 API 请求速率已达上限（HTTP 429）。", "接口返回了明确的速率错误码。", "请求发起者",
                         "请等供应商的限流窗口结束后继续任务，或降低并发请求数。")
        return Cause("limit_unspecified", "模型 API 返回 HTTP 429。", "返回信息未能区分账户额度和速率限制。", "账户管理员",
                     "请核对供应商的限流和账户额度记录，等待限流窗口结束或恢复额度后继续任务。")
    if kind in {"APIConnectionError", "APITimeoutError"}:
        timed_out = kind == "APITimeoutError"
        return Cause("timeout" if timed_out else "connection", "等待模型 API 响应超时。" if timed_out else "模型 API 连接中断。",
                     "SDK 未取得完整响应。现有记录无法定位故障发生在本机、代理还是远端服务。", "后端运行者",
                     "请核对配置的接口地址，以及运行后端的电脑到该地址的网络和代理，再继续任务。")
    if kind == "ModelTurnError":
        if exc.refused:
            return Cause("model_refusal", str(exc), "服务商返回了明确的拒答或内容过滤标记，未提供可执行的工具请求。",
                         "实验者", "核对当前任务的可执行范围，查看保留的依据，再决定是否使用可用的手工分析工具。")
        return Cause("model_response", str(exc), "模型适配器未取得可验证的完整工具请求。", "配置负责人",
                     "核对模型和接口是否支持此工具协议，可选择支持的模型后继续原任务。")
    if isinstance(status, int):
        return Cause("provider_response", "模型接口未接受请求或未能完成响应。", basis+"具体原因尚未确认。", "配置负责人",
                     "核对服务商状态、接口和模型配置，恢复后继续原任务。")
    return Cause("execution_error", "工作台本轮执行中断。", f"执行器记录了 {kind}，现有记录不足以确定根因。",
                 "工作台维护者", "查看本轮执行记录和后端运行日志，按最后确认的步骤继续。")


CANCELLED = Cause("cancelled", "本轮任务已停止。", "工作台记录了停止状态。", "实验者",
                  "若仍需原目标，可先核对已保存的记录，再点「继续任务」开始新一轮。")
UNKNOWN_OWNER = Cause("owner_unverified", "暂时无法核实原执行进程的状态。", "当前窗口未取得可确认进程存活或退出的依据。",
                      "实验者", "回到原执行窗口或点击「重新检查状态」。确认要结束原轮次后再继续。")
RELEASED = Cause("owner_released", "原执行进程已退出。", "该轮次的执行归属已释放，未取得可验证的模型最终答复。",
                 "实验者", "核对已保存的工具记录，点击「继续任务」载入原目标和依据。")
UNKNOWN_STOP = Cause("interrupted", "本轮运行中断。", "已有记录未保存可验证的模型最终答复，无法据此判定根因。",
                     "实验者", "先查看保留的执行记录，再点击「继续任务」载入原目标和依据。")
LIMIT = Cause("execution_limit", "本轮达到工具执行上限。", "执行器已用尽本轮允许的工具轮次或调用次数。",
              "实验者", "核对现有结果，优先选择尚待验证的一项，再从原任务继续。")


def _label(value, fallback):
    return " ".join(value.split())[:160] if isinstance(value, str) and value.strip() else fallback


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _table_summary(out):
    source = out.get("source") or {}
    title = _label(source.get("path"), "归档表格")
    text = f"已从《{title}》的所选范围读取 {out['valid']} 个有效点。"
    point = out.get("maximum")
    if isinstance(point, (tuple, list)) and len(point) == 3 and all(_number(v) for v in point):
        selection, labels = out.get("selection") or {}, out.get("labels") or {}
        def axis(key, default):
            column = selection.get(key)
            return _label(labels.get(str(column), labels.get(column)), default)
        x = axis("x_column", "记录号" if selection.get("x_column") is None else "横轴列")
        y = axis("y_column", "纵轴列")
        text += f" 所选范围内 {y} 最大值为 {point[2]:g}，对应 {x} 为 {point[1]:g}（原件第 {point[0]} 行）。"
    return text


def _progress(call):
    name, out = call.tool_name, call.output_json
    title = capability_title(name)
    kind, summary = "operation", f"已保存「{title}」的返回记录。"
    if name in {"search_capabilities", "inspect_tool", "read_project_outputs"}:
        count = len(out["items"]) if isinstance(out.get("items"), list) else None
        method = out.get("name") if name == "inspect_tool" else None
        kind, summary = "discovery", (f"已列出 {count} 项可用方法或操作。" if count is not None else
                                     f"已查阅「{capability_title(method) if isinstance(method, str) else title}」的说明。")
    elif name == "begin_optical_study" and isinstance(out.get("model_basis"), dict):
        rid = out["model_basis"].get("revision_id")
        kind, summary = "snapshot", (f"独立试算已绑定模型快照 #{rid}。" if rid else "已打开独立试算，此时没有已存模型版本。")
    elif name == "inspect_measurement_table" and isinstance(out.get("valid"), int):
        kind, summary = "source", _table_summary(out)
    elif name == "read_attachment":
        rid = out.get("attachment_id")
        if rid:
            kind, summary = "source", f"已读取原件 #{rid}《{_label(out.get('filename'), '归档资料')}》。"
        else:
            kind, summary = "discovery", "已查阅归档文件目录。"
    elif isinstance(out.get("study_record"), dict) and out["study_record"].get("id"):
        study = out["study_record"]
        verb = {"reused":"已复用", "computed":"已保存"}.get(study.get("execution"), "工具返回")
        kind, summary = "calculation", f"{verb}计算记录 #{study['id']}《{_label(study.get('title'),title)}》。"
        ports = study.get("available_outputs")
        if isinstance(ports, dict):
            for key, port in ports.items():
                if isinstance(port, dict) and port.get("shape") == "scalar" and isinstance(key, str):
                    value = _output_value(out, key)
                    if _number(value):
                        display = format(value, ".8g")
                        relation = "约为" if float(display) != value else "="
                        summary += f" {_label(port.get('label'),key)} {relation} {display} {_label(port.get('unit'), '')}。"
                        break
    elif isinstance(out.get("revision_id"), int):
        kind, summary = "snapshot", f"「{title}」返回了工程版本 #{out['revision_id']}。"
    elif name.startswith("read_"):
        kind, summary = "source", f"已查阅「{title}」并保存返回记录。"
    if call.status == "partial":
        summary += " 该工具仅返回了部分结果。"
    return {"tool_call_id":call.id, "kind":kind, "summary":summary}


def summary_progress(progress):
    priority = {"calculation":0, "snapshot":1, "source":2, "operation":3, "discovery":4}
    ordered = sorted(progress, key=lambda r:(priority[r["kind"]], -r["tool_call_id"]))
    return " ".join(f"{r['summary']}（记录 #{r['tool_call_id']}）" for r in ordered[:3])


def _row_problems(out):
    rows = out.get("rows")
    if not isinstance(rows, list):
        return ""
    examples, count = [], 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        state = row.get("status", row.get("state"))
        if state not in ("failed","needs_input","partial","unsupported","invalid","unavailable") and not (state is None and out.get("status") in ("needs_input","failed")):
            continue
        message = _label(row.get("message"), "")
        if message:
            count += 1
            if len(examples) < 2 and message not in examples:
                examples.append(message)
    if not count:
        return ""
    suffix = f" 其余 {count-len(examples)} 条行记录可展开核对。" if count > len(examples) else ""
    return f" {count} 条行记录给出了原因："+" ".join(examples)+suffix


def build_handoff(task, cause, *, remaining=None, actions=(), diagnostic_ids=None):
    progress, diagnostics = [], []
    validation_issue = missing_source = False
    for call in sorted(task.tool_calls, key=lambda c:c.id):
        if call.tool_name in {"workbench_request", "finish"}:
            continue
        out = call.output_json if isinstance(call.output_json, dict) else {}
        if call.status in {"completed", "partial"} and out:
            progress.append(_progress(call))
        row_problems = _row_problems(out) if call.status in {"partial", "needs_input", "failed"} else ""
        if (call.status in {"failed", "needs_input", "cancelled", "running"} or row_problems) and (diagnostic_ids is None or call.id in diagnostic_ids):
            summary = f"「{capability_title(call.tool_name)}」尚无确认完成的结果。"
            if call.status == "partial":
                summary = f"「{capability_title(call.tool_name)}」的部分结果仍有缺项。"
            elif call.status == "needs_input":
                summary = f"「{capability_title(call.tool_name)}」需要补充输入。"+_label(out.get("message"),"请打开记录核对所缺条件。")
            elif out.get("failure_code") == "invalid_tool_arguments":
                validation_issue = True
                summary = f"「{capability_title(call.tool_name)}」的调用参数未通过校验。"
                if out.get("invalid_fields"):
                    summary += " 需核对字段 "+ "、".join(out["invalid_fields"][:6])+"。"
            elif out.get("failure_code") in {"missing_original", "original_mismatch", "storage_error"}:
                missing_source |= out.get("failure_code") in {"missing_original", "original_mismatch"}
                summary += _label(out.get("message"), "请核对归档文件与备份。")
            diagnostics.append({"tool_call_id":call.id, "summary":summary+row_problems})
    pending = remaining or "原目标尚无已验证的模型最终答复，其余工作有待核对。"
    if remaining is None and progress and all(item["kind"] == "discovery" for item in progress):
        pending = "目前只有方法或目录的查阅记录，尚无本轮科学计算结果，也没有已验证的模型最终答复。"
    action_kind = {"authentication":"review_settings", "model_rejected":"review_settings", "model_response":"review_settings",
                   "owner_unverified":"check_owner", "cancelled":"continue_after_fix","owner_released":"inspect_record",
                   "interrupted":"inspect_record","execution_limit":"inspect_record", "needs_input":"supply_information",
                   "tool_input":"inspect_record","partial_result":"inspect_record","unsupported":"inspect_record"}.get(cause.category,"external_action")
    next_steps = [{"kind":action_kind,"actor":cause.actor,"action":cause.action}, *actions]
    if validation_issue and cause.category != "owner_unverified":
        next_steps.append({"kind":"inspect_record","actor":"Agent","action":"下一轮先读取异常记录，按工具声明的字段、类型和单位更正调用参数。"})
    if missing_source and cause.category != "owner_unverified":
        next_steps.append({"kind":"supply_information","actor":"资料提供者","action":"在实验资料中核对原件编号和指纹，选择与归档指纹相符的备份恢复原件。"})
    if cause.category != "owner_unverified" and action_kind not in {"continue_after_fix", "supply_information"}:
        next_steps.append({"kind":"continue_after_fix", "actor":"实验者",
                           "action":"处理上述问题并核对保留记录后，点击「继续任务」载入原目标与依据，再确认发送。"})
    return FailureHandoff(category=cause.category, reason=cause.reason, basis=cause.basis, progress=progress, progress_summary=summary_progress(progress),
                          diagnostics=diagnostics, remaining=pending,
                          next_steps=next_steps).model_dump()

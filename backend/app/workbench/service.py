"""Persisted iterative tool loop. Decisions belong to the model, execution to tools."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from time import perf_counter

from pydantic import ValidationError
from sqlalchemy import case as sql_case

from ..attachment_files import file_reference
from ..database import SessionLocal
from ..models import AgentChatMessage, AgentChatSession, AgentStep, AgentTask, AgentToolCall, Attachment, ExperimentCase, GeneratedContent
from ..observability.usage import apply_usage_to_generated, attach_usage_payload
from ..providers import get_ai_provider
from ..providers.base import ModelTurnError
from .contracts import Finish, InterruptedResult
from .handoff import CANCELLED, LIMIT, RELEASED, UNKNOWN_OWNER, UNKNOWN_STOP, Cause, build_handoff, exception_cause
from .prompt import INSTRUCTIONS
from .editable_results import MODEL_WRITE_TOOLS
from .studies import StudyFailure
from .tools import REGISTRY, STARTING_TOOLS, compact, execute, schemas
from .policy_records import policy_selection
from .projects import current_project_input, read_project_inputs
from .study_inputs import basis
from .context import attachment_page, selected_context

logger = logging.getLogger(__name__)
PROCESS_STARTED = datetime.now(timezone.utc).timestamp()
ACTIVE_RUNS: set[int] = set()
MAX_ROUNDS = 16
MAX_CALLS = 32


def interruption_reason(exc):
    """Credential-free wording shared with older clients reading the plain summary."""
    cause = exception_cause(exc)
    return cause.reason + cause.action


def study_view_for_call(name, output, call_id):
    """The saved result and its exact workbench navigation are visible to both clients."""
    target = {}
    policy = None
    if name == "inspect_adjustment_policy" and output.get("designs"):
        policy = output
    elif name == "save_adjustment_evaluation":
        policy = output.get("adjustment_evaluation")
    if policy:
        comparison = output.get("id") or policy["search_id"]
        target = {"run_id": output["baseline_id"], "comparison_id": comparison, "selection": policy_selection(policy),
                  "label": f"查看比较 #{comparison} 的调节策略"}
    elif name == "inspect_study_tolerance" and output.get("nominal"):
        target = {"run_id": output["baseline_id"], "comparison_id": output["search_id"],
                  "selection": {key: output[key] for key in ("row_index", "axis_index", "coordinate_index", "scope")},
                  "label": f"查看比较 #{output['search_id']} 的装调余量"}
    elif (output.get("id") and output.get("baseline_id") and isinstance(output.get("rows"), list)
          and isinstance(output.get("recipe"), dict) and output["recipe"].get("axes")):
        target = {"run_id": output["baseline_id"], "comparison_id": output["id"],
                  "label": f"查看候选比较 #{output['id']}"}
    elif (output.get("study_record") or {}).get("id"):
        run_id = output["study_record"]["id"]
        target = {"run_id": run_id, "label": f"查看计算记录 #{run_id} 与输入来源"}
    if target:
        return {**target, "tool_call_id": call_id,
                "saved_in": "本轮计算结果中单独保存的工具记录。引用此 tool_call_id 即可从工作台打开对应计算或恢复该比较视图。"}
    return None


def interrupted_result(task, cause):
    handoff = build_handoff(task, cause)
    calls = [item["tool_call_id"] for item in handoff["progress"]]
    problems = [item["tool_call_id"] for item in handoff["diagnostics"]]
    terminal = "cancelled" if task.status == "cancelled" else "failed"
    count = f"本轮已保留 {len(calls)} 条可引用的工具记录。" if calls else "本轮尚无可引用的工具结果。"
    progress = handoff["progress_summary"]
    summary = " ".join(filter(None, [cause.reason, cause.basis, count, progress, handoff["remaining"], cause.action]))
    return InterruptedResult(status="unverified" if cause.category == "owner_unverified" else "partial" if calls else terminal,
                             summary=summary, next_steps=[step["action"] for step in handoff["next_steps"]],
                             evidence_tool_call_ids=calls, diagnostic_tool_call_ids=problems,
                             failure_handoff=handoff).model_dump()


def model_handoff(task, result):
    """Attach host-verified records to an unfinished model answer; leave the model's answer intact."""
    status = result.get("status")
    if status not in {"partial", "needs_input", "unsupported"}:
        return result
    question = (result.get("questions") or [None])[0]
    diagnostics = result.get("diagnostic_tool_call_ids") or []
    if question:
        cause = Cause("needs_input", "模型答复中列出了需要补充的信息。", "以下条件来自本轮保存的问题与说明。",
                      "实验者", question["how_to_obtain"])
        remaining = question["question"]
    elif diagnostics:
        cause = Cause("tool_input", "本轮有尚待核对的工具记录。", "这些调用返回失败或要求补充输入，模型在答复中引用了对应编号。",
                      "实验者", "打开异常记录，核对输入、来源或调用参数，根据记录补齐条件后再继续。")
        remaining = "原目标仍有未验证的部分。请结合模型答复和所引用工具的返回记录核查。"
    else:
        category = "unsupported" if status == "unsupported" else "partial_result"
        cause = Cause(category, "模型报告当前方法或资料仅能支持部分工作。" if status == "partial" else "模型报告当前方法无法完成原目标。",
                      "本轮保存了一份模型答复，完成范围以其引用的实际工具结果为依据。", "实验者",
                      "查看已保存的依据与模型说明，明确尚待补充的条件或可用方法。")
        remaining = "模型尚未确认原目标全部完成。"
    handoff = build_handoff(task, cause, remaining=remaining, diagnostic_ids=set(diagnostics))
    return {**result, "failure_handoff":handoff}


def recorded_finish(task, *, matching=None):
    """Validate a saved finish against this task's actual tool evidence."""
    recorded = {c.id:c.status for c in task.tool_calls if c.tool_name not in {"workbench_request", "finish"}
                and c.output_json}
    for call in sorted(task.tool_calls, key=lambda c:c.id, reverse=True):
        if call.tool_name == "finish" and call.status == "completed":
            try:
                report = validate_finish(call.input_json, recorded)
            except (ValueError, TypeError):
                continue
            result = report.model_dump()
            if matching is None or result == {k:v for k,v in matching.items() if k != "failure_handoff"}:
                return result
    return None


def result_from_trace(task, *, unverified=False):
    """Read an interrupted task without creating an artifact or writing a chat answer."""
    report = recorded_finish(task) if task.status != "cancelled" else None
    if report is not None:
        return model_handoff(task, report), "recorded_model_finish"
    if unverified:
        cause = UNKNOWN_OWNER
    elif task.status == "cancelled":
        cause = CANCELLED
    else:
        released = any((c.output_json or {}).get("failure_code") == "owner_released" for c in task.tool_calls
                       if isinstance(c.output_json, dict) and c.tool_name == "workbench_request")
        cause = RELEASED if released else UNKNOWN_STOP
    return interrupted_result(task, cause), "system_receipt"


def validate_finish(raw, recorded):
    """Conservatively downgrade contradictory completion, without another LLM call."""
    normalized = dict(raw)
    normalized.pop("purpose", None)
    problems = {key for key, state in recorded.items() if state in {"failed", "needs_input"}}
    all_blocked = bool(recorded) and len(problems) == len(recorded)
    if all_blocked and not normalized.get("diagnostic_tool_call_ids"):
        normalized["diagnostic_tool_call_ids"] = sorted(problems)
    if normalized.get("status") == "completed":
        if normalized.get("questions"):
            normalized["status"] = "partial" if any(s in {"completed","partial"} for s in recorded.values()) else "needs_input"
        elif any(recorded.get(i) == "partial" for i in normalized.get("evidence_tool_call_ids", [])):
            normalized["status"] = "partial"
        elif all_blocked:
            normalized["status"] = "partial"
    result = Finish.model_validate(normalized)
    invalid = [i for i in result.evidence_tool_call_ids if recorded.get(i) not in {"completed","partial"}]
    if invalid:
        raise ValueError(f"Evidence ids must refer to successful recorded calls in this run; invalid: {invalid}. Use diagnostic_tool_call_ids to cite a failed read or calculation.")
    if set(result.diagnostic_tool_call_ids) - problems:
        raise ValueError("Diagnostic ids must refer to a failed or needs-input tool call in this run.")
    return result


def recover_received_finish(task):
    """Recover a received result rejected solely for contradictory status.

    This never generates a conclusion: it validates the original model payload
    and real evidence ids, preserving the original rejection in the trace.
    """
    recorded = {c.id: c.status for c in task.tool_calls if c.tool_name not in {"finish", "workbench_request"}}
    for call in sorted(task.tool_calls, key=lambda c: c.id, reverse=True):
        if call.tool_name != "finish" or "Required unanswered questions" not in (call.error_message or ""):
            continue
        try:
            result = validate_finish(call.input_json, recorded).model_dump()
        except (ValueError, TypeError):
            continue
        call.output_json = {"status": result["status"], "message": "已恢复模型收到的完整结论；含未回答问题，状态保守降级。",
                            "original_validation_error": call.error_message}
        call.status, call.error_message = "completed", None
        return result
    return None


def session_for_case(db, case, user_id):
    params = case.parameters or {}
    sid = params.get("workbench_session_id") or params.get("design_session_id")
    session = db.get(AgentChatSession, sid) if sid else None
    if not session or session.case_id != case.id:
        session = AgentChatSession(case_id=case.id, user_id=user_id, title=f"实验任务 · {case.title}"[:255])
        db.add(session)
        db.flush()
    if params.get("workbench_session_id") != session.id:
        case.parameters = {**params, "workbench_session_id": session.id}
    return session


def context_for_case(db, case, session_id, attachment_ids=()):
    messages = db.query(AgentChatMessage).filter_by(session_id=session_id).order_by(AgentChatMessage.id.desc()).limit(25).all()
    artifacts = db.query(GeneratedContent.id, GeneratedContent.content_type, GeneratedContent.content["summary"],
                         GeneratedContent.content[("workbench","summary")]).filter(GeneratedContent.case_id == case.id).order_by(GeneratedContent.id.desc()).limit(20)
    calls = db.query(AgentToolCall.id, AgentToolCall.tool_name, AgentToolCall.status, AgentToolCall.input_json).join(
        AgentTask, AgentTask.id == AgentToolCall.task_id).filter(AgentTask.case_id == case.id,
        AgentToolCall.tool_name.notin_(["workbench_request", "finish"])).order_by(AgentToolCall.id.desc()).limit(30)
    latest_project = current_project_input(db, case.id)
    current_project = ({"revision_id":latest_project["revision_id"], "project":compact(latest_project["project"]),
                       "authority":"Current user-selected component snapshot; takes precedence over old generated plans and previous-part labels."}
                       if latest_project else None)
    page = attachment_page(db, case.id)
    chosen = db.query(Attachment).filter(Attachment.case_id == case.id, Attachment.id.in_(attachment_ids)).all() if attachment_ids else []
    selected_files = {item.id:file_reference(item) for item in chosen}
    return {"current_optical_project":current_project,
            "presentation_capabilities": {
                "registered_sections": "Incident paths join host uniform segments at their entrance. Observation planes can stay at a fixed unfolded z from the main-path origin or follow a named element at a fixed local depth. Use analyze_mode_overlap for two registered Gaussian beams: intensity similarity across wavelengths, scalar spatial projected power for equal wavelengths with known wavefronts in the same medium. Use a power or gain model for absorption or efficiency.",
                "optical_analysis": "The workspace renders physical positions, T/S beam-radius curves and normalized Gaussian cross-section intensity from returned mode radii. This is a visualization of the computed model, not a measured image or an additional pulse/gain solver.",
                "parameter_scans": "One/two-parameter scans have interactive plots and downloadable numerical records; saved analysis records retain model provenance.",
                "engineering": "Users can inspect/edit versioned engineering objects and their evidence while continuing the same Agent conversation."},
            "case": {"id": case.id, "title": case.title, "description": case.description,
                     "parameters": compact(case.parameters or {}), "measurements": compact(case.measurements or {}),
                     "symptoms": case.symptoms, "goal": case.goal},
            "conversation": [{"id": m.id, "role": m.role, "content": m.content, "metadata": compact(m.metadata_json or {})}
                             for m in reversed(messages[:24])],
            "older_messages_available": len(messages) > 24,
            "saved_tool_results": [{"id": c.id, "name": c.tool_name, "status": c.status, "input": compact(c.input_json)} for c in calls],
            "attachments": page["items"],
            "archive": {"total_files":page["total_matches"], "next_before_id":page["next_before_id"],
                        "selected_files":[selected_files[i] for i in attachment_ids if i in selected_files],
                        "directory_tool":"read_attachment"},
            "artifact_note": "Historical records may be proposals, measurements or numerical results. Read the record's inputs, source and method before using it. A saved calculation does not establish the current hardware.",
            "artifacts": [{"id":rid, "type":kind, "summary":summary or workbench_summary or ""} for rid,kind,summary,workbench_summary in artifacts],
            "module_results": [{"id": m.id, "type": m.module_type, "status": m.status, "result": compact(m.result_json)}
                               for m in case.case_modules if m.result_json],
            "inventory_note": "Use list_inventory / match_components to inspect actual available parts."}


def _save_result(db, case, task, request, result, usage, elapsed, working=None, *, origin="model"):
    if origin == "model":
        result = model_handoff(task, result)
    payload = {"workbench": result, "summary": result["summary"], "task_id": task.id,
               "model_basis":request.get("model_basis"), "result_model":working, "result_origin":origin,
               "request_references":{"attachment_ids":request.get("attachment_ids") or [], "context_refs":request.get("context_refs") or {}}}
    if request.get("agent_profile"):
        payload["agent_profile"] = request["agent_profile"]
    attach_usage_payload(payload, provider=usage.get("provider", "unknown"), model=usage.get("model", "unknown"), usage=usage)
    artifact = GeneratedContent(case_id=case.id, content_type="workbench", content=payload,
                                prompt_version="task-agent-v1", latency_ms=int(elapsed * 1000))
    apply_usage_to_generated(artifact, payload)
    db.add(artifact)
    db.flush()
    claim = db.query(AgentTask).filter_by(id=task.id, case_id=case.id, final_content_id=None)
    if origin == "model":
        claim = claim.filter(AgentTask.status.in_(["pending", "running"]))
    claimed = claim.update({
        AgentTask.final_content_id:artifact.id,
        AgentTask.status:sql_case((AgentTask.status.in_(["cancelled", "failed"]), AgentTask.status), else_=result["status"]),
    }, synchronize_session=False)
    if not claimed:
        db.rollback()
        if origin == "model" and task.status == "cancelled" and task.final_content_id is None:
            _save_result(db, case, task, request, interrupted_result(task, CANCELLED), usage, elapsed, working,
                         origin="system_receipt")
        return
    session_id = request.get("session_id")
    if db.query(AgentChatSession.id).filter_by(id=session_id, case_id=case.id).first():
        db.add(AgentChatMessage(session_id=session_id, role="assistant", content=result["summary"],
                                metadata_json={"workbench_run_id": task.id, "generated_content_id": artifact.id,
                                               "result": result, "result_origin":origin}))
    db.commit()


async def execute_run(task_id):
    db = SessionLocal()
    started = perf_counter()
    usage = {"input_tokens": 0, "output_tokens": 0}
    result = None
    working = None
    try:
        task = db.get(AgentTask, task_id)
        if not task or task.status != "pending":
            return
        request_call = db.query(AgentToolCall).filter_by(task_id=task.id, tool_name="workbench_request").one()
        request = request_call.input_json
        case = db.get(ExperimentCase, task.case_id)
        task.status = "running"
        db.commit()
        provider = get_ai_provider()
        if getattr(provider, "agent_profile", None):
            request = {**request, "agent_profile": provider.agent_profile}
            request_call.input_json = request
            db.commit()
        context = context_for_case(db, case, request["session_id"], request["attachment_ids"])
        context["selected_references"] = selected_context(db, case.id, request.get("context_refs"))
        working = request.get("model_basis")
        if working is not None:
            context["question_model"] = working
            if working["revision_id"] is not None:
                if context["selected_references"].get("project_revision_id") == working["revision_id"]:
                    context["question_model"] = {**working, "project_context_key":"selected_references"}
                elif (context.get("current_optical_project") or {}).get("revision_id") == working["revision_id"]:
                    context["question_model"] = {**working, "project_context_key":"current_optical_project"}
                else:
                    frozen = read_project_inputs(db, case.id, [working["revision_id"]])[working["revision_id"]]
                    context["question_model"] = {**working, "project":compact(frozen["project"])}
        messages = [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
        recorded, call_count = {}, 0
        has_reference_model = working and working.get("revision_id") and any(
            item["type"] in {"optical_project","optical_snapshot"} and item["id"] != working["revision_id"]
            for item in context["selected_references"].get("artifacts", [])
        )
        discovered = ["compare_project_inputs"] if has_reference_model else []
        if request.get("attachment_ids"):
            discovered.append("find_study_usage")
        selected_types = {item["type"] for item in context["selected_references"].get("artifacts", [])}
        if "study_search" in selected_types:
            discovered.extend(["reassess_study_search", "inspect_study_tolerance", "inspect_adjustment_policy"])
        if selected_types & {"beam_prediction_comparison", "beam_observation", "prediction_check"}:
            discovered.extend(["read_beam_readings", "check_prediction_reading"])
        for round_index in range(MAX_ROUNDS):
            db.refresh(task)
            if task.status == "cancelled":
                break
            step = AgentStep(task_id=task.id, step_index=round_index + 1, title=f"Agent 第 {round_index + 1} 轮", status="running",
                             rationale="根据目标、已有证据与上一轮结果选择下一步。")
            db.add(step)
            db.commit()
            if round_index == MAX_ROUNDS - 1 or call_count >= MAX_CALLS:
                available = schemas(["finish"])
                messages.append({"role": "user", "content": "本轮已到执行上限。用 finish 保存已有结果与尚未完成的工作；未完成原目标时标 partial，不能谎称完成。"})
            else:
                available = schemas([*STARTING_TOOLS, *discovered])
            turn = await provider.agent_turn(INSTRUCTIONS, messages, available)
            used = turn.get("_usage", {})
            for key in ("provider", "model"):
                if used.get(key):
                    usage[key] = used[key]
            for key in ("input_tokens", "output_tokens"):
                usage[key] += used.get(key) or 0
            db.refresh(task)
            if task.status == "cancelled":
                step.status = "cancelled"
                db.commit()
                break
            messages.append({"role": "assistant", "items": turn.get("items", [])})
            calls = turn.get("calls") or []
            if not calls:
                raise ModelTurnError("模型未返回工具调用。")
            for call in calls:
                if result:
                    break
                name = call["name"]
                trace = AgentToolCall(task_id=task.id, step_id=step.id, tool_name=name, status="running", input_json={})
                db.add(trace)
                db.flush()
                tick = perf_counter()
                pictures = []
                try:
                    raw = json.loads(call["arguments"])
                    if not isinstance(raw, dict):
                        raise ValueError("Tool arguments must be a JSON object")
                    trace.input_json = raw.copy()
                    purpose = raw.pop("purpose", "")
                    step.title = f"{name}"[:255]
                    step.rationale = (purpose.strip()[:2000] or None) if isinstance(purpose, str) else None
                    db.commit()
                    if name not in REGISTRY or (call_count >= MAX_CALLS and name != "finish"):
                        raise ValueError("Unknown or exhausted tool; use an available tool or finish with partial results")
                    args = validate_finish(raw, recorded) if name == "finish" else REGISTRY[name][0].model_validate(raw)
                    if name == "finish":
                        result = args.model_dump()
                        output = {"status": result["status"], "message": "任务结果已整理"}
                    else:
                        call_count += 1
                        output = await execute(name, args, db=db, case=case, task=task, provider=provider,
                                               session_id=request["session_id"], attachment_ids=request["attachment_ids"], working=working)
                        found = []
                        if name == "search_capabilities":
                            found = [item["name"] for item in output["items"]]
                            found.extend(output[key] for key in ("connection_action","table_inspection_action") if key in output)
                        elif name == "inspect_tool":
                            found = [output["name"]]
                        elif name == "inspect_adjustment_policy" and output.get("designs"):
                            found = ["save_adjustment_evaluation"]
                        elif name in {"compare_beam_predictions", "record_beam_reading"} and output.get("id"):
                            found = ["read_beam_readings", "check_prediction_reading"]
                        elif name in {"search_study_parameters", "reassess_study_search", "refine_study_tolerance"} and output.get("rows"):
                            found = ["reassess_study_search", "inspect_study_tolerance", "inspect_adjustment_policy"]
                        elif name == "inspect_study_tolerance" and output.get("scope") == "slice" and len(output.get("paths", [])) == 1:
                            if any(output[side]["termination"] == "outside" for side in ("lower", "upper")):
                                found = ["refine_study_tolerance"]
                        if output.get("native_grid_access") or (output.get("study_record") or {}).get("native_grid_access"):
                            found.append("inspect_native_grid")
                        if found:
                            discovered = [n for n in discovered if n not in found] + [n for n in found if n not in STARTING_TOOLS]
                            discovered = discovered[-max(20, len(found)):]
                        if name == "begin_optical_study":
                            working = output["model_basis"]
                        elif name in MODEL_WRITE_TOOLS and output.get("revision_id"):
                            working = basis(output)
                        pictures = output.pop("_images", [])
                        view = study_view_for_call(name, output, trace.id)
                        if view is not None:
                            output["workbench_view"] = view
                    # Reject non-finite solver output before JSON storage/API serialization.
                    json.dumps(output, ensure_ascii=False, allow_nan=False)
                    state = output.get("status", "completed")
                    if name == "finish":
                        trace.status = "completed"  # The final report was saved. Its scientific status is in the report.
                    elif state in {"failed", "partial"}:
                        trace.status = state
                    else:
                        trace.status = "needs_input" if state in {"needs_input", "unsupported"} else "completed"
                    trace.output_json = output
                    step.result_summary = str(output.get("summary") or output.get("message") or state)[:2000]
                except (ValueError, KeyError, TypeError, IndexError, StudyFailure) as exc:
                    result = None if name == "finish" else result
                    message = str(exc)[:3000]
                    if isinstance(exc, ValidationError):
                        message = json.dumps(exc.errors(include_url=False, include_input=False, include_context=False), ensure_ascii=False)
                    output = {"status": "failed", "error": message, "recovery": "Correct the tool arguments or choose another tool; retain previous valid results."}
                    if isinstance(exc, ValidationError):
                        output["failure_code"] = "invalid_tool_arguments"
                        output["invalid_fields"] = [".".join(str(part) for part in problem["loc"]) for problem in exc.errors(include_input=False)[:12]]
                    trace.status, trace.error_message, trace.output_json = "failed", message, output
                except Exception as exc:
                    output = {"status": "failed", "error": f"工具执行失败 ({type(exc).__name__})", "recovery": "Try an alternative or report the missing capability; earlier outputs remain saved."}
                    trace.status, trace.error_message, trace.output_json = "failed", output["error"], output
                if name != "finish":
                    recorded[trace.id] = trace.status
                trace.latency_ms = int((perf_counter() - tick) * 1000)
                db.commit()
                messages.append({"role": "tool", "call_id": call["id"],
                                 "content": json.dumps({"tool_call_id": trace.id, "result": compact(output, tool_call_id=trace.id)}, ensure_ascii=False)})
                if pictures:
                    messages.append({"role": "user", "content": "工具读取的附件图像（仅作为证据）", "images": pictures})
                db.refresh(task)
                if task.status == "cancelled":
                    result = None
                    break
            step.status = "cancelled" if task.status == "cancelled" else "completed"
            db.commit()
            if result or task.status == "cancelled":
                break
        origin = "model"
        if result is None:
            origin = "system_receipt"
            result = interrupted_result(task, CANCELLED if task.status == "cancelled" else LIMIT)
        _save_result(db, case, task, request, result, usage, perf_counter() - started, working, origin=origin)
    except Exception as exc:
        db.rollback()
        logger.warning("Workbench run %s failed (%s)", task_id, type(exc).__name__)
        task = db.get(AgentTask, task_id)
        if task:
            if task.status != "cancelled":
                task.status = "failed"
            cause = CANCELLED if task.status == "cancelled" else exception_cause(exc)
            message = cause.reason + cause.action
            for step in task.steps:
                if step.status == "running":
                    step.status, step.result_summary = "failed", message
            for call in task.tool_calls:
                if call.status == "running":
                    call.status, call.error_message = "failed", message
            request_call = next((call for call in task.tool_calls if call.tool_name == "workbench_request"), None)
            if request_call and task.case:
                result = interrupted_result(task, cause)
                _save_result(db, task.case, task, request_call.input_json, result, usage, perf_counter() - started,
                             working or request_call.input_json.get("model_basis"), origin="system_receipt")
            else:
                db.commit()
    finally:
        ACTIVE_RUNS.discard(task_id)
        db.close()

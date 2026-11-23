"""Persisted design runs: model intake -> deterministic simulation -> inventory -> revision."""
from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from datetime import datetime, timezone
from time import perf_counter

from sqlalchemy.orm import selectinload

from ..attachment_files import read_file
from ..database import SessionLocal
from ..inventory.crystals import CrystalSpec
from ..models import AgentChatMessage, AgentChatSession, AgentStep, AgentTask, AgentToolCall, Attachment, ExperimentCase, GeneratedContent, InventoryItem
from ..providers import get_ai_provider
from ..observability.usage import apply_usage_to_generated
from .brief import FIELDS, merge_facts
from .projection import accepted_updates, coupled_inputs, record_projection
from .solver import solve_design

logger = logging.getLogger(__name__)
PROCESS_STARTED = datetime.now(timezone.utc).timestamp()
# Local background runner: IDs accepted by this process, including queued tasks.
ACTIVE_RUNS: set[int] = set()


def specialized_projection(db, case, *, revision_id=None):
    """Refuse a lossy conversion of the actual project into one scalar crystal.

    This is shared by the legacy design entry and the Agent's optional planner;
    general project analyses remain available and no hardware is changed.
    """
    from ..workbench.projects import read_project
    from ..workbench.assemblies import object_keys
    params = case.parameters or {}
    saved = read_project(db, case.id, revision_id)
    project = saved["project"] if saved else {}
    system = project.get("system") or params.get("current_optical_system")
    facts = params.get("design_facts") or {}
    if not system:
        return facts, None, None
    elements = system.get("elements",[])
    reasons = []
    if system.get("mode") != "standing_wave":
        reasons.append("当前工程是环形腔或单程传播，不能投影成端泵两镜直线腔。")
    if (len(elements) < 3 or elements[0].get("kind") != "mirror" or elements[-1].get("kind") != "mirror"
            or sum(e.get("kind") == "mirror" for e in elements) != 2):
        reasons.append("当前工程的反射面/折叠光路超出两个端镜的专用拓扑。")
    if any(e.get("kind") != "space" for e in elements[1:-1]):
        reasons.append("当前工程含额外透镜、GRIN或自定义矩阵等内部光学作用，不能在标量规划中省略。")
    if any(e.get("kind") == "mirror" and e.get("aoi_deg",0) != 0 for e in elements):
        reasons.append("当前镜面存在非零入射角；专用标量腔不能替代完整T/S光学模型。")
    keys = object_keys(project) if project else []
    if len({keys[p["element_index"]] for p in project.get("parts", []) if p["role"] == "crystal"}) > 1:
        reasons.append("当前工程包含多块分离晶体；单块晶体的长度、折射率与增益区标量不能代表整条光路。")
    version = saved["revision_id"] if saved else params.get("current_optical_project_revision_id")
    if not reasons:
        try:
            return coupled_inputs(project, version, facts), None, version
        except ValueError as exc:
            reasons.append(str(exc))
    guidance = ("请用 read_optical_project 读取该版本，用 analyze_observation_planes 逐轴计算真实主路、入射支路和观察面。"
                "主腔可用 analyze_optical_system / scan_optical_system / scan_optical_system_2d 复算或扫描。")
    return facts, {"schema":"optical_design_v2","status":"unsupported", "facts":deepcopy(facts),
            "summary":"".join(reasons)+guidance, "issues":[{"level":"blocking","text":reason} for reason in reasons],
            "questions":[],"assumptions":[],"candidates":[],"resolved_inputs":{},"component_bindings":[],"sources":[],
            "project_revision_id":version,
            "engine":{"name":"专用模型适配检查","implementation":"当前工程拓扑与实物组校验",
                      "scope":"未运行两镜单晶体规划；完整模型仍可由通用光学内核计算。"},
            "power_prediction":{"output_w":None,"reason":"当前完整工程没有被简化或做输出功率预测。"},
            "next_steps":[guidance]}, version


def specialized_projection_guard(db, case, facts=None, *, revision_id=None):
    _, blocked, _ = specialized_projection(db, case, revision_id=revision_id)
    if blocked and facts is not None:
        blocked["facts"] = deepcopy(facts)
    return blocked


def inventory_snapshot(db, case=None):
    rows = db.query(InventoryItem).options(selectinload(InventoryItem.coatings), selectinload(InventoryItem.loans)).all()
    result = [{"id": item.id, "name": item.name, "category": item.category, "roc_mm": item.roc_mm,
             "roc_is_flat": item.roc_is_flat, "diameter_mm": item.diameter_mm,
             "focal_length_mm": item.focal_length_mm, "condition": item.effective_condition,
             "thickness_mm": item.thickness_mm, "dimensions": item.dimensions, "material": item.material,
             "crystal_spec": item.crystal_spec, "parse_notes": item.parse_notes or [],
             "cut_axis": item.cut_axis, "doping_pct": item.doping_pct,
             "available_quantity": max(0, (item.quantity or 0) - sum(loan.quantity for loan in item.loans if loan.returned_at is None)),
             "location": item.location, "raw_spec": item.raw_spec, "parse_confidence": item.parse_confidence,
             "coatings": [{key: getattr(band, key) for key in ["surface", "wl_min_nm", "wl_max_nm", "function", "value_type", "comparator", "value_pct"]} for band in item.coatings]}
            for item in rows]
    if case is not None:
        selected = (case.parameters or {}).get("current_crystal") or {}
        if selected.get("inventory_id") and isinstance(selected.get("spec"), dict):
            for item in result:
                if item["id"] == selected["inventory_id"]:
                    # The inventory retains label order; model planning uses the
                    # explicitly selected physical orientation from this case.
                    item["crystal_spec"] = selected["spec"]
                    item["selected_orientation"] = selected.get("orientation")
                    # Stock/loan availability is live. Optical input belongs to the
                    # mounted revision even when a later catalog edit identifies a section.
                    spec = CrystalSpec.model_validate(selected["spec"])
                    thickness = spec.overall_length_mm if spec.overall_length_mm is not None else spec.total_length_mm
                    item.update(thickness_mm=thickness, dimensions=spec.dimension_summary(thickness),
                                material=spec.material_label or spec.material_summary, doping_pct=spec.gain_doping)
                    context = (case.parameters or {}).get("current_component_context") or {}
                    source = (context.get("components") or {}).get("crystal") or {}
                    if source.get("inventory_id") == item["id"]:
                        for field in ("name", "raw_spec", "cut_axis", "coatings"):
                            if field in source:
                                item[field] = deepcopy(source[field])
    return result


def _progress(db, task, number, title, detail=""):
    if db.query(AgentTask.status).filter_by(id=task.id).scalar() == "cancelled":
        raise RuntimeError("本轮设计已由用户结束。")
    for step in task.steps:
        if step.status == "running":
            step.status = "completed"
    step = AgentStep(task_id=task.id, step_index=number, title=title, status="running", rationale=detail)
    db.add(step)
    db.commit()
    db.refresh(task)


async def read_design_files(db, case_id, attachment_ids, *, labels_only=False):
    """Read exactly what the extractor sees. Leave full measurement processing to the measurement tools."""
    images, readings = [], []
    for aid in dict.fromkeys(attachment_ids):
        item = db.get(Attachment, aid)
        if item is None or item.case_id != case_id:
            raise ValueError("附件已移除或不属于当前工程，请重新选择。")
        page = await asyncio.to_thread(read_file, item, 0, 4000, raw_image=True)
        pictures = page.pop("_images", [])
        if labels_only:
            if page["status"] != "completed":
                raise RuntimeError(page["message"])
            if not pictures or pictures[0]["display_copy"] or pictures[0]["mime"] not in {"image/png", "image/jpeg", "image/webp"}:
                raise RuntimeError("设计标签识别支持 PNG、JPEG、WebP 图片。")
        if pictures:
            image = pictures[0]
            images.append(image)
            page.update(source=f"image:{aid}", mime=image["mime"], display_copy=image["display_copy"])
        elif page.get("text"):
            page.update(source=f"file:{aid}", offset=0)
            if page.get("table_candidate"):
                page["measurement_tool"] = "inspect_measurement_table"
        readings.append(page)
    return images, readings


async def execute_design(task_id: int):
    db = SessionLocal()
    started = perf_counter()
    try:
        task = db.get(AgentTask, task_id)
        if not task or task.status != "pending":
            return
        task.status = "running"
        db.commit()
        request = db.query(AgentToolCall).filter_by(task_id=task.id, tool_name="design_request").one().input_json
        case = db.get(ExperimentCase, task.case_id)
        previous, projection, model_revision = specialized_projection(db, case)
        await asyncio.sleep(0)
        _progress(db, task, 1, "读取需求与晶体标签", "保留原话、单位、图片出处；不把模型猜测记为事实。")
        images, readings = await read_design_files(db, case.id, request["attachment_ids"], labels_only=True)
        inventory = inventory_snapshot(db, case)
        constraints = request.get("constraints", {})
        context = {"title": case.title if not previous else "", "message": task.goal,
                   "description": case.description or "" if not previous else "",
                   "existing_facts": previous, "explicit_constraints": constraints,
                   "inventory": inventory[:150], "selected_originals": readings}
        # Include every explicitly bound item even in a large catalogue.
        bound_ids = {int(value) for key, value in {**{k: f["value"] for k, f in previous.items()}, **constraints}.items() if key.endswith("_inventory_id") and value is not None}
        context["inventory"] += [item for item in inventory[150:] if item["id"] in bound_ids]
        evidence = {key: context[key] for key in ["title", "message", "description"]}
        for item in inventory:
            if item["id"] in bound_ids:
                evidence[f"inventory:{item['id']}"] = item.get("raw_spec") or item["name"]
        extraction = {"facts":[]} if projection else await get_ai_provider().extract_design_facts(context, images)
        if not isinstance(extraction.get("facts"), list):
            raise RuntimeError("模型未返回可核对的需求字段，未覆盖原设计，请重试。")
        facts, changes, notes = merge_facts(previous, extraction, evidence, [image["id"] for image in images], readings)
        for key, value in constraints.items():
            old = facts.get(key, {}).get("value")
            if old != value:
                changes.append({"key": key, "before": old, "after": value, "quote": "用户在约束面板修改"})
            if value is None:
                facts.pop(key, None)
            else:
                facts[key] = {"value": value, "quote": "用户在约束面板指定", "source": "constraint_panel",
                              "label": FIELDS[key][0], "unit": FIELDS[key][1], "verification": "user_constraint"}
        # UI and audit use the accepted subset; raw bytes/credentials never persist here.
        db.add(AgentToolCall(task_id=task.id, tool_name="extract_design_facts", status="completed",
                             input_json={"attachment_ids": request["attachment_ids"]},
                             output_json={"facts": facts, "changes": changes, "notes": notes}))
        policy = request.get("assumption_policy", "exploratory")
        _progress(db, task, 2, "计算光路", "按本次取值方式评估可计算的物理量。")
        design = projection
        if design:
            design["facts"] = facts
            design["assumption_policy"] = policy
        else:
            design = await asyncio.to_thread(solve_design, facts, inventory, assumption_policy=policy)
            record_projection(design, previous, model_revision)
        design["changes"] = changes
        design["intake_notes"] = notes
        design["intake_files"] = readings
        design["inventory_snapshot_at"] = datetime.now(timezone.utc).isoformat()
        design["inventory_count"] = len(inventory)
        _progress(db, task, 3, "核对库存并整理设计版本", "匹配镀膜/曲率/口径、排除损坏与借出，生成采购规格与缺失参数清单。")
        summary = design.get("summary") or "已记录需求。补齐下方关键参数后，我会继续进行数值设计。"
        if design["issues"]:
            summary += "\n" + "\n".join(item["text"] for item in design["issues"])
        if design["questions"]:
            summary += "\n可进一步收敛的参数：" + "；".join(item["question"] for item in design["questions"][:4])
        content = GeneratedContent(case_id=case.id, content_type="plan", model=extraction.get("model"),
                                   prompt_version="engineering_design_v2", latency_ms=int((perf_counter() - started) * 1000),
                                   content={"title": f"{case.title} · 光路设计", "engineering_design": design, "summary": summary})
        db.add(content)
        apply_usage_to_generated(content, extraction)
        db.flush()
        db.refresh(case)
        evidence_facts = accepted_updates((case.parameters or {}).get("design_facts", {}), previous, facts)
        parameters = {**(case.parameters or {}), "design_facts": evidence_facts}
        # Keep downstream reports and the existing physics tools grounded in accepted facts.
        for key, parameter in [("pump_wavelength_nm", "pump_nm"), ("laser_wavelength_nm", "wavelength_nm"), ("gain_material", "gain_medium")]:
            if key in evidence_facts:
                parameters[parameter] = evidence_facts[key]["value"]
        case.parameters = parameters
        if case.goal == "待通过设计对话明确" and facts.get("laser_wavelength_nm"):
            wave = facts["laser_wavelength_nm"]["value"]
            medium = facts.get("gain_material", {}).get("value", "待确认材料")
            case.goal = f"设计 {medium} 激光器，目标波长 {wave:g} nm；详见设计工作区。"
        if facts.get("cavity_type", {}).get("value") in {"linear", "ring", "bow-tie", "custom"}:
            case.cavity_type = facts["cavity_type"]["value"]
        if not db.query(AgentTask).filter_by(id=task.id, status="running").update(
                {AgentTask.status:"completed", AgentTask.final_content_id:content.id}, synchronize_session=False):
            db.rollback()
            return
        for step in task.steps:
            step.status = "completed"
        db.add(AgentToolCall(task_id=task.id, tool_name="optical_design_solver", status="completed",
                             input_json={"fact_count": len(facts), "inventory_count": len(inventory), "assumption_policy": policy},
                             output_json={"search": design.get("search"), "status": design["status"], "content_id": content.id}))
        db.add(AgentChatMessage(session_id=request["session_id"], role="assistant", content=summary,
                                metadata_json={"design_run_id": task.id, "generated_content_id": content.id}))
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("Design run %s failed", task_id)
        task = db.get(AgentTask, task_id)
        if task and task.status != "cancelled":
            task.status = "failed"
            # Provider exceptions can include request bodies. Keep them out of UI.
            message = str(exc) if isinstance(exc, RuntimeError) else f"设计计算失败（{type(exc).__name__}），原设计已保留。检查输入或模型连接后重试。"
            for step in task.steps:
                if step.status == "running":
                    step.status = "failed"
                    step.result_summary = message[:1000]
            db.commit()
    finally:
        ACTIVE_RUNS.discard(task_id)
        db.close()


def design_session(db, case, user_id=None):
    sid = (case.parameters or {}).get("design_session_id")
    session = db.get(AgentChatSession, sid) if sid else None
    if session is None or session.case_id != case.id:
        session = AgentChatSession(case_id=case.id, user_id=user_id, title=f"设计工作区 · {case.title}"[:255])
        db.add(session)
        db.flush()
        case.parameters = {**(case.parameters or {}), "design_session_id": session.id}
    return session

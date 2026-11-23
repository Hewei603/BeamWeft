"""Continue engineering from one immutable, typed input of a scientific calculation."""
from ..models import GeneratedContent
from .network import declared_ports
from .observation import OpticalLayout
from .project_outputs import LAYOUT, FrozenProjectOutputs
from .study_inputs import SaveVariant, basis, save_variant, snapshot_request_key
from .study_replay import binding_edges


def layout_objects(port, value):
    """Project only object identities from the exact layout port the calculation will consume."""
    if (port.quantity, port.unit, port.shape, port.scale) != (LAYOUT.quantity, LAYOUT.unit, LAYOUT.shape, LAYOUT.scale):
        raise ValueError("所选数据没有声明工程光路接口。")
    if not isinstance(value, dict) or not isinstance(value.get("system"), dict) or not isinstance(value["system"].get("elements"), list):
        raise ValueError("所选光路缺少元件序列，无法读取对象目录。")

    def entries(container, key):
        rows = container.get(key, []) if isinstance(container, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("uid"), str) for row in rows):
            raise ValueError("所选光路的对象标识格式不完整。")
        return rows

    def identity(row):
        return {"uid": row["uid"], "name": row.get("name") or row["uid"]}

    components = []
    for row in entries(value.get("network") or {}, "components"):
        model = row.get("model") or {}
        if not isinstance(model, dict):
            raise ValueError("所选光路的端口声明格式不完整。")
        components.append({**identity(row), "model": {"ports": declared_ports(model.get("kind"), model.get("ports"))}})
    return {"incident_paths": [identity(row) for row in entries(value, "incident_paths")],
            "observation_planes": [identity(row) for row in entries(value, "observation_planes")],
            "network": {"components": components}}


def study_layout_inputs(run_id, record):
    definition = record.get("definition")
    ports = definition.get("input_ports") if isinstance(definition, dict) else None
    values = record.get("inputs")
    if not isinstance(ports, dict) or not isinstance(values, dict):
        return []
    refs = record.get("connections")
    result = []
    for field, port in ports.items():
        if not isinstance(port, dict) or (port.get("quantity"), port.get("shape"), port.get("unit"), port.get("scale")) != (
                LAYOUT.quantity, LAYOUT.shape, LAYOUT.unit, LAYOUT.scale) or not isinstance(values.get(field), dict):
            continue
        item = {"input_key": field, "label": port.get("label") or field}
        if refs is None:
            result.append({**item, "kind": "frozen", "history_unknown": True})
            continue
        if not isinstance(refs, dict):
            continue
        if field not in refs:
            result.append({**item, "kind": "frozen"})
            continue
        ref = refs[field]
        try:
            binding_edges(run_id, {field: ref})
        except ValueError:
            continue
        if "revision_id" in ref:
            origin = (record.get("project_evidence") or {}).get(field) or {}
            result.append({**item, "kind": "project", "revision_id": ref["revision_id"], "port": ref["port"],
                           "scope": origin.get("scope"), "location": origin.get("location")})
        elif "run_id" in ref:
            result.append({**item, "kind": "frozen", "upstream_run_id": ref["run_id"], "port": ref["port"]})
    return result


def begin_study_input(db, case_id, run_id, input_key, variant_name=None):
    row = db.query(GeneratedContent).filter_by(case_id=case_id, id=run_id, content_type="scientific_study").one_or_none()
    if row is None:
        raise ValueError("当前工程中找不到所选计算。")
    choice = next((item for item in study_layout_inputs(run_id, row.content) if item["input_key"] == input_key), None)
    if choice is None:
        raise ValueError("所选输入没有有效的冻结光路及其接口声明。请查看计算记录中的输入来源。")
    origin = {"run_id": run_id, "method_title": row.content["definition"].get("title"), **choice}
    label = variant_name or f"计算 #{run_id} · {input_key}"[:160]
    if choice["kind"] == "project":
        # A referenced plane or arm is a view of the source project. Keep its other paths and locks.
        rid = choice["revision_id"]
        try:
            source = FrozenProjectOutputs(db, case_id, rid)
        except ValueError as exc:
            raise ValueError(f"当前工程中找不到计算引用的光路版本 #{rid}。请核对工程文件中的历史版本。") from exc
        published = source.get(choice["port"])
        if not published or published["port"]["quantity"] != LAYOUT.quantity:
            raise ValueError(f"来源工程 #{rid} 缺少计算时使用的光路接口。已存计算仍可查看。")
        evidence = (row.content.get("project_evidence") or {}).get(input_key) or {}
        identities = ("revision", "scope", "path_uid", "plane_uid", "element_uid")
        if any(evidence[key] != published.get(key) for key in identities if key in evidence):
            raise ValueError(f"来源工程 #{rid} 或其取值位置与原计算记录不一致。请核对输入来源。")
        frozen = OpticalLayout.model_validate(row.content["inputs"][input_key])
        if OpticalLayout.model_validate(published["value"]) != frozen:
            raise ValueError(f"来源工程 #{rid} 的光路与计算保存的输入不一致。请查看原计算的输入和来源。")
        model = source.row
        if model.get("model_kind") == "draft" and not variant_name:
            return {"status": "completed", "model_basis": basis(model), "project": model["project"], "study_input": origin}
        base = model.get("base_revision_id") if model.get("model_kind") == "draft" else rid
        draft = SaveVariant(project=model["project"], source_revision_id=rid, base_revision_id=base, variant_name=label)
        note = f"从计算 #{run_id} 的输入 {input_key} 打开来源工程 #{rid} 的独立方案"
    else:
        layout = OpticalLayout.model_validate(row.content["inputs"][input_key])
        note = f"从计算 #{run_id} 的输入 {input_key} 复制冻结光路"
        draft = SaveVariant(project={"title": label, **layout.model_dump(mode="json")}, variant_name=label)
    # Opening the same saved input selects its existing fork across installations. New solver
    # receipts belong to explicit recalculations. Anonymous elements may receive IDs on first save.
    key = snapshot_request_key("study-layout:", [run_id, input_key, label])
    saved = save_variant(db, case_id, draft, operation_key=key, note=note, study_input=origin)
    return {"status": "completed", "model_basis": basis(saved), "project": saved["project"], "study_input": origin}

"""Compare archived power-meter readings with a specific saved optical path."""

import hashlib
import json
import math
import statistics
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import AgentTask, GeneratedContent
from .measurement_sources import TableRange, case_original, read_numeric_cells
from .authoring import EditableLayout
from .observation import host_positions, host_route, locate_planes
from .optics import StrictInput
from .path_power import partial_stage_transmission
from .projects import read_project

Unit = Literal["W", "mW", "uW", "nW", "dBm"]
METHOD_VERSION = 1


class MeterTable(TableRange):
    kind: Literal["table"]
    decimal_mark: Literal[".", ","] = "."
    signal_column: int = Field(ge=1, le=16384)
    reference_column: int | None = Field(
        default=None,
        ge=1,
        le=16384,
        description="Simultaneous power at the selected upstream reference, calibrated to the entire beam.",
    )

    @model_validator(mode="after")
    def different_channels(self):
        if self.signal_column == self.reference_column:
            raise ValueError("测点功率和上游参考不能指向同一列。")
        return self


class MeterNote(StrictInput):
    kind: Literal["transcribed"]
    attachment_id: int = Field(gt=0)
    locator: str = Field(
        min_length=1,
        max_length=500,
        pattern=r"\S",
        description="Original page/region, timestamp, or record identifier.",
    )
    signal: float
    reference: float | None = Field(
        default=None, description="Simultaneous full-beam upstream power if the original contains it."
    )


class MeasureAt(StrictInput):
    kind: Literal["before", "after", "plane"]
    uid: str = Field(min_length=1, max_length=100)
    scope: Literal["arm", "host"] | None = None


class MeterCorrection(StrictInput):
    dark_w: float = Field(
        default=0, description="Signed meter offset in watts, subtracted after converting the recorded unit to watts."
    )
    scale: float = Field(
        default=1,
        gt=0,
        description="Multiplier converting the offset-corrected meter reading into full beam power at this position.",
    )
    source: str = Field(
        min_length=1, max_length=500, pattern=r"\S", description="Calibration record, or an explicit model assumption."
    )


class MeasurePower(StrictInput):
    revision_id: int = Field(gt=0)
    path_uid: str = Field(min_length=1, max_length=100)
    target: MeasureAt
    reference_target: MeasureAt | None = Field(
        default=None, description="Simultaneously measured upstream point on the same path. Omit for the path source."
    )
    source: MeterTable | MeterNote = Field(discriminator="kind")
    signal_unit: Unit
    reference_unit: Unit | None = None
    signal_correction: MeterCorrection | None = None
    reference_correction: MeterCorrection | None = None
    signal_bound_w: float | None = Field(
        default=None,
        ge=0,
        description="Optional declared absolute error bound in watts, including calibration, for each measured signal.",
    )
    reference_bound_w: float | None = Field(
        default=None, ge=0, description="Optional declared absolute error bound in watts for each reference reading."
    )
    wavelength_nm: float | None = Field(default=None, gt=0, le=1e6)
    conditions: str = Field(min_length=1, max_length=800, pattern=r"\S")
    comparable_conditions: bool = Field(
        default=False,
        description="Acquisition corresponds to this model's beam, plane and operating condition, in calibrated full-beam power, using matching time windows for paired channels.",
    )
    independence: Literal["unspecified", "held_out", "model_input"] = "unspecified"

    @model_validator(mode="after")
    def valid_reference(self):
        has_reference = (
            self.source.reference_column is not None
            if isinstance(self.source, MeterTable)
            else self.source.reference is not None
        )
        if has_reference != (self.reference_unit is not None):
            raise ValueError("上游参考读数和参考单位需成对填写。")
        if not has_reference and (self.reference_bound_w is not None or self.reference_correction is not None):
            raise ValueError("缺少上游参考读数，无法应用其误差界限。")
        if not has_reference and self.reference_target is not None:
            raise ValueError("选择上游参考位置时，还须提供同步参考读数。")
        return self


class MeterHistory(StrictInput):
    revision_id: int = Field(gt=0)
    measurement_id: int | None = Field(default=None, gt=0)


def meter_targets(revision):
    """Resolve the same physical boundaries as the ledger, even before a budget exists."""
    project = revision["project"]
    layout = EditableLayout.model_validate({k: v for k, v in project.items() if k in EditableLayout.model_fields})
    anchors = host_positions(layout.system)
    planes = [p for p, _ in locate_planes(layout, anchors).values() if p is not None]
    budgets = {b["path_uid"]: b for b in revision["analysis"].get("sections", {}).get("power_budgets", [])}
    paths = []
    if layout.system.mode == "single_pass":
        paths.append(
            ("primary", "主光束", layout.system.wavelength_nm, [], [(el, z, None) for _, el, z in anchors.values()])
        )
    for path in layout.incident_paths:
        route, _ = host_route(path, anchors, layout.system.wavelength_nm)
        paths.append((path.uid, path.name, path.wavelength_nm, path.elements, route))
    result = []
    for uid, name, wavelength, arm, route in paths:
        ledger = budgets.get(uid, {})
        rows = {(r["scope"], r["uid"]): r for r in ledger.get("rows", [])}
        sections = {s["plane_uid"]: s for s in ledger.get("planes", [])}
        z = route[0][1] - sum(el.length_mm for el in arm) if route else None
        elements = []
        for el in arm:
            elements.append(("arm", el, z))
            if z is not None:
                z += el.length_mm
        elements.extend(("host", el, start) for el, start, _ in route)
        targets = []
        upstream_missing = []
        for index, (scope, el, start) in enumerate(elements):
            if not el.uid:
                continue
            row = rows.get((scope, el.uid), {})
            for kind, metric, label in (("before", "incoming", "之前"), ("after", "outgoing", "之后")):
                pos = start + el.length_mm if kind == "after" and start is not None else start
                missing = (
                    upstream_missing
                    if kind == "before"
                    else upstream_missing + [el.name + " · " + note for note in row.get("missing", [])]
                )
                targets.append(
                    {
                        "target": {"kind": kind, "uid": el.uid, "scope": scope},
                        "label": el.name + label,
                        "z_mm": pos,
                        "model": row.get(metric),
                        "missing": missing,
                        "order": [index, 0, 0] if kind == "before" else [index, 2, el.length_mm],
                    }
                )
            if scope == "host" and el.kind == "space":
                for plane in planes:
                    if plane.target_uid == el.uid and plane.depth_mm <= el.length_mm:
                        local_notes = [
                            el.name + " · " + note
                            for note in row.get("missing", [])
                            if note != "出面保留比例" and not (plane.depth_mm == 0 and note.startswith("体吸收系数"))
                        ]
                        targets.append(
                            {
                                "target": {"kind": "plane", "uid": plane.uid},
                                "label": plane.name + "（介质内侧）",
                                "z_mm": start + plane.depth_mm,
                                "model": sections.get(plane.uid, {}).get("forward"),
                                "missing": upstream_missing + local_notes,
                                "order": [index, 1, plane.depth_mm],
                            }
                        )
            upstream_missing = upstream_missing + [el.name + " · " + note for note in row.get("missing", [])]
        result.append(
            {
                "uid": uid,
                "name": name,
                "wavelength_nm": wavelength,
                "targets": targets,
                "model_status": ledger.get("status"),
                "missing": ledger.get("missing", []),
            }
        )
    return result


def _transmission_between(ledger, start, end):
    if not ledger or not ledger.get("rows"):
        return None
    lo, hi = 1.0, 1.0
    first, last = start[0], end[0]
    for i in range(first, last + 1):
        row = ledger["rows"][i]
        if (i == first and start[1] == 2) or (i == last and end[1] == 0):
            continue
        left = start[2] if i == first else 0.0
        right = end[2] if i == last else row["length_mm"]
        a, b = partial_stage_transmission(row, right - left, i != first or start[1] == 0, i != last or end[1] == 2)
        lo, hi = lo * a, hi * b
    return [lo, hi]


def _finite(value):
    if not math.isfinite(value):
        raise ValueError("读数或换算结果超出数值范围，请核对单位和上游参考。")
    return value


def _watts(value, unit):
    if unit == "dBm":
        try:
            watts = 10.0 ** ((value - 30.0) / 10.0)
        except OverflowError as exc:
            raise ValueError("dBm 读数超出可换算的范围。") from exc
        if watts == 0:
            raise ValueError("dBm 读数低于可表示范围，请核对单位。")
        return _finite(watts)
    return _finite(value * {"W": 1.0, "mW": 0.001, "uW": 1e-6, "nW": 1e-9}[unit])


def _observations(db, case_id, source):
    if isinstance(source, MeterTable):
        columns = [source.signal_column]
        if source.reference_column is not None:
            columns.append(source.reference_column)
        cells, evidence = read_numeric_cells(db, case_id, source, columns, source.decimal_mark)
        return [(row, values[0], values[1] if len(values) > 1 else None) for row, values in cells], evidence
    with case_original(db, case_id, source.attachment_id) as (_, evidence):
        return [(None, source.signal, source.reference)], evidence


def _compare(value, model, bound):
    if model is None or value is None:
        return None
    difference = [_finite(value - model[1]), _finite(value - model[0])]

    def apart(a, b):
        return a < b and not math.isclose(a, b, rel_tol=1e-12)

    side = "below" if apart(value, model[0]) else "above" if apart(model[1], value) else "inside"
    disjoint = None if bound is None else apart(bound[1], model[0]) or apart(model[1], bound[0])
    return {"difference": difference, "side": side, "disjoint_with_bound": disjoint}


def _bounds(value, bound):
    return None if bound is None else [_finite(value - bound), _finite(value + bound)]


def _sample(args, item, raw_model):
    row, raw, reference = item

    def corrected(watts, correction):
        return _finite((watts - correction.dark_w) * correction.scale) if correction else watts

    meter_w = _watts(raw, args.signal_unit)
    value = corrected(meter_w, args.signal_correction)
    power_range = _bounds(value, args.signal_bound_w)
    ref_meter_w = None if reference is None else _watts(reference, args.reference_unit)
    ref = None if ref_meter_w is None else corrected(ref_meter_w, args.reference_correction)
    ratio = _finite(value / ref) if ref is not None and ref > 0 else None
    fraction_range = None
    if ratio is not None and power_range is not None and args.reference_bound_w is not None:
        ref_range = _bounds(ref, args.reference_bound_w)
        if ref_range[0] > 0:
            endpoints = [_finite(v / r) for v in power_range for r in ref_range]
            fraction_range = [min(endpoints), max(endpoints)]
    transmission = raw_model.get("fraction")
    conditional = [_finite(ref * t) for t in transmission] if ratio is not None and transmission is not None else None
    return {
        "row": row,
        "raw_signal": raw,
        "raw_reference": reference,
        "meter_w": meter_w,
        "reference_meter_w": ref_meter_w,
        "watts": value,
        "reference_w": ref,
        "fraction": ratio,
        "reading_bounds_w": power_range,
        "reading_bounds_fraction": fraction_range,
        "prediction_at_reference_w": conditional,
        "absolute": _compare(value, raw_model.get("watts"), power_range),
        "normalized": _compare(ratio, raw_model.get("fraction"), fraction_range),
    }


def _overview(points, key, metric):
    values = [p[key] for p in points if p[key] is not None]
    comparisons = [p[metric] for p in points if p[metric] is not None]
    if not values:
        return None
    return {
        "count": len(values),
        "mean": statistics.mean(values),
        "min": min(values),
        "max": max(values),
        "compared": len(comparisons),
        "below": sum(c["side"] == "below" for c in comparisons),
        "above": sum(c["side"] == "above" for c in comparisons),
        "bounded": sum(c["disjoint_with_bound"] is not None for c in comparisons),
        "disjoint": sum(c["disjoint_with_bound"] is True for c in comparisons),
    }


def measure_power(db, case_id, args, *, task_id=None, origin="manual"):
    revision = read_project(db, case_id, args.revision_id)
    path = next((p for p in meter_targets(revision) if p["uid"] == args.path_uid), None)
    target = (
        next((t for t in path["targets"] if t["target"] == args.target.model_dump(exclude_none=True)), None)
        if path
        else None
    )
    if target is None:
        raise ValueError("指定的测点不在此版本的单程光路上，请重新选择光束和位置。")
    reference_label = "源面"
    fraction = (target["model"] or {}).get("fraction")
    if args.reference_target is not None:
        address = args.reference_target.model_dump(exclude_none=True)
        ref = next((t for t in path["targets"] if t["target"] == address), None)
        if ref is None or ref["order"] >= target["order"]:
            raise ValueError("参考测点须在同一光路中，且位于测点上游。")
        ledger = next(
            (
                b
                for b in revision["analysis"].get("sections", {}).get("power_budgets", [])
                if b["path_uid"] == path["uid"]
            ),
            None,
        )
        fraction = _transmission_between(ledger, ref["order"], target["order"])
        reference_label = ref["label"]
    values, evidence = _observations(db, case_id, args.source)
    recipe = args.model_dump()
    digest = hashlib.sha256(
        json.dumps([METHOD_VERSION, recipe, evidence], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    key = "meter:" + digest

    def existing():
        return db.query(GeneratedContent).filter_by(case_id=case_id, operation_key=key).one_or_none()

    prior = existing()
    if prior:
        return {"id": prior.id, **prior.content}

    reasons = []
    if not args.comparable_conditions:
        reasons.append("请核对测点、波长响应、收光和采样工况，确认此读数代表模型中的整束前向功率。")
    if (
        args.wavelength_nm is None
        or path["wavelength_nm"] is None
        or not math.isclose(args.wavelength_nm, path["wavelength_nm"], rel_tol=1e-9)
    ):
        reasons.append("实测波长与模型波长未对齐。")
    if target["model"] is None:
        reasons.append("此版本在该测点没有可对照的功率预算。")
    model = {} if reasons else {**target["model"], "fraction": fraction}
    points = [_sample(args, item, model) for item in values]
    absolute = _overview(points, "watts", "absolute")
    normalized = _overview(points, "fraction", "normalized")
    if model and model["watts"] is None:
        reasons.append("工程中缺少源面输入瓦数。提供同步上游参考可直接对照传输比例。")
    if any(p["reference_w"] is not None and p["reference_w"] <= 0 for p in points):
        reasons.append("部分上游参考为零或负值，相应行仅保留功率读数。")
    if (
        normalized
        and args.signal_bound_w is not None
        and args.reference_bound_w is not None
        and any(p["fraction"] is not None and p["reading_bounds_fraction"] is None for p in points)
    ):
        reasons.append("上游参考的误差界限包含零，相应行无法限定传输比例误差。")
    reasons.extend(target["missing"])
    compared = absolute["compared"] + (normalized["compared"] if normalized else 0)
    content = {
        "status": "completed" if compared and not reasons else "partial",
        "method_version": METHOD_VERSION,
        "recipe": recipe,
        "origin": origin,
        "source_file": evidence,
        "path_name": path["name"],
        "revision_id": args.revision_id,
        "model_wavelength_nm": path["wavelength_nm"],
        "location": target,
        "reference_label": reference_label,
        "transmission_model": fraction,
        "points": points,
        "absolute": absolute,
        "normalized": normalized,
        "notes": list(dict.fromkeys(reasons)),
    }
    if task_id is not None:
        status = db.query(AgentTask.status).filter_by(case_id=case_id, id=task_id).with_for_update().scalar()
        if status != "running":
            raise ValueError("任务已停止，本次读数未写入。")
    record = GeneratedContent(case_id=case_id, content_type="power_measurement", operation_key=key, content=content)
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        prior = existing()
        if prior:
            return {"id": prior.id, **prior.content}
        raise
    return {"id": record.id, **content}


def agent_measure(bind, case_id, args, task_id):
    with Session(bind) as db:
        return measure_power(db, case_id, args, task_id=task_id, origin="agent")


def meter_history(db, case_id, revision_id, measurement_id=None):
    revision = read_project(db, case_id, revision_id)
    rows = (
        db.query(GeneratedContent)
        .filter_by(case_id=case_id, content_type="power_measurement")
        .filter(GeneratedContent.content["revision_id"].as_integer() == revision_id)
    )
    if measurement_id is not None:
        item = rows.filter_by(id=measurement_id).one_or_none()
        if not item:
            raise ValueError("此案例及版本中没有该功率测量记录。")
        return {"id": item.id, **item.content}
    data = GeneratedContent.content
    query = rows.with_entities(
        GeneratedContent.id,
        data["recipe"]["conditions"].as_string(),
        data["path_name"].as_string(),
        data["location"]["label"].as_string(),
        data["status"].as_string(),
    )
    paths = meter_targets(revision)
    for path in paths:
        for target in path["targets"]:
            target.pop("missing", None)
    return {
        "paths": paths,
        "items": [
            dict(zip(("id", "conditions", "path", "location", "status"), row))
            for row in query.order_by(GeneratedContent.id.desc())
        ],
    }

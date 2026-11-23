"""Resolve scientific inputs against a frozen selection of original instrument records."""
import csv
import math
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Literal
from xml.etree.ElementTree import ParseError

from openpyxl.utils.exceptions import InvalidFileException
from pydantic import Field

from ..study_api import StrictInput
from .measurement_sources import case_original, numeric_cell, text_cell
from .measurement_view import InspectMeasurement, window_rows


class SelectedTable(InspectMeasurement):
    delimiter: Literal["comma", "tab", "semicolon", "whitespace", "single"]
    decimal_mark: Literal[".", ","]
    first_row: int = Field(ge=1, le=100_000_000)
    x_column: int | None
    y_column: int | None = Field(default=None, ge=1, le=16384)


class Calibration(StrictInput):
    gain: float
    offset: float = 0
    note: str = Field(min_length=1, max_length=500, pattern=r"\S")


class FileColumnReference(StrictInput):
    table: SelectedTable
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    column: int = Field(ge=1, le=16384)
    unit: str | None = Field(default=None, min_length=1, description="Required original unit for a physical series. Omit for a declared sequence of text labels.")
    omit_incomplete_rows: bool = False
    calibration: Calibration | None = Field(default=None, description="Values in the source unit become gain * raw + offset before unit conversion.")
    registration: str = Field(default="", max_length=500, description="When combining measurements from different sources, explain how rows correspond.")
    condition_values: dict[str, float | str] = Field(default_factory=dict, description="Documented conditions of this measurement, keyed and expressed as declared by the input port.")
    condition_note: str = Field(default="", max_length=500, description="Location or source of the documented conditions.")


def table_group(ref):
    window = ref.table.model_dump(exclude={"y_column"})
    if window["x_lower"] is None and window["x_upper"] is None:
        window.pop("x_column")
    return ref.sha256, tuple(sorted(window.items()))


def _cell(row, column, decimal):
    try:
        return numeric_cell(row[column - 1], decimal)
    except (IndexError, TypeError, ValueError, OverflowError):
        return None


def _read_group(db, case_id, study, fields):
    first = next(iter(fields.values()))
    selection = first.table
    if len({r.omit_incomplete_rows for r in fields.values()}) != 1:
        raise ValueError("同一选区的各列须使用一致的缺失行处理方式，才能保持测点配对。")
    numeric_columns = {ref.column for key, ref in fields.items() if key not in study.text_column_inputs}
    if selection.x_column and (selection.x_lower is not None or selection.x_upper is not None):
        numeric_columns.add(selection.x_column)
    maximum_column = max(numeric_columns | {ref.column for ref in fields.values()})
    numeric_columns = sorted(numeric_columns)
    text_limits = {key: study.text_column_inputs[key]["max_length"] for key in fields if key in study.text_column_inputs}
    values = {key: [] for key in fields}
    factors = {}
    schema = study.inputs.model_json_schema()["properties"]
    limits = []
    for key, ref in fields.items():
        if key in study.text_column_inputs:
            if ref.unit is not None or ref.calibration is not None or ref.condition_values or ref.condition_note:
                raise ValueError(f"{key} 对应原件文本，请移除物理单位、校准和测量工况。")
            if (limit := study.text_column_inputs[key]["max_items"]) is not None:
                limits.append(limit)
            continue
        port = study.input_ports[key]
        units = {**port.alternate_units, port.unit: port.scale}
        if port.shape != "series" or ref.unit not in units:
            raise ValueError(f"{key} 需要数列。请按列的实际单位选择 {'、'.join(units)}，并核对物理含义。")
        factors[key] = units[ref.unit] / port.scale
        if "maxItems" in schema[key]:
            limits.append(schema[key]["maxItems"])
    row_ids, omitted = [], []
    max_points = min(limits) if limits else None
    outside = 0
    with case_original(db, case_id, selection.attachment_id) as (stream, source):
        if source["sha256"] != first.sha256:
            raise ValueError("选区的校验值与当前原件不一致，请重新打开原件核对。")
        extension = Path(source["path"]).suffix.lower()
        text_columns = {fields[key].column for key in text_limits}
        with closing(window_rows(stream, selection, extension, maximum_column, text_columns=text_columns)) as rows:
            for row_id, row, _, out_of_range in rows:
                if out_of_range:
                    outside += 1
                    continue
                numbers = {c: _cell(row, c, selection.decimal_mark) for c in numeric_columns}
                invalid = next((c for c, v in numbers.items() if v is None), None)
                reason = "缺少有限数值"
                labels = {key: text_cell(row, fields[key].column, row_id, limit) for key, limit in text_limits.items()}
                if invalid is None:
                    invalid = next((fields[key].column for key, value in labels.items() if value is None), None)
                    reason = "缺少非空文本"
                if invalid is not None:
                    if not first.omit_incomplete_rows:
                        raise ValueError(f"原件第 {row_id} 条、第 {invalid} 列{reason}。请核对选区，或选择整行排除并保留行号。")
                    omitted.append(row_id)
                    continue
                if max_points is not None and len(row_ids) >= max_points:
                    raise ValueError(f"本计算输入最多容纳 {max_points} 个测点，请缩小选区。")
                row_ids.append(row_id)
                for key, ref in fields.items():
                    if key in labels:
                        values[key].append(labels[key])
                        continue
                    v = numbers[ref.column]
                    if ref.calibration:
                        v = v * ref.calibration.gain + ref.calibration.offset
                    converted = v * factors[key]
                    if not math.isfinite(converted):
                        raise ValueError(f"{key} 在原件第 {row_id} 条校准或换算后超出有限数值范围。")
                    values[key].append(converted)
    if not row_ids:
        raise ValueError("选区内没有各列同时有效的测点。")
    evidence = {"source": source, "selection": selection.model_dump(), "fields": list(sorted(fields)),
                "row_numbers": row_ids, "omitted_rows": omitted, "outside": outside}
    return values, evidence


def resolve_table_inputs(db, case_id, study, fields):
    groups = {}
    for key, ref in fields.items():
        groups.setdefault(table_group(ref), {})[key] = ref
    values, evidence = {}, []
    for members in groups.values():
        try:
            resolved, source = _read_group(db, case_id, study, members)
        except (UnicodeError, csv.Error, InvalidFileException, zipfile.BadZipFile, ParseError, KeyError) as exc:
            raise ValueError("无法按保存的列和格式读取原件，请核对编码或工作表。") from exc
        values.update(resolved)
        evidence.append(source)
    return values, evidence

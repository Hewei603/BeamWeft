"""Read an instrument table in acquisition order. Display reduction never feeds the measurements."""

import csv
import io
import re
import zipfile
from collections import Counter
from contextlib import closing
from itertools import islice
from pathlib import Path
from typing import Literal
from xml.etree.ElementTree import ParseError

from openpyxl.utils.exceptions import InvalidFileException
from pydantic import Field, model_validator
from sqlalchemy.orm import Session

from ..attachment_files import WORKBOOK_EXTENSIONS, text_sample
from .measurement_sources import case_original, numeric_cell, open_table_book, table_rows
from .optics import StrictInput

SEPARATORS = {"comma": ",", "tab": "\t", "semicolon": ";", "single": "\x1f"}
ROW_LIMIT = 1_000_000


class InspectMeasurement(StrictInput):
    attachment_id: int = Field(gt=0)
    sheet: str = Field(default="", max_length=160)
    delimiter: Literal["auto", "comma", "tab", "semicolon", "whitespace", "single"] = "auto"
    decimal_mark: Literal["auto", ".", ","] = "auto"
    first_row: int | None = Field(default=None, ge=1, le=100_000_000)
    last_row: int | None = Field(default=None, ge=1, le=100_000_000)
    x_lower: float | None = Field(default=None, description="Inclusive lower bound in the original x column's units.")
    x_upper: float | None = Field(
        default=None, description="Inclusive upper bound, applied to every record in acquisition order."
    )
    x_column: int | Literal["auto"] | None = Field(
        default="auto", description="1-based column, null for source record number."
    )
    y_column: int | None = Field(
        default=None, ge=1, le=16384, description="1-based column. Omit for a suggested numerical column."
    )

    @model_validator(mode="after")
    def check_selection(self):
        if isinstance(self.x_column, int) and not 1 <= self.x_column <= 16384:
            raise ValueError("横轴列号须在 1 至 16384 之间。")
        if self.last_row and self.first_row and self.last_row < self.first_row:
            raise ValueError("末行不能早于首行。")
        if self.x_lower is not None and self.x_upper is not None and self.x_upper < self.x_lower:
            raise ValueError("横轴上限不能小于下限。")
        return self


def _number(raw, decimal):
    try:
        return numeric_cell(raw, decimal)
    except (ValueError, TypeError, OverflowError):
        return None


def window_rows(stream, selection, extension, max_column, *, text_columns=()):
    """Read original rows in acquisition order. Bounds apply to the unscaled horizontal axis."""
    first = selection.first_row
    count = None if selection.last_row is None else selection.last_row - first + 1
    scanned = 0
    with closing(table_rows(stream, selection, extension if extension in WORKBOOK_EXTENSIONS else ".txt", max_column,
                            text_columns=text_columns)) as rows:
        for scanned, row in enumerate(islice(rows, count), 1):
            if scanned > ROW_LIMIT:
                raise ValueError("本次读取超过 100 万条记录，请指定较小的起止区间。")
            number = first + scanned - 1
            x = selection.x_column
            xv = number if x is None else _number(row[x-1], selection.decimal_mark) if x <= len(row) else None
            outside = xv is not None and ((selection.x_lower is not None and xv < selection.x_lower)
                                           or (selection.x_upper is not None and xv > selection.x_upper))
            yield number, row, xv, outside
    if count is not None and scanned != count:
        raise ValueError("选定的末行超过原件记录数。")


def _profile(rows, decimal):
    masks = [tuple(i for i, cell in enumerate(row[:64], 1) if _number(cell, decimal) is not None) for row in rows]
    common = Counter(mask for mask in masks if mask)
    if not common:
        return (0, 0, 0), [], None, {}, 0
    # Prefer repeated multichannel records over isolated numeric metadata.
    mask = max(common, key=lambda m: (common[m] * min(3, len(m)), len(m)))
    first = masks.index(mask)
    while first and set(mask).intersection(masks[first - 1]):
        first -= 1
    row_before = rows[first - 1] if first else []
    labelled_width = max((i for i, cell in enumerate(row_before, 1) if cell not in (None, "")), default=0)
    labels = {}
    for col, raw in enumerate(row_before[:64], 1):
        if raw is not None and str(raw).strip() and _number(raw, decimal) is None and labelled_width >= max(mask):
            labels[col] = str(raw).strip()[:120]
    observed = rows[masks.index(mask)]
    occupancy = len(mask) / sum(cell not in (None, "") for cell in observed)
    return (common[mask] * min(3, len(mask)), occupancy, len(mask)), list(mask), first + 1, labels, sum(map(len, masks))


def _decimal_profile(rows, decimals):
    # For the same separators, an intermittent missing value should not reward a
    # decimal convention that treats every entry of a numerical channel as text.
    candidates = ((_profile(rows, decimal), decimal) for decimal in decimals)
    profile, decimal = max(candidates, key=lambda entry: entry[0][-1])
    return profile[:4], decimal


def _layout(stream, ext, request):
    sheets, sheet = [], ""
    decimals = [".", ","] if request.decimal_mark == "auto" else [request.decimal_mark]
    ambiguous = False
    if ext in WORKBOOK_EXTENSIONS:
        with open_table_book(stream) as book:
            sheets = [ws.title for ws in book.worksheets]
            sheet = request.sheet or next(iter(sheets), "")
            if sheet not in sheets:
                raise ValueError("找不到工作表，可选 " + "、".join(sheets[:30]))
            ws = book[sheet]
            ws.reset_dimensions()
            rows = list(islice(ws.iter_rows(max_col=64, values_only=True), 80))
        (_, columns, first, labels), decimal = _decimal_profile(rows, decimals)
        delimiter = "comma"
    else:
        sample = text_sample(stream)
        if sample is None:
            raise ValueError("无法读取数值表。请提供 CSV、TSV、带字节序标记的 UTF-16 文本或 Excel 工作簿。")
        candidates = [*SEPARATORS, "whitespace"] if request.delimiter == "auto" else [request.delimiter]
        if ext == ".tsv" and "tab" in candidates:
            candidates = ["tab", *(k for k in candidates if k != "tab")]
        profiles = []
        for key in candidates:
            records = (
                (line.split() for line in io.StringIO(sample))
                if key == "whitespace"
                else csv.reader(io.StringIO(sample, newline=""), delimiter=SEPARATORS[key])
            )
            rows = list(islice(records, 80))
            profile, decimal = _decimal_profile(rows, decimals)
            profiles.append((profile, key, decimal))
        (_, columns, first, labels), delimiter, decimal = max(profiles, key=lambda pair: pair[0][0])
        if (
            request.delimiter == "auto"
            and request.decimal_mark == "auto"
            and delimiter == "comma"
            and len(columns) == 2
            and len(labels) < 2
        ):
            snippet = [s.strip() for s in sample.splitlines()[(first or 1) - 1 :][:20] if s.strip()]
            ambiguous = bool(snippet) and all(re.fullmatch(r"[+-]?\d+,\d+", s) for s in snippet)
    stream.seek(0)
    return sheets, sheet, delimiter, decimal, columns, first, labels, ambiguous


def _representatives(points):
    # Each bucket retains both ends of both axes and the first/last sample, all with original row IDs.
    return sorted(
        {
            points[0],
            points[-1],
            min(points, key=lambda p: p[1]),
            max(points, key=lambda p: p[1]),
            min(points, key=lambda p: p[2]),
            max(points, key=lambda p: p[2]),
        }
    )


class DisplayPoints:
    """Adaptive row buckets bound memory even when many consecutive records are missing."""

    def __init__(self):
        self.buckets = {}
        self.stride = 1
        self.full = []

    def add(self, point):
        if self.full is not None:
            if len(self.full) < 1536:
                self.full.append(point)
            else:
                self.full = None
        key = point[0] // self.stride
        bucket = self.buckets.setdefault(key, [])
        bucket.append(point)
        if len(bucket) > 12:
            self.buckets[key] = _representatives(bucket)
        while len(self.buckets) > 256:
            grouped = {}
            for old, points in self.buckets.items():
                grouped.setdefault(old // 2, []).extend(points)
            self.buckets = {k: _representatives(v) for k, v in grouped.items()}
            self.stride *= 2

    def values(self):
        if self.full is not None:
            return self.full
        return [p for points in self.buckets.values() for p in _representatives(points)]


def _empty_selection_message(selection, scanned, outside, missing_x):
    if not scanned:
        return f"从原件第 {selection.first_row} 行开始没有读到记录。请核对数据首行和工作表。"

    parts = []
    if outside:
        low, high = selection.x_lower, selection.x_upper
        if low is None:
            interval = f"横轴 ≤ {high}"
        elif high is None:
            interval = f"横轴 ≥ {low}"
        else:
            interval = f"{low} ≤ 横轴 ≤ {high}"
        if outside == scanned:
            return (f"扫描的 {scanned} 行全部在所选横轴窗口（{interval}）之外。"
                    "请按原件横轴单位修改或清除窗口上下限，再读取。")
        parts.append(f"扫描 {scanned} 行，其中 {outside} 行在所选横轴窗口（{interval}）之外。")
    if missing_x:
        parts.append(f"{missing_x} 行未能读取数值横坐标，请核对横轴列和原始单元格。")
    bad_y = scanned - outside - missing_x
    if bad_y:
        parts.append(f"{bad_y} 行有可用横坐标，但纵轴单元格为空或无法解析为数值，请核对纵轴列及数值格式。")
    if outside:
        parts.append("也可修改或清除横轴窗口，重新读取其他行。")
    return "".join(parts)


def _inspect(db, case_id, args):
    with case_original(db, case_id, args.attachment_id) as (stream, source):
        ext = Path(source["path"]).suffix.lower()
        sheets, sheet, delim, decimal, columns, suggested_first, labels, ambiguous = _layout(stream, ext, args)
        y = args.y_column or (columns[min(1, len(columns) - 1)] if columns else None)
        x = args.x_column
        if x == "auto":
            x = columns[0] if len(columns) >= 2 and columns[0] != y else None
        first = args.first_row or suggested_first or 1
        selection = args.model_copy(
            update={
                "first_row": first,
                "x_column": x,
                "y_column": y,
                "delimiter": delim,
                "decimal_mark": decimal,
                "sheet": sheet,
            }
        )
        data = {
            "source": source,
            "selection": selection.model_dump(),
            "sheets": sheets,
            "labels": labels,
            "columns": columns,
            "scan_note": "首行与列的建议取自开头最多 80 条记录、64 列。列名沿用原件，单位以仪器记录为准。",
        }
        if ambiguous:
            return {
                **data,
                "status": "needs_input",
                "message": "逗号可能分隔两列整数，也可能是一列读数的小数标记。请核对原件，选择分隔方式与小数标记。单列数据可用原件记录号作横轴。",
            }
        if args.last_row and first > args.last_row:
            raise ValueError("建议的数据首行晚于所选末行，请调整数据范围。")
        if y is None:
            with closing(window_rows(stream, selection, ext, x or 1)) as rows:
                scanned = sum(1 for _ in rows)
            return {**data, "status": "needs_input", "scanned": scanned,
                    "last_scanned": first + scanned - 1 if scanned else None,
                    "message": ("选区未识别出可绘图的数值列。可在研究中引用原始文本列，也可指定数值列号与数据首行。"
                                if scanned else _empty_selection_message(selection, 0, 0, 0))}
        scanned = valid = invalid = outside = missing_x = 0
        invalid_examples = []
        display = DisplayPoints()
        low = high = None
        x_min = x_max = None
        with closing(window_rows(stream, selection, ext, max(x or 1, y))) as rows:
            for row_no, row, xv, out_of_range in rows:
                scanned += 1
                if out_of_range:
                    outside += 1
                    continue
                yv = _number(row[y - 1], decimal) if y <= len(row) else None
                if xv is None or yv is None:
                    invalid += 1
                    missing_x += int(xv is None)
                    if len(invalid_examples) < 8:
                        invalid_examples.append(row_no)
                    continue
                valid += 1
                point = (row_no, xv, yv)
                display.add(point)
                if low is None or yv < low[2]:
                    low = point
                if high is None or yv > high[2]:
                    high = point
                x_min = xv if x_min is None else min(xv, x_min)
                x_max = xv if x_max is None else max(xv, x_max)
        points = display.values()
        return {
            **data,
            "status": "completed" if valid else "needs_input",
            "scanned": scanned,
            "valid": valid,
            "skipped": invalid,
            "outside": outside,
            "missing_x": missing_x,
            "skipped_examples": invalid_examples,
            "last_scanned": first + scanned - 1 if scanned else None,
            "points": points,
            "sampled": len(points) < valid,
            "minimum": low,
            "maximum": high,
            "x_range": [x_min, x_max],
            "message": "" if valid else _empty_selection_message(selection, scanned, outside, missing_x),
        }


def inspect_measurement(db, case_id, args):
    try:
        return _inspect(db, case_id, args)
    except (UnicodeError, csv.Error, InvalidFileException, zipfile.BadZipFile, ParseError, KeyError) as exc:
        raise ValueError("无法按所选格式读取表格。请核对原件编码和工作表，或使用仪器导出的文本。") from exc


def agent_inspect_measurement(bind, case_id, args):
    with Session(bind) as db:
        return inspect_measurement(db, case_id, args)

"""Read numeric cells from case originals, preserving file identity and row numbers."""

import csv
import io
import math
import zipfile
from contextlib import ExitStack, closing, contextmanager
from itertools import islice
from pathlib import Path
from typing import Literal
from xml.etree.ElementTree import ParseError

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from pydantic import Field, model_validator

from ..attachment_files import WORKBOOK_EXTENSIONS, image_mime, tabular_text, text_encoding, text_sample, verified_original
from ..models import Attachment
from .optics import StrictInput


class TableRange(StrictInput):
    attachment_id: int = Field(gt=0)
    sheet: str = Field(default="", max_length=160)
    delimiter: Literal["comma", "tab", "semicolon", "whitespace", "single"] = "comma"
    first_row: int = Field(ge=1, le=1000000)
    last_row: int | None = Field(default=None, ge=1, le=1000000)

    @model_validator(mode="after")
    def ordered_rows(self):
        if self.last_row is not None and self.last_row < self.first_row:
            raise ValueError("末行不能早于首行。")
        return self


@contextmanager
def _open_original(db, case_id, attachment_id):
    item = db.get(Attachment, attachment_id)
    if item is None or item.case_id != case_id:
        raise ValueError("案例中找不到该原件。")
    try:
        handle = open(item.filepath, "rb")
    except OSError as exc:
        raise ValueError("无法打开归档原件，请核对文件。") from exc
    with handle:
        yield handle, item


def original_prefix(db, case_id, attachment_id):
    """Bound directory lookups independently of the size of an archived acquisition."""
    limit = 65536
    with _open_original(db, case_id, attachment_id) as (handle, item):
        try:
            data = handle.read(limit + 1)
        except OSError as exc:
            raise ValueError("无法读取原件开头，请核对归档文件。") from exc
    return data[:limit], len(data) <= limit, item.filename


def offers_table_columns(prefix, filename):
    """Bounded format hint. The table reader still verifies the file, range, types and units."""
    suffix = Path(filename).suffix.lower()
    if suffix in WORKBOOK_EXTENSIONS:
        return prefix.startswith(b"PK\x03\x04")
    stream = io.BytesIO(prefix)
    if prefix.startswith(b"%PDF-") or image_mime(stream):
        return False
    sample = text_sample(stream)
    if not sample:
        return False
    if suffix in {".csv", ".tsv"} or tabular_text(sample):
        return True
    if suffix == ".txt":
        # A plain text file can carry identifiers alone. Require repeated, structured records before offering columns.
        for delimiter in ("\t", ";", ","):
            counts = set()
            records = 0
            for row in islice(csv.reader(io.StringIO(sample), delimiter=delimiter), 12):
                if any(cell.strip() for cell in row):
                    counts.add(len(row))
                    records += 1
            if records >= 2 and len(counts) == 1 and min(counts) >= 2:
                return True
    return False


@contextmanager
def case_original_snapshot(db, case_id, attachment_id):
    item = db.get(Attachment, attachment_id)
    if item is None or item.case_id != case_id:
        raise ValueError("案例中找不到该原件。")
    with ExitStack() as scope:
        try:
            path, digest = scope.enter_context(verified_original(item))
        except OSError as exc:
            raise ValueError("无法读取并校验归档原件，请检查文件和可用空间。") from exc
        yield path, {"attachment_id": item.id, "path": item.relative_path or item.filename, "sha256": digest}


@contextmanager
def case_original(db, case_id, attachment_id):
    with case_original_snapshot(db, case_id, attachment_id) as (path, evidence), path.open("rb") as handle:
        yield handle, evidence


@contextmanager
def open_table_book(stream):
    with zipfile.ZipFile(stream) as archive:
        if sum(part.file_size for part in archive.infolist()) > 256 * 1024**2:
            raise ValueError("工作簿展开后过大，请将需要的工作表归档为 CSV。")
    stream.seek(0)
    with closing(load_workbook(stream, read_only=True, data_only=True, keep_links=False)) as book:
        yield book


def table_rows(stream, spec, extension, max_column, *, text_columns=()):
    if extension in WORKBOOK_EXTENSIONS:
        if not spec.sheet:
            raise ValueError("请指定工作表名称。")
        with open_table_book(stream) as book:
            if spec.sheet not in book.sheetnames:
                raise ValueError("找不到工作表，可选 " + "、".join(book.sheetnames[:30]))
            sheet = book[spec.sheet]
            sheet.reset_dimensions()
            raw = sheet.iter_rows(min_row=spec.first_row, max_col=max_column, values_only=not text_columns)
            if text_columns:
                for row in raw:
                    yield tuple(None if i in text_columns and cell.data_type == "e" else cell.value
                                for i, cell in enumerate(row, 1))
            else:
                yield from raw
    elif extension in {".csv", ".tsv", ".txt"} or tabular_text(text_sample(stream)):
        signature = stream.read(4)
        stream.seek(0)
        encoding = text_encoding(signature)
        with io.TextIOWrapper(stream, encoding=encoding, newline="") as decoded:
            if spec.delimiter == "whitespace":
                yield from (line.split() for line in islice(decoded, spec.first_row - 1, None))
                return
            delimiter = {"comma": ",", "semicolon": ";", "tab": "\t", "single": "\x1f"}[spec.delimiter]
            yield from islice(csv.reader(decoded, delimiter=delimiter, strict=True), spec.first_row - 1, None)
    else:
        raise ValueError("当前表格分析支持 CSV、TSV、分隔文本和 Excel 工作簿。")


def numeric_cell(raw, decimal_mark):
    if decimal_mark == "," and isinstance(raw, str):
        if "." in raw:
            raise ValueError()
        raw = raw.replace(",", ".")
    if isinstance(raw, bool) or not math.isfinite(value := float(raw)):
        raise ValueError()
    return value


def text_cell(row, column, row_id, limit):
    """Retain the original text exactly. Excel display formats cannot establish a stored identifier."""
    value = row[column - 1] if column <= len(row) else None
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"原件第 {row_id} 条、第 {column} 列不是文本单元格。"
                         "请在源表将标识存为文本，或将显示的编号导出为 CSV，以保留前导零等格式。")
    if limit is not None and len(value) > limit:
        raise ValueError(f"原件第 {row_id} 条、第 {column} 列的文本超过 {limit} 个字符。")
    return value if value.strip() else None


def read_numeric_cells(db, case_id, spec, columns, decimal_mark=".", *, identifier_column=None):
    count = None if spec.last_row is None else spec.last_row - spec.first_row + 1
    result = []
    with case_original(db, case_id, spec.attachment_id) as (stream, evidence):
        try:
            extension = Path(evidence["path"]).suffix.lower()
            with closing(table_rows(stream, spec, extension, max(*columns, identifier_column or 0),
                                    text_columns={identifier_column} if identifier_column else ())) as rows:
                for index, row in enumerate(islice(rows, count), spec.first_row):
                    if len(result) >= 3000:
                        raise ValueError("所选区间超过 3000 行，请指定本次实验的起止行。")
                    values = []
                    for column in columns:
                        try:
                            value = numeric_cell(row[column - 1], decimal_mark)
                        except (IndexError, TypeError, ValueError, OverflowError) as exc:
                            raise ValueError(f"第 {index} 行第 {column} 列的数值无效，请核对选区和单元格。") from exc
                        values.append(value)
                    if identifier_column is not None:
                        identifier = text_cell(row, identifier_column, index, 500)
                        if identifier is None:
                            raise ValueError(f"第 {index} 行第 {identifier_column} 列缺少采集标识。请核对同一次采集的分组。")
                        values.append(identifier)
                    result.append((index, values))
        except (UnicodeError, csv.Error, InvalidFileException, zipfile.BadZipFile, ParseError, KeyError) as exc:
            raise ValueError("无法读取该表格。请核对格式、编码和工作表。") from exc
    if count is not None and count != len(result):
        raise ValueError("选定的末行超过原件中的数据范围。")
    if not result:
        raise ValueError("选定范围内没有读数。")
    return result, evidence

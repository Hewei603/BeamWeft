"""Strict measurement ingestion: validate complete rows before calculating."""
import math
import re
import csv
from pathlib import Path

POWER_SCALE = {"W": 1.0, "mW": 1e-3, "uW": 1e-6, "µW": 1e-6, "μW": 1e-6, "kW": 1e3}
LENGTH_UNITS = {"m": 1, "cm": .01, "µm": 1e-6, "um": 1e-6, "μm": 1e-6, "nm": 1e-9}
_HEADER = re.compile(r"^(?:pump|p[_ ]?pump|input|wavelength|wl|lambda|λ|x|z|泵浦功率|波长)(?:$|[\s_(\[])", re.I)


def numeric_rows(filepath: str, *, max_columns: int = 2) -> list[list[float]]:
    rows = []
    width = None
    first = True
    for number, line in enumerate(Path(filepath).read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        delimiter = next((value for value in (",", "\t", ";") if value in line), None)
        parts = next(csv.reader([line], delimiter=delimiter)) if delimiter else line.split()
        if first and _HEADER.match(parts[0].strip()):
            first = False
            continue
        first = False
        if len(parts) < 2:
            raise ValueError(f"第 {number} 行不足两列；未跳过任何测量行，请修正原文件。")
        try:
            values = [float(cell.strip()) for cell in parts[:max_columns]]
        except ValueError as exc:
            raise ValueError(f"第 {number} 行包含非数值单元格；未跳过任何测量行，请修正原文件。") from exc
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f"第 {number} 行包含 NaN 或 Infinity，不能作为测量值。")
        if width is not None and len(values) != width:
            raise ValueError(f"第 {number} 行列数不一致；R/T 数据必须逐行完整。")
        width = len(values)
        rows.append(values)
    return rows


def power_scale(unit: str) -> float:
    if unit not in POWER_SCALE:
        raise ValueError(f"不支持的功率单位 {unit!r}；请选择 W、mW、μW 或 kW。")
    return POWER_SCALE[unit]


def power_in_watts(values, unit: str) -> list[float]:
    factor = power_scale(unit)
    result = [float(value) * factor for value in values]
    if not all(math.isfinite(value) for value in result):
        raise ValueError("功率数据包含 NaN 或 Infinity。")
    return result

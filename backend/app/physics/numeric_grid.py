"""Decode a single numeric NPY plane without unpickling or changing its recorded lattice."""
import io
import math

import numpy as np
from numpy.lib import format as npy


MAX_FRAME_PIXELS = 32_000_000


def npy_header(stream):
    try:
        version = npy.read_magic(stream)
        if version == (1, 0):
            return npy.read_array_header_1_0(stream)
        if version in ((2, 0), (3, 0)):
            # These versions share a four-byte header length; supported numeric dtype descriptors are ASCII.
            return npy.read_array_header_2_0(stream)
        raise ValueError(f"版本 {version} 不受支持。")
    except (EOFError, ValueError, TypeError, SyntaxError, OverflowError) as exc:
        raise ValueError("无法读取 NPY 文件头，请提供一个完整的二维数值数组。") from exc


def read_npy_plane(stream, *, allow_complex=False, require_finite=True, max_cells=MAX_FRAME_PIXELS):
    shape, fortran_order, dtype = npy_header(stream)
    if len(shape) != 2 or min(shape) < 1:
        raise ValueError("需要一个非空二维数组。请从原始数据中明确选择所需截面。")
    if dtype.kind == "c" and not allow_complex:
        raise ValueError("此输入需要实数网格。复振幅原件应交给相干场求解器，或先依据测量定义取得强度。")
    if dtype.kind not in "buifc":
        raise ValueError("光学网格须为数值数组。请将所需的物理量导出为二维 NPY。")
    count = math.prod(shape)
    if count > max_cells:
        raise ValueError(f"本次读取每个网格最多容纳 {max_cells:,} 个采样，请核对采样区域和补零倍数。")
    data_start = stream.tell()
    byte_count = count * dtype.itemsize
    if stream.seek(0, io.SEEK_END) - data_start != byte_count:
        raise ValueError("NPY 的原始字节长度与声明的行列数、类型不符。请核对是否截断或拼接了多个数组。")
    stream.seek(data_start)
    values = np.frombuffer(stream.read(byte_count), dtype=dtype).reshape(shape, order="F" if fortran_order else "C")
    if require_finite and not np.isfinite(values).all():
        raise ValueError("光学网格包含非有限数值，请核对原始采样。")
    return values

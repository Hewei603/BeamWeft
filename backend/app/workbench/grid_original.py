"""Read numerical planes in their recorded row/column order and units."""

import io
from numpy.lib import format as npy
from PIL import Image

from ..physics.detector_frames import MAX_FRAME_PIXELS, read_pixels
from ..physics.numeric_grid import read_npy_plane
from ..study_api import OriginalFile


_IMAGE_FORMATS = ("PNG", "TIFF", "BMP", "PPM")
_IMAGE_SIGNATURES = (b"\x89PNG\r\n\x1a\n", b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+", b"BM")
_SINGLE_CHANNEL = {"1", "L", "I", "F", "I;16", "I;16L", "I;16B", "I;16N"}


class GridOriginal(OriginalFile):
    """One two-dimensional NPY or single-plane grayscale instrument image."""

    @classmethod
    def recognizes(cls, prefix, *, complete):
        if prefix.startswith(npy.MAGIC_PREFIX) or prefix[:2] in (b"P2", b"P5") and prefix[2:3].isspace():
            return True
        if not prefix.startswith(_IMAGE_SIGNATURES):
            return False
        try:
            with Image.open(io.BytesIO(prefix), formats=_IMAGE_FORMATS) as image:
                return image.mode in _SINGLE_CHANNEL
        except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError):
            return False if complete else None


def read_grid(original: GridOriginal, *, allow_complex=False, max_cells=MAX_FRAME_PIXELS):
    stream = original.stream
    stream.seek(0)
    prefix = stream.read(16)
    stream.seek(0)
    if prefix.startswith(npy.MAGIC_PREFIX):
        return read_npy_plane(stream, allow_complex=allow_complex, max_cells=max_cells)
    if prefix[:2] in (b"P2", b"P5") and prefix[2:3].isspace():
        pixels, _ = read_pixels(original, 1, None, max_cells=max_cells)
        if prefix[:2] == b"P5" and any(part.strip() for part in iter(lambda: stream.read(4096), b"")):
            raise ValueError("原件首帧之后还有数据。请将选定的单个 PGM 截面或二维数组单独归档。")
        return pixels
    try:
        with Image.open(stream, formats=_IMAGE_FORMATS) as image:
            if image.mode not in _SINGLE_CHANNEL or getattr(image, "n_frames", 1) != 1:
                raise ValueError("请选择单通道、单帧的原始读数；多帧或多通道原件请单独导出所需截面。")
    except (OSError, SyntaxError, EOFError, Image.DecompressionBombError) as exc:
        raise ValueError("无法识别二维原件。可使用数值 NPY 或单帧、单通道的无损 PNG、TIFF、PGM、BMP。") from exc
    pixels, _ = read_pixels(original, 1, None, max_cells=max_cells)
    return pixels

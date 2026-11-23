"""Read detector samples on their stored pixel lattice, independently of display scaling."""
import re
import warnings

import numpy as np
from numpy.lib import format as npy
from PIL import Image, UnidentifiedImageError

from .numeric_grid import MAX_FRAME_PIXELS, read_npy_plane


def _window(size, window, max_cells):
    if size[0] * size[1] > max_cells:
        raise ValueError(f"本次读取每帧最多支持 {max_cells:,} 个像素，请核对采样区域或补零倍数。")
    left, top, width, height = window or (0, 0, *size)
    if left < 0 or top < 0 or width < 1 or height < 1 or left + width > size[0] or top + height > size[1]:
        raise ValueError(f"选区超出 {size[0]} × {size[1]} 原图。请按左列、上行、宽、高填写。")
    return left, top, width, height


def _pgm_token(stream):
    token = bytearray()
    while c := stream.read(1):
        if c == b'#':
            while (c := stream.read(1)) not in (b'\r', b'\n', b''):
                pass
        if not c or c.isspace():
            if token:
                break
        else:
            token.extend(c)
            if len(token) > 32:
                raise ValueError("PGM 头字段过长，请核对仪器导出格式。")
    return bytes(token)


def _pgm_pixels(stream, frame, window, dtype, max_cells):
    # PGM display decoders rescale arbitrary Maxval to 255 or 65535. Dark and saturation
    # values refer to encoded detector samples, including for 10/12/14-bit exports.
    for number in range(1, frame + 1):
        encoding = _pgm_token(stream)
        if encoding not in (b'P2', b'P5'):
            raise ValueError(f"原件中找不到完整的第 {number} 帧 PGM 数据。")
        fields = [_pgm_token(stream) for _ in range(3)]
        if not all(t.isdigit() for t in fields):
            raise ValueError("PGM 尺寸或灰度上限无效。")
        w, h, maximum = map(int, fields)
        if not (w > 0 and h > 0 and 0 < maximum < 65536):
            raise ValueError("PGM 尺寸或灰度上限无效。")
        if number != frame:
            if encoding == b'P2':
                raise ValueError("文本 PGM 每个原件只有一帧。")
            stream.seek(w * h * (1 if maximum < 256 else 2), 1)
    left, top, width, height = _window((w, h), window, max_cells)
    if encoding == b'P5':
        storage = np.dtype('u1' if maximum < 256 else '>u2')
        raw = stream.read(w * h * storage.itemsize)
        if len(raw) != w * h * storage.itemsize:
            raise ValueError("PGM 数据短于标注的行列数，请核对原件完整性。")
        values = np.frombuffer(raw, dtype=storage).reshape(h, w)
    else:
        # The plain variant permits arbitrary line breaks between samples and comments.
        raw = re.sub(rb'#[^\r\n]*', b' ', stream.read())
        try:
            samples = (int(s.group()) for s in re.finditer(rb'\S+', raw))
            values = np.fromiter(samples, dtype=np.int64, count=w*h).reshape(h, w)
            if next(samples, None) is not None:
                raise ValueError
        except (ValueError, OverflowError) as exc:
            raise ValueError("文本 PGM 的像元读数与标注的行列数不符。") from exc
    region = values[top:top+height, left:left+width]
    if region.min() < 0 or region.max() > maximum:
        raise ValueError("选区含有超出原件 PGM 灰度范围的读数。")
    return np.array(region, dtype=dtype), {"format":"PGM", "mode":"L" if maximum < 256 else "I;16B",
        "encoded_max":maximum, "size":(w,h), "frame":frame, "window":[left,top,width,height],
        "sample_type":"u1" if maximum < 256 else "u2"}


def read_pixels(original, frame, window, *, dtype=None, max_cells=MAX_FRAME_PIXELS):
    source = original.stream
    source.seek(0)
    prefix = source.read(26)
    source.seek(0)
    if prefix[:2] in (b'P2', b'P5') and prefix[2:3].isspace():
        return _pgm_pixels(source, frame, window, dtype, max_cells)
    if prefix.startswith(npy.MAGIC_PREFIX):
        if frame != 1:
            raise ValueError("单个二维 NPY 仅有第 1 帧。请为其他采集分别选择原件。")
        values = read_npy_plane(source, require_finite=False, max_cells=max_cells)
        size = (values.shape[1], values.shape[0])
        left, top, width, height = _window(size,window,max_cells)
        pixels = np.array(values[top:top+height, left:left+width], dtype=dtype)
        if not np.isfinite(pixels).all():
            raise ValueError("选区含非有限像素。请核对原件或调整选区。")
        return pixels, {"format":"NPY","mode":values.dtype.name, "sample_type":values.dtype.str[1:],
                        "size":size,"frame":1,"window":[left,top,width,height]}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(original.stream) as image:
                image.seek(frame - 1)
                size = image.size
                if image.mode not in {"1", "L", "I", "F", "I;16", "I;16L", "I;16B", "I;16N"}:
                    raise ValueError(f"原图通道为 {image.mode}。请提供有线性强度含义的单通道原件。")
                orientation, bits, sample_kind, white_zero = 1, None, None, False
                if image.format == 'TIFF':
                    tags = image.tag_v2
                    size = (tags[256], tags[257])
                    orientation = tags.get(274, 1)
                    bits = tags.get(258, (1,))[0]
                    sample_kind = tags.get(339, (1,))[0]
                    white_zero = tags.get(262) == 0
                    if white_zero and (bits > 8 or sample_kind != 1):
                        raise ValueError("此高位深 TIFF 使用白为零的灰度解释。请无损导出原始探测器计数并注明编码，避免显示反相改变标定和背景。")
                elif image.format == 'PNG' and prefix[25] == 0:
                    bits, sample_kind = prefix[24], 1
                left, top, width, height = _window(size, window, max_cells)
                # Pillow displays TIFF according to its orientation tag. Undo that presentation
                # transform before applying an ROI or dark frame in detector row/column coordinates.
                inverse = {2:Image.Transpose.FLIP_LEFT_RIGHT, 3:Image.Transpose.ROTATE_180,
                           4:Image.Transpose.FLIP_TOP_BOTTOM, 5:Image.Transpose.TRANSPOSE,
                           6:Image.Transpose.ROTATE_90, 7:Image.Transpose.TRANSVERSE, 8:Image.Transpose.ROTATE_270}
                if orientation not in (1, *inverse):
                    raise ValueError("TIFF 方向标记无效，无法配准原始行列。")
                stored = image.transpose(inverse[orientation]) if orientation != 1 else image
                try:
                    with stored.crop((left, top, left + width, top + height)) as region:
                        pixels = np.array(region)
                finally:
                    if stored is not image:
                        stored.close()
                if sample_kind == 1 and bits in (2, 4):
                    pixels //= 255 // (2**bits - 1)
                if white_zero:
                    pixels = (2**bits - 1) - pixels.astype(np.uint8, copy=False)
                if sample_kind == 2 and bits == 8:
                    pixels = pixels.view(np.int8)
                if sample_kind == 1 and bits == 32 and pixels.dtype == np.int32:
                    pixels = pixels.view(np.uint32)
                sample_type = pixels.dtype.str[1:]
                if dtype is not None:
                    pixels = pixels.astype(dtype, copy=False)
                description = {"format":image.format, "mode":image.mode, "sample_type":sample_type, "size":size, "frame":frame,
                               "window":[left, top, width, height]}
                if bits and sample_kind == 1:
                    description['encoded_max'] = 2**bits - 1
                if orientation != 1:
                    description['display_orientation_tag'] = orientation
                if white_zero:
                    description['response_warning'] = 'TIFF 使用白为零的显示标记，计算按编码原值进行，请核对仪器读数的物理方向。'
                if image.format == "JPEG" or "gamma" in image.info or "srgb" in image.info or "icc_profile" in image.info:
                    description["response_warning"] = "原图含有损编码或色彩响应标记，请核实像素读数对入射强度的线性。"
    except EOFError as exc:
        raise ValueError(f"原件中找不到第 {frame} 帧，帧号从 1 开始。") from exc
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("无法作为单通道原图读取。请核对文件完整性、图像编码及帧大小。") from exc
    if not np.isfinite(pixels).all():
        raise ValueError("选区含非有限像素。请核对原件或调整选区。")
    return pixels, description

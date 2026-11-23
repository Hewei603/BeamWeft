"""File identity, preview capabilities and paged case-file reads."""
import base64
import codecs
import hashlib
import io
import math
import mimetypes
import re
import tempfile
import zipfile
from contextlib import closing, contextmanager
from pathlib import Path, PureWindowsPath

from openpyxl import load_workbook
from PIL import Image

DISPLAY_COPY_EXTENSIONS = {".bmp", ".tif", ".tiff", ".pgm", ".ppm", ".pbm", ".pnm"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"} | DISPLAY_COPY_EXTENSIONS
DISPLAY_COPY_FORMATS = {"BMP", "TIFF", "PPM"}
IMAGE_FORMATS = ("PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF", "PPM")
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".tsv", ".json", ".log", ".pdf"}
WORKBOOK_EXTENSIONS = {".xlsx", ".xlsm"}


class PreviewLimitError(ValueError):
    """A valid original can exceed the interactive decoder's budget."""


class OriginalMismatch(ValueError):
    """Archived evidence no longer matches its recorded bytes."""

    def __init__(self, item, actual):
        super().__init__(f"原件 #{item.id} 的内容与归档指纹不符。请核对校验值，从备份恢复该原件，或将新测量另存为独立原件。")
        self.expected = item.content_hash.lower()
        self.actual = actual


@contextmanager
def verified_original(item):
    """Hand readers the exact bytes that were hashed, even if the archive is restored mid-read."""
    with tempfile.TemporaryDirectory(prefix="beamweft-original-") as directory:
        # The PDF and stability readers dispatch on a suffix. Never stage a submitted path.
        suffix = Path(item.filename).suffix.lower()
        snapshot = Path(directory) / ("source" + (suffix if suffix in (".pdf", ".zip") else ".bin"))
        with open(item.filepath, "rb") as source, snapshot.open("xb") as target:
            actual = transfer(source, target)
        if item.content_hash and actual != item.content_hash.lower():
            raise OriginalMismatch(item, actual)
        yield snapshot, actual


def transfer(source, target):
    digest = hashlib.sha256()
    while chunk := source.read(1024 * 1024):
        target.write(chunk)
        digest.update(chunk)
    return digest.hexdigest()


def file_mode(filename, mime=None):
    ext = Path(filename).suffix.lower()
    if ext in IMAGE_EXTENSIONS or (mime and mime.startswith("image/")):
        return "image"
    if ext in WORKBOOK_EXTENSIONS:
        return "workbook"
    if ext in TEXT_EXTENSIONS or mime == "text/plain":
        return "text"
    return "archive"


def text_encoding(signature):
    return "utf-16" if signature.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8-sig"


def text_sample(stream, size=65536):
    """Inspect a prefix without assuming vendor filename conventions or cutting a code point."""
    raw = stream.read(size)
    stream.seek(0)
    try:
        sample = codecs.getincrementaldecoder(text_encoding(raw))(errors="strict").decode(raw, final=len(raw) < size)
    except UnicodeError:
        return None
    return sample if sample and all(ch.isprintable() or ch.isspace() or ch == "\ufeff" for ch in sample) else None


def image_mime(stream):
    """Recognize instrument files with uninformative suffixes from a bounded header."""
    prefix = stream.read(65536)
    stream.seek(0)
    try:
        with Image.open(io.BytesIO(prefix), formats=IMAGE_FORMATS) as im:
            return im.get_format_mimetype()
    except (OSError, SyntaxError, ValueError, EOFError, Image.DecompressionBombError):
        return None


def tabular_text(sample):
    if not sample:
        return False
    number = re.compile(r'^[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][+-]?\d+)?$')
    matches = 0
    for line in sample.splitlines():
        explicit = any(c in line for c in ",;\t")
        fields = re.split(r"[,;\t]", line) if explicit else line.split()
        numeric = [bool(number.fullmatch(s.strip().strip('"'))) for s in fields]
        if numeric and (any(numeric) if explicit else all(numeric)):
            matches += 1
            if matches == 2:
                return True
    return False


def archive_location(filename, relative_path=None):
    """The submitted hierarchy is metadata. It never determines a storage path."""
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    path = (relative_path or name).replace("\\", "/")
    parts = path.split("/")
    if (not name or len(name) > 255 or len(path) > 2048 or parts[-1] != name
            or any(p in {"", ".", ".."} or len(p) > 255 or PureWindowsPath(p).drive
                   or any(ord(c) < 32 for c in p) for p in parts)):
        raise ValueError("文件名或相对目录无效，请选择资料目录中的文件。")
    return name, path


def file_reference(item):
    return {"id": item.id, "filename": item.filename, "relative_path": item.relative_path,
            "read_mode": file_mode(item.filename, getattr(item, "file_type", None))}


def bundle_paths(items):
    """Keep the original tree where portable; retain separate uploads of the same name."""
    files, directories, result = set(), set(), {}
    for item in items:
        try:
            _, path = archive_location(item.filename, item.relative_path)
        except ValueError:
            path = f"file-{item.id}"
        parts = []
        for part in path.split("/"):
            portable = re.sub(r'[<>:"|?*]', "_", part).rstrip(" .") or "_"
            if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", portable.split(".")[0], re.I):
                portable = "_" + portable
            parts.append(portable)
        portable_path = "/".join(parts)
        key = portable_path.casefold()
        parents = {"/".join(parts[:i]).casefold() for i in range(1, len(parts))}
        if key in files or key in directories or parents & files:
            result[item.id] = f"attachment-versions/{item.id}/{portable_path}"
        else:
            result[item.id] = f"attachments/{portable_path}"
            files.add(key)
            directories.update(parents)
    return result


def _image_bytes(path, filename):
    with Image.open(path, formats=IMAGE_FORMATS) as im:
        if im.format in DISPLAY_COPY_FORMATS:
            if im.width * im.height > 32_000_000:
                raise PreviewLimitError("原图已归档。请提供较小的图像副本用于预览，定量分析仍应使用原件。")
            if im.mode in {"I", "F"} or im.mode.startswith("I;16"):
                channel = im.convert("F")
                low, high = channel.getextrema()
                if not (math.isfinite(low) and math.isfinite(high)):
                    raise ValueError("Non-finite display range")
                channel.thumbnail((1600, 1600))
                if high > low:
                    scale = 255 / (high - low)
                    display = channel.point(lambda value: (value - low) * scale).convert("L")
                else:
                    display = Image.new("L", channel.size, 0 if low == 0 else 128)
            else:
                im.thumbnail((1600, 1600))
                display = im.convert("RGB")
            preview = io.BytesIO()
            display.save(preview, format="PNG")
            return "image/png", preview.getvalue(), True
        if path.stat().st_size > 12 * 1024 * 1024:
            raise PreviewLimitError("原图已归档。请提供 12 MB 以内的副本以进行图像预览。")
        mime = im.get_format_mimetype()
        if Path(filename).suffix.lower() in IMAGE_EXTENSIONS and mime != mimetypes.guess_type(filename)[0]:
            raise ValueError("Image content and file extension disagree")
        im.verify()
    return mime, path.read_bytes(), False


def _workbook_lines(path):
    with zipfile.ZipFile(path) as archive:
        if sum(part.file_size for part in archive.infolist()) > 256 * 1024 * 1024:
            raise PreviewLimitError("工作簿展开后超过预览容量。请选取所需工作表并导出 CSV。")
    with open(path, "rb") as source, closing(load_workbook(source, read_only=True, data_only=True, keep_links=False)) as book:
        for sheet in book:
            yield f"Sheet: {sheet.title}\n"
            sheet.reset_dimensions()
            for n, row in enumerate(sheet.iter_rows(values_only=True), 1):
                yield f"{n}\t" + "\t".join("" if cell is None else str(cell) for cell in row) + "\n"


def _text_chunks(path):
    with open(path, "rb") as source:
        signature = source.read(4)
        source.seek(0)
        encoding = text_encoding(signature)
        with io.TextIOWrapper(source, encoding=encoding) as decoded:
            while chunk := decoded.read(8192):
                yield chunk


def _index_text(item, path):
    if Path(item.filename).suffix.lower() == ".pdf":
        from .knowledge.ingestion import extract_text_from_file
        return extract_text_from_file(str(path), "application/pdf")
    return "".join(_text_chunks(path))


def case_index_text(item):
    with verified_original(item) as (path, _):
        return _index_text(item, path)


def read_file(item, offset, limit, *, raw_image=False):
    result = {**file_reference(item), "attachment_id": item.id}
    try:
        with verified_original(item) as (path, actual):
            return _read_snapshot(item, path, offset, limit,
                                  {**result, "content_hash": actual, "integrity":"verified" if item.content_hash else "computed"},
                                  raw_image)
    except FileNotFoundError:
        return {**result, "status": "failed", "failure_code":"missing_original", "expected_sha256":item.content_hash,
                "message": "附件记录存在，但找不到原文件。请从备份恢复或重新提供文件。"}
    except OriginalMismatch as exc:
        return {**result, "status": "failed", "failure_code":"original_mismatch", "message": str(exc),
                "expected_sha256":exc.expected, "observed_sha256":exc.actual}
    except OSError:
        return {**result, "status": "failed", "failure_code":"storage_error", "message": "无法建立用于校验的原件副本，请检查存储是否可读以及可用空间。"}


def _read_snapshot(item, path, offset, limit, result, raw_image):
    mode = result["read_mode"]
    try:
        if mode == "archive":
            with path.open("rb") as source:
                if image_mime(source):
                    result["read_mode"] = mode = "image"
                elif tabular_text(text_sample(source)):
                    result["read_mode"] = mode = "text"
                else:
                    return {**result, "status": "unsupported", "message": "原件已归档，可下载。此格式没有直接预览。可以用支持该格式的计算读取整份原件，或补充仪器导出的表格、图像及格式说明。"}
        if mode == "image":
            mime, data, display_copy = _image_bytes(path, item.filename)
            image = {"id":item.id, "mime":mime, "display_copy":display_copy}
            if raw_image:
                image["data"] = data
            else:
                image["data_url"] = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
            return {**result, "status": "completed", "_images": [image],
                    "message": ("预览为首帧显示副本，高位深灰度图按本帧读数范围拉伸。" if display_copy else "预览用于查看图像。") + "定量计算读取归档原件与标定。"}
        if Path(item.filename).suffix.lower() == ".pdf":
            text = _index_text(item, path)
            page, total = text[offset:offset + limit], len(text)
            more = offset + limit < total
        else:
            chunks = _workbook_lines(path) if mode == "workbook" else _text_chunks(path)
            parts, position = [], 0
            with closing(chunks):
                for chunk in chunks:
                    end = position + len(chunk)
                    if end > offset:
                        parts.append(chunk[max(0, offset-position):max(0, offset+limit-position)])
                    position = end
                    if position > offset + limit:
                        break
            more = position > offset + limit
            page, total = "".join(parts), None if more else position
        if not page and offset == 0:
            return {**result, "status": "needs_input", "message": "原件中未读到可用文字。请补充可读的数据导出或图像。"}
        if mode == "workbook":
            result["message"] = "预览各工作表保存的单元格值；公式使用文件内缓存，未保存的计算值可能为空。"
        ext = Path(item.filename).suffix.lower()
        can_plot = mode == "workbook" or ext in {".csv", ".tsv"} or (ext not in TEXT_EXTENSIONS and mode == "text")
        if not can_plot and ext in {".txt", ".log"}:
            with path.open("rb") as source:
                can_plot = tabular_text(text_sample(source))
        return {**result, "status": "completed", "text": page, "table_candidate": can_plot, "total_characters": total,
                "next_offset": offset + limit if more else None}
    except PreviewLimitError as exc:
        return {**result, "status": "needs_input", "message": str(exc)}
    except UnicodeDecodeError:
        return {**result, "status": "needs_input", "message": "原件已归档，文本编码无法识别。请提供 UTF-8 或带字节序标记的 UTF-16 副本。"}
    except Exception:
        return {**result, "status": "needs_input",
                "message": "原件可下载，当前预览未能解码。请检查文件是否完整；文本可另存为 UTF-8，工作簿可导出 CSV。"}

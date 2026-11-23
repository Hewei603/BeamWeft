"""Coordinate file identity and atomic saves across local document windows."""
import hashlib
import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile, mkdtemp

from .storage_paths import PROJECT_SUFFIX, database_path

MARKER = "opened-case.json"


def fingerprint(source):
    with source.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_snapshot_file(path, write, check_destination, *, new=False):
    """Publish a finished snapshot beside its destination, leaving partial writes private."""
    check_destination()
    with NamedTemporaryFile(dir=path.parent, prefix=".beamweft-", delete=False) as stream:
        target = Path(stream.name)
    try:
        write(target)
        digest = fingerprint(target)
        check_destination()
        if new and os.name == "nt":
            os.rename(target, path)  # Windows rename refuses to replace an intervening file.
        elif new:
            os.link(target, path)
        else:
            os.replace(target, path)
        return {"filename": path.name, "sha256": digest}
    finally:
        target.unlink(missing_ok=True)


def _lost_project(folder):
    """Recognize an old working copy whose root case was deleted through the library UI."""
    marker = folder / MARKER
    if not marker.is_file():
        return False
    database = database_path(folder)
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
        case_id = record["case_id"]
        if record["sha256"] != folder.name or type(case_id) is not int or not 0 < case_id < 2**63:
            raise ValueError
        if not database.exists():
            return True
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            return db.execute("SELECT 1 FROM experiment_cases WHERE id=?", (case_id,)).fetchone() is None
    except (OSError, KeyError, TypeError, ValueError, sqlite3.Error) as exc:
        raise ValueError(f"无法核对本地工作副本。请保留此目录，检查访问权限，或在新的接收目录打开工程文件。\n{folder}") from exc


def document_workspace(preferred, digest):
    """Choose a stable recovery location without moving an old copy or its original files."""
    folder = preferred
    previous = None
    generation = 0
    while _lost_project(folder):
        previous = folder
        generation += 1
        folder = preferred.parent / f"recovered-{generation}" / digest
    recovered_from = str(previous) if previous is not None and not folder.exists() else None
    folder.parent.mkdir(parents=True, exist_ok=True)
    return folder, recovered_from


class NativeDocument:
    def __init__(self, path, root):
        self.path = Path(path).resolve()
        identity = hashlib.sha256(os.fsencode(os.path.normcase(str(self.path)))).hexdigest()
        self.root = Path(root).resolve() / ("file-" + identity[:32])
        self.root.mkdir(parents=True, exist_ok=True)
        self.folder = None
        self.recovered_from = None

    @contextmanager
    def locked(self):
        with closing(sqlite3.connect(self.root / "document.db", timeout=30)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS document (id INTEGER PRIMARY KEY, digest TEXT, folder TEXT)")
            yield db

    def workspace(self, db, digest):
        row = db.execute("SELECT folder FROM document WHERE digest=?", (digest,)).fetchone()
        if row and (self.root / row[0] / MARKER).is_file():
            self.folder, self.recovered_from = document_workspace(self.root / row[0], digest)
        else:
            self.folder = Path(mkdtemp(dir=self.root, prefix="work-")) / digest
        if not row or self.root / row[0] != self.folder:
            db.execute("INSERT OR REPLACE INTO document VALUES (1,?,?)",
                       (digest, str(self.folder.relative_to(self.root))))
        return self.folder

    def save(self, write, *, selection=None):
        if self.path.suffix.lower() != PROJECT_SUFFIX:
            raise ValueError("请将旧格式工程另存为 .beamweft 文件。")
        with self.locked() as db:
            row = db.execute("SELECT digest, folder FROM document WHERE id=1").fetchone()
            if selection is not None and row and selection.previous_digest != row[0]:
                raise ValueError("选择的位置已被其他程序改动。请重新选择要保存的工程文件。")
            def check_source():
                if (not row or self.root / row[1] != self.folder or not self.path.is_file()
                        or fingerprint(self.path) != row[0]):
                    raise ValueError("磁盘上的工程文件已移动或已被修改。请另存为工程文件，或重新打开磁盘版本。")
            receipt = write_snapshot_file(self.path, write, check_source)
            db.execute("UPDATE document SET digest=? WHERE id=1", (receipt["sha256"],))
        return receipt

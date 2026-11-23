"""Keep OS-selected Save As destinations local to the running document window."""
import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from .native_document import fingerprint, write_snapshot_file


@dataclass(frozen=True)
class Destination:
    path: Path
    previous_digest: str | None

    def check(self):
        current = fingerprint(self.path) if self.path.exists() else None
        if current != self.previous_digest:
            raise ValueError("选择的位置已被其他程序改动。请重新选择要保存的工程文件。")

    def write(self, build):
        return write_snapshot_file(self.path, build, self.check, new=self.previous_digest is None)


class DocumentDestinations:
    def __init__(self):
        self._selected = {}
        self._lock = Lock()

    def choose(self, source):
        script = Path(__file__).resolve().parents[1] / "scripts" / "choose_project_location.py"
        result = subprocess.run(
            [sys.executable, str(script), str(source)], capture_output=True, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"}, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise ValueError("无法打开本机保存对话框。请返回桌面窗口重试。")
        name = result.stdout.rstrip("\r\n")
        if not name:
            return None
        path = Path(name).resolve()
        if path.suffix.lower() != ".beamweft":
            raise ValueError("请选择扩展名为 .beamweft 的工程文件。")
        target = Destination(path, fingerprint(path) if path.exists() else None)
        now = time.monotonic()
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._selected = {k: v for k, v in self._selected.items() if v[0] > now}
            self._selected[token] = (now + 86400, target)
        return token

    def take(self, token):
        with self._lock:
            entry = self._selected.pop(token, None)
        if entry is None or entry[0] <= time.monotonic():
            raise ValueError("上次选择的保存位置已失效。请重新选择。")
        return entry[1]

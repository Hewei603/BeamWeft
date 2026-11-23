"""The production frontend used by the desktop project library and document windows."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from starlette.exceptions import HTTPException
from starlette.responses import HTMLResponse, PlainTextResponse
from starlette.staticfiles import StaticFiles

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
DISTRIBUTION_MARKER = "beamweft-distribution.json"


def asset_hashes(dist):
    hashes = {}
    for path in sorted(dist.rglob("*")):
        if path.is_file() and path != dist / DISTRIBUTION_MARKER:
            with path.open("rb") as stream:
                hashes[path.relative_to(dist).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    return hashes


def name_desktop_console(title):
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(title)


def desktop_assets(data_root, frontend=FRONTEND):
    dist = frontend / "dist"
    marker = dist / DISTRIBUTION_MARKER
    if marker.exists() or not (frontend / "vite.config.js").is_file():
        try:
            manifest = json.loads(marker.read_text(encoding="utf-8"))
            hashes = manifest["sha256"]
            if manifest["format"] == 1 and "index.html" in hashes and hashes == asset_hashes(dist):
                return dist
        except (KeyError, OSError, TypeError, ValueError):
            pass
        raise ValueError("工作台界面文件缺失或已改变。请把完整的软件 ZIP 重新解压到新目录，然后从该目录启动。实验工程保存在独立的数据目录。")

    files = [frontend / name for name in ("index.html", "package.json", "package-lock.json", "vite.config.js")]
    for name in (".env", ".env.local", ".env.production", ".env.production.local"):
        if (frontend / name).is_file():
            files.append(frontend / name)
    for name in ("public", "src"):
        files.extend(path for path in (frontend / name).rglob("*") if path.is_file())
    signature = hashlib.sha256()
    # The main library uses the compiled API address. A native document instead uses its own origin.
    for key, value in sorted(os.environ.items()):
        if key.startswith("VITE_") or key == "NODE_ENV":
            signature.update(os.fsencode(key) + b"=" + os.fsencode(value) + b"\0")
    for path in sorted(files):
        signature.update(path.relative_to(frontend).as_posix().encode() + b"\0")
        with path.open("rb") as stream:
            signature.update(hashlib.file_digest(stream, "sha256").digest())
    parent = data_root / "desktop-ui"
    target = parent / signature.hexdigest()[:32]
    if (target / "index.html").is_file():
        return target
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=parent) as temp:
        output = Path(temp) / "dist"
        subprocess.run(["node", str(frontend / "node_modules/vite/bin/vite.js"), "build", "--outDir", str(output)],
                       cwd=frontend, stdout=sys.stderr, check=True)
        try:
            output.rename(target)
        except OSError:
            if not (target / "index.html").is_file():
                raise
    return target


class DesktopFrontend:
    def __init__(self, dist, head=""):
        if not (dist / "index.html").is_file():
            raise ValueError("缺少前端程序，请用 Launch-BeamWeft.bat 打开，或先在 frontend 运行 npm run build。")
        self.html = (dist / "index.html").read_text(encoding="utf-8").replace("<head>", "<head>" + head, 1)
        self.static = StaticFiles(directory=dist)
        self.public_files = {"/" + path.name for path in dist.iterdir() if path.is_file() and path.name != "index.html"}

    async def response(self, request):
        path = request.url.path
        if path.startswith("/assets/") or path in self.public_files or path == "/favicon.ico":
            try:
                return await self.static.get_response(path.lstrip("/"), request.scope)
            except HTTPException as exc:
                return PlainTextResponse("Asset unavailable", status_code=exc.status_code)
        return HTMLResponse(self.html, headers={"Cache-Control": "no-store"})

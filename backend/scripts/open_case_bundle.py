"""Open an exported BeamWeft case with the installed application and a private local data directory."""
import argparse
import asyncio
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import webbrowser
import zipfile
from contextlib import closing, nullcontext
from html import escape
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from threading import Lock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))
from app.storage_paths import PREVIOUS_PROJECT_SUFFIX, PROJECT_SUFFIX, database_path, library_data_root


def document_route(origin, marker):
    path = origin + f"/cases/{marker['case_id']}"
    revision = marker.get("opening_revision_id")
    return path if revision is None else f"{path}?model_revision={revision}"


def serve(folder, marker, port, show_browser, document=None, assets=None):
    import uvicorn
    from app.desktop_ui import DesktopFrontend
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    from starlette.responses import JSONResponse, PlainTextResponse

    from app.main import app

    dist = assets or REPO / "frontend" / "dist"
    legacy = "legacy" if marker["legacy"] else "current"
    native = ""
    if document:
        path = document.path
        label = str(Path(path.parent.name or path.parent.anchor) / path.name)
        native = (f'<meta name="beamweft-native-file" content="{marker["case_id"]}" '
                  f'data-save-as="{str(path.suffix.lower() != PROJECT_SUFFIX).lower()}" data-label="{escape(label)}" data-path="{escape(str(path))}">')
    ui = DesktopFrontend(dist, f'<meta name="beamweft-case-copy" content="{legacy}">{native}')
    app.state.opened_case = marker
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        route = document_route(origin, marker)

        if document:
            from fastapi import HTTPException as ApiError
            from fastapi import Request
            from fastapi.responses import JSONResponse as SaveResponse
            from app.api.cases import write_case_snapshot
            from app.api.desktop_documents import launch_document
            from app.auth.security import Principal
            from app.database import engine
            from app.case_package import PackageError
            from app.document_destinations import DocumentDestinations

            save_lock = Lock()
            destinations = DocumentDestinations()

            def local_write(request):
                if request.headers.get("origin") != origin:
                    raise ApiError(403, "请从本机工程窗口保存文件。")

            @app.post("/api/document/save-location")
            def choose_location(request: Request):
                local_write(request)
                try:
                    return {"selection": destinations.choose(document.path)}
                except (OSError, ValueError) as exc:
                    raise ApiError(409, str(exc)) from exc

            @app.post("/api/document/save")
            def save_document(request: Request, revision_id: int | None = None, selection: str | None = None):
                local_write(request)

                def snapshot(path):
                    return write_case_snapshot(
                        engine, marker["case_id"], Principal(actor="desktop", role="user"), path, revision_id)

                try:
                    target = destinations.take(selection) if selection is not None else None
                    with save_lock:
                        if target is None or target.path == document.path:
                            receipt = document.save(snapshot, selection=target)
                            app.state.opened_case = {**app.state.opened_case, "sha256": receipt["sha256"],
                                                     "opening_revision_id": revision_id}
                            return receipt
                        receipt = target.write(snapshot)
                except (PackageError, ValueError, OSError) as exc:
                    return SaveResponse({"detail": str(exc)}, status_code=409)
                try:
                    url = launch_document(target.path, document.root.parent)
                    if not url:
                        raise ValueError("缺少新文件的工作台地址。")
                except (ApiError, OSError, ValueError) as exc:
                    detail = getattr(exc, "detail", str(exc))
                    return {**receipt, "open_error": f"工程已保存到 {target.path}，当前窗口仍在原文件。"
                            f"可从已保存的位置重新打开。{detail}"}
                return {**receipt, "url": url}

        @app.middleware("http")
        async def local_workspace(request, call_next):
            if request.headers.get("origin") not in (None, origin):
                return PlainTextResponse("Open this case in its local workbench window.", status_code=403)
            path = request.url.path
            if path == "/.well-known/beamweft-case":
                return JSONResponse({"sha256": app.state.opened_case["sha256"]})
            if request.method in {"GET", "HEAD"} and not (
                path == "/api" or path.startswith(("/api/", "/docs", "/openapi.json", "/redoc")) or path == "/health"
            ):
                return await ui.response(request)
            return await call_next(request)

        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1"])
        (folder / "desktop-port").write_text(str(sock.getsockname()[1]), encoding="ascii")
        sock.listen(32)
        print(f"本机工程 {folder}\n{route}", flush=True)
        if show_browser:
            # Queue connections while the application finishes startup.
            webbrowser.open(route)
        server = uvicorn.Server(uvicorn.Config(app, proxy_headers=False, log_level="warning"))
        asyncio.run(server.serve(sockets=[sock]))


def open_window(folder, marker, port, show_browser, document=None, assets=None):
    # Each workspace has one scheduler. Two processes must not recover or
    # cancel each other's live runs. This OS-backed lock releases on a crash.
    with closing(sqlite3.connect(folder / "desktop-lock.db", timeout=0.2)) as lock:
        try:
            lock.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_BUSY:
                raise
            for _ in range(40):
                try:
                    active_port = int((folder / "desktop-port").read_text(encoding="ascii"))
                    origin = f"http://127.0.0.1:{active_port}"
                    with closing(HTTPConnection("127.0.0.1", active_port, timeout=0.3)) as client:
                        client.request("GET", "/.well-known/beamweft-case")
                        reply = client.getresponse()
                        if reply.status == 200 and json.loads(reply.read(200)).get("sha256") == marker["sha256"]:
                            route = document_route(origin, marker)
                            print(route, flush=True)
                            if show_browser:
                                webbrowser.open(route)
                            return
                except (OSError, ValueError, HTTPException):
                    pass
                time.sleep(0.15)
            raise ValueError("这个工作副本正在另一个窗口启动。请稍后重新打开。") from exc
        serve(folder, marker, port, show_browser, document, assets)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", nargs="?")
    data_root = library_data_root()
    parser.add_argument("--root", type=Path, default=data_root / "opened-cases")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--extract-only", action="store_true")
    parser.add_argument("--build-frontend", action="store_true")
    args = parser.parse_args()
    archive = args.archive
    if not archive:
        from tkinter import Tk, filedialog
        window = Tk()
        window.withdraw()
        try:
            archive = filedialog.askopenfilename(title="打开工程文件", filetypes=[
                ("所有可用工程", f"*{PROJECT_SUFFIX} *{PREVIOUS_PROJECT_SUFFIX} *.zip"),
                ("BeamWeft 工程", f"*{PROJECT_SUFFIX}"), ("历史工程包", f"*{PREVIOUS_PROJECT_SUFFIX} *.zip")])
        finally:
            window.destroy()
        if not archive:
            return
    from app.desktop_ui import desktop_assets
    assets = desktop_assets(data_root) if args.build_frontend and not args.extract_only else None
    os.environ["BEAMWEFT_DESKTOP_UI"] = "1"
    os.environ.pop("BEAMWEFT_LIBRARY_INSTANCE", None)
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    from app.native_document import NativeDocument, document_workspace
    document = NativeDocument(archive, root) if Path(archive).suffix.lower() in (PROJECT_SUFFIX, PREVIOUS_PROJECT_SUFFIX) else None
    with (document.locked() if document else nullcontext()) as registry, Path(archive).open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
        if document:
            folder = document.workspace(registry, digest)
            recovered_from = document.recovered_from
        else:
            folder, recovered_from = document_workspace(root / digest, digest)
        # Set storage paths before any ORM import creates the application's engine.
        os.environ.setdefault("AGENT_SETTINGS_FILE", str(data_root.resolve() / "uploads" / ".agent-settings.json"))
        os.environ.update(DATABASE_URL="sqlite:///" + database_path(folder).as_posix(),
                          UPLOAD_DIR=str(folder / "uploads"), VECTOR_STORE_DIR=str(folder / "vectors"),
                          MIGRATE_LEGACY_ON_START="false", AUTO_CREATE_TABLES="true", REQUIRE_AUTH="false",
                          RETRIEVAL_BACKEND="sql_json", CORS_ORIGINS="[]")
        from app.open_case import open_stream, opening_revision
        _, marker = open_stream(source, folder.parent, folder.name)
        # In-place saves reuse the working database. The file can now open on
        # a different revision from the one used to create that database.
        if digest != folder.name:
            with zipfile.ZipFile(source) as archive:
                marker = {**marker, "sha256": digest, "opening_revision_id": opening_revision(archive)}
        if recovered_from:
            marker = {**marker, "recovered_from": recovered_from}
    if args.extract_only:
        print(json.dumps({**marker, "directory": str(folder)}, ensure_ascii=False))
    else:
        open_window(folder, marker, args.port, not args.no_browser, document, assets)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print("OPEN_ERROR " + str(exc), flush=True)
        print(str(exc), file=sys.stderr)
        sys.exit(1)

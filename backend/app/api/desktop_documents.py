"""Open a local file chosen through the desktop launcher."""
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request

from ..config import get_settings
from ..auth.security import get_current_principal

router = APIRouter(dependencies=[Depends(get_current_principal)])


def available(request):
    return (os.environ.get("LASERCLAW_DESKTOP_UI") == "1"
            and request.client and request.client.host in {"::1", "127.0.0.1"}
            and request.url.hostname in {"127.0.0.1", "localhost", "::1"}
            and not get_settings().require_auth)


@router.get("/document-picker")
def picker_status(request: Request):
    return {"available": bool(available(request))}


@router.post("/document-picker")
def open_document(request: Request):
    origin = request.headers.get("origin", "")
    if (not available(request) or urlsplit(origin).hostname not in {"127.0.0.1", "localhost", "::1"}
            or origin not in [str(request.base_url).rstrip("/"), *get_settings().cors_origins]):
        raise HTTPException(403, "请从本机工作台打开工程文件。")
    return {"url": launch_document()}


def launch_document(path=None, root=None):
    script = Path(__file__).resolve().parents[2] / "scripts" / "open_case_bundle.py"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    args = [sys.executable, str(script), "--no-browser", "--build-frontend"]
    if path is not None:
        args.append(str(path))
    if root is not None:
        args.extend(["--root", str(root)])
    child = subprocess.Popen(args,
                             stdout=subprocess.PIPE, encoding="utf-8", errors="replace", env=env,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    with child.stdout as output:
        for line in output:
            if re.fullmatch(r"http://127\.0\.0\.1:\d+/cases/\d+(?:\?model_revision=[1-9]\d*)?", line.strip()):
                return line.strip()
            if line.startswith("OPEN_ERROR "):
                raise HTTPException(422, line[11:].strip())
    if child.wait():
        raise HTTPException(422, "工程未能打开。请检查文件完整性和本机前端程序。")
    return None

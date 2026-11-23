"""Identify running desktop services so reopening the library preserves active work."""
import argparse
from contextlib import closing
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[2]
PROBE = "/.well-known/beamweft-library"
OCCUPIED = 4


def library_data_root():
    return Path(os.environ.get("LASERCLAW_DATA_DIR") or Path.home() / "LaserClaw-Data").resolve()


def build_identity(repo, data_root=None, api_port=8000, ui_port=5173):
    # An installer may rewrite an unchanged lockfile. Compare the actual inputs.
    digest = hashlib.sha256(os.fsencode(os.path.normcase(str(repo.resolve()))))
    root = data_root if data_root is not None else library_data_root()
    digest.update(b"\0" + os.fsencode(os.path.normcase(str(root.resolve()))))
    digest.update(f"\0{api_port}:{ui_port}\0".encode())
    files = [repo / ".env", repo / "backend/.env", repo / "Launch-BeamWeft.bat"]
    files.extend((repo / "backend").glob("requirements*.txt"))
    for pattern in ("*.py", "*.json"):
        files.extend((repo / "backend/app").rglob(pattern))
    files.extend((repo / "backend/scripts").glob("*.py"))
    if (repo / "frontend/dist/beamweft-distribution.json").is_file():
        files.extend((repo / "frontend/dist").rglob("*"))
    for name in ("src", "public"):
        files.extend((repo / "frontend" / name).rglob("*"))
    for pattern in ("package*.json", "vite.config.*", "index.html", ".env*"):
        files.extend((repo / "frontend").glob(pattern))
    for path in sorted(files):
        if path.is_file():
            digest.update(path.relative_to(repo).as_posix().encode() + b"\0")
            with path.open("rb") as stream:
                digest.update(hashlib.file_digest(stream, "sha256").digest())
    return digest.hexdigest()


def running_services(identity, api_port, ui_port, *, quiet=False):
    mask = 0
    for flag, name, port in ((1, "api", api_port), (2, "ui", ui_port)):
        # Windows can take two seconds to report ECONNREFUSED for an unused loopback port.
        # Test availability without starting a listener. Only occupied ports need an HTTP probe.
        with socket.socket() as probe:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind(("127.0.0.1", port))
                continue
            except OSError:
                pass
        try:
            with closing(HTTPConnection("127.0.0.1", port, timeout=3)) as conn:
                conn.request("GET", PROBE)
                reply = conn.getresponse()
                if reply.status == 200 and json.loads(reply.read(1024)) == {"instance": identity, "service": name}:
                    mask |= flag
                    continue
        except ConnectionRefusedError:
            continue
        except (OSError, HTTPException, ValueError):
            pass
        if not quiet:
            print(f"端口 {port} 上的服务与所选资料库或软件版本不匹配。请先保存工作，再关闭旧服务或选用其他端口。")
        return OCCUPIED
    return mask


def launch_library(identity, api_port, ui_port, data_root, *, creationflags=None):
    data_root.mkdir(parents=True, exist_ok=True)
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if creationflags is None else creationflags
    data_root = data_root.resolve()
    api_origin = f"http://127.0.0.1:{api_port}"
    env = {**os.environ, "BEAMWEFT_LIBRARY_INSTANCE": identity, "LASERCLAW_DESKTOP_UI": "1",
           "LASERCLAW_DATA_DIR": str(data_root), "DATABASE_URL": "sqlite:///" + (data_root / "laserclaw.db").as_posix(),
           "UPLOAD_DIR": str(data_root / "uploads"), "VECTOR_STORE_DIR": str(data_root / "vector_store"),
           "AUTO_CREATE_TABLES": "true", "BEAMWEFT_API_ORIGIN": api_origin, "VITE_API_URL": api_origin}
    if ui_port != 5173:
        env["CORS_ORIGINS"] = json.dumps([f"http://127.0.0.1:{ui_port}", f"http://localhost:{ui_port}"])
    if data_root != (Path.home() / "LaserClaw-Data").resolve():
        # A separately selected library must not silently acquire an old checkout's data.
        env.setdefault("MIGRATE_LEGACY_ON_START", "false")
    # Serialize the check and start across simultaneous double-clicks. A crashed launcher releases the lock.
    with closing(sqlite3.connect(data_root / "library-launch.db", timeout=75)) as lock:
        lock.execute("BEGIN IMMEDIATE")
        mask = running_services(identity, api_port, ui_port)
        if mask == OCCUPIED:
            return 1
        processes = []
        backend = REPO / "backend"
        if not mask & 1:
            processes.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1", "--port", str(api_port)], cwd=backend, env=env, creationflags=flags))
        if not mask & 2:
            processes.append(subprocess.Popen([sys.executable, str(backend / "scripts/serve_desktop_ui.py"),
                "--port", str(ui_port)], cwd=backend, env=env, creationflags=flags))
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if running_services(identity, api_port, ui_port, quiet=True) == 3:
                return 0
            if any(proc.poll() is not None for proc in processes):
                break
            time.sleep(0.3)
        print("工作台尚未就绪。请检查 API 与 Web 的启动信息；再次打开启动器可连接已就绪的服务。")
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", action="store_true")
    parser.add_argument("--launch", action="store_true")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--ui-port", type=int, default=5173)
    args = parser.parse_args()
    data_root = library_data_root()
    identity = build_identity(REPO, data_root, args.api_port, args.ui_port)
    if args.identity:
        print(identity)
        return 0
    if args.launch:
        return launch_library(identity, args.api_port, args.ui_port, data_root)
    return running_services(identity, args.api_port, args.ui_port)


if __name__ == "__main__":
    raise SystemExit(main())

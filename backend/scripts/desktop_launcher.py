"""Prepare and open the Windows workbench from a source checkout or a pilot distribution."""
import argparse
from contextlib import closing
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import webbrowser

from desktop_status import OCCUPIED, REPO, build_identity, library_data_root, running_services


def dependency_key(files, runtime):
    digest = hashlib.sha256(runtime.encode())
    for path in files:
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def installed(stamp, key):
    return stamp.is_file() and stamp.read_text(encoding="ascii") == key


def prepare(repo):
    backend = repo / "backend"
    frontend = repo / "frontend"
    packaged = (frontend / "dist/beamweft-distribution.json").is_file()
    npm = shutil.which("npm.cmd") if os.name == "nt" else shutil.which("npm")
    if not packaged and not (npm and shutil.which("node")):
        raise ValueError("从源码构建界面需要 Node.js 22.12 以上或兼容版本。安装后重新打开，或使用已构建界面的试用包。")

    venv = backend / ".venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    venv.mkdir(exist_ok=True)
    # Concurrent first launches share the same dependency installation even if they use different libraries.
    with closing(sqlite3.connect(venv / ".beamweft-setup.db", timeout=900)) as setup:
        setup.execute("BEGIN IMMEDIATE")
        env_file = repo / ".env"
        example = repo / ".env.example"
        if not env_file.exists() and example.is_file():
            with env_file.open("xb") as target:
                target.write(example.read_bytes())
        if not python.is_file():
            print("首次启动，正在创建独立 Python 环境。", flush=True)
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        requirements = backend / ("requirements-desktop.txt" if packaged else "requirements.txt")
        # Source requirements include the desktop runtime. Track both when either changes.
        inputs = [requirements]
        desktop_requirements = backend / "requirements-desktop.txt"
        if not packaged and desktop_requirements.is_file():
            inputs.append(desktop_requirements)
        key = dependency_key(inputs, (venv / "pyvenv.cfg").read_text(encoding="utf-8"))
        stamp = venv / ".beamweft-requirements.sha256"
        if not installed(stamp, key):
            print("正在安装后端依赖，第一次需要网络连接。", flush=True)
            subprocess.run([str(python), "-m", "pip", "install", "-r", str(requirements)], check=True)
            stamp.write_text(key, encoding="ascii")
        if not packaged:
            node_version = subprocess.check_output(["node", "--version"], text=True).strip()
            package = [frontend / "package.json", frontend / "package-lock.json"]
            stamp = frontend / "node_modules/.beamweft-dependencies.sha256"
            vite = frontend / "node_modules/vite/bin/vite.js"
            if not vite.is_file() or not installed(stamp, dependency_key(package, node_version)):
                print("正在安装前端依赖。", flush=True)
                subprocess.run([npm, "install", "--no-audit", "--no-fund"], cwd=frontend, check=True)
                stamp.write_text(dependency_key(package, node_version), encoding="ascii")
    return python


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", nargs="?", help="工程文件路径；/open 可打开文件选择窗口")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--ui-port", type=int, default=5173)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not (0 < args.api_port < 65536 and 0 < args.ui_port < 65536 and args.api_port != args.ui_port):
        parser.error("API 和界面各需要一个不同的 1–65535 端口。")
    root = library_data_root()
    print(f"BeamWeft 工作台　实验资料库：{root}", flush=True)
    if args.archive:
        python = prepare(REPO)
        command = [str(python), str(REPO / "backend/scripts/open_case_bundle.py"), "--build-frontend"]
        if args.no_browser:
            command.append("--no-browser")
        if args.archive.lower() != "/open":
            command.append(str(Path(args.archive).resolve()))
        return subprocess.call(command)
    identity = build_identity(REPO, root, args.api_port, args.ui_port)
    state = running_services(identity, args.api_port, args.ui_port)
    if state == OCCUPIED:
        return 1
    if state != 3:
        python = prepare(REPO)
        command = [str(python), str(REPO / "backend/scripts/desktop_status.py"), "--launch",
                   "--api-port", str(args.api_port), "--ui-port", str(args.ui_port)]
        if subprocess.call(command) != 0:
            return 1
    url = f"http://127.0.0.1:{args.ui_port}"
    print(f"工作台已就绪：{url}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"启动准备失败（{Path(str(exc.cmd[0])).name}，退出码 {exc.returncode}）。"
              "请查看上方安装或构建输出，检查网络和安装目录的写入权限，再重新打开启动器。", file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"无法启动工作台：{exc}。请检查安装目录、资料库位置和可用磁盘空间，修正后重新打开。", file=sys.stderr)
        raise SystemExit(1)

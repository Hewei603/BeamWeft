"""Build the Windows lab trial from reviewed application inputs in the working tree."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))
from app.desktop_ui import DISTRIBUTION_MARKER, asset_hashes  # noqa: E402
from app.version import APP_VERSION  # noqa: E402


PUBLIC_ART = ("beamweft.png", "background.png", "optical-relay.webp", "optical-relay-small.webp")
DESKTOP_SCRIPTS = ("desktop_launcher", "desktop_status", "serve_desktop_ui", "open_case_bundle", "choose_project_location")
RUNTIME_DATA = ("physics/data/materials.json", "workbench/element_catalog.json", "workbench/action_labels.json")


def source_files(directory, extensions):
    for path in sorted(directory.rglob("*")):
        parts = path.relative_to(directory).parts
        if any(part.startswith(".") or part == "__pycache__" for part in parts):
            continue
        if path.is_file() and path.suffix.lower() in extensions:
            if not path.resolve().is_relative_to(directory.resolve()):
                raise ValueError(f"Source file resolves outside its directory: {path}")
            yield path


def build_interface(stage, dist):
    frontend = REPO / "frontend"
    for path in source_files(frontend / "src", {".js", ".jsx", ".ts", ".tsx", ".css", ".json", ".png", ".svg", ".webp"}):
        target = stage / path.relative_to(frontend)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    (stage / "public").mkdir()
    for name in PUBLIC_ART:
        shutil.copyfile(frontend / "public" / name, stage / "public" / name)
    for name in ("index.html", "package.json", "vite.config.js"):
        shutil.copyfile(frontend / name, stage / name)
    # Vite reads the current directory's .env files and the process's VITE_* variables.
    # Building in a clean root keeps the developer's endpoint and local config out of the ZIP.
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("VITE_")}
    env["NODE_ENV"] = "production"
    subprocess.run(["node", str(frontend / "node_modules/vite/bin/vite.js"), "build", "--outDir", str(dist)],
                   cwd=stage, env=env, check=True)
    if not (dist / "THIRD-PARTY-LICENSES.md").is_file():
        raise ValueError("The build did not emit the bundled frontend dependency notices.")
    manifest = {"format": 1, "version": APP_VERSION, "sha256": asset_hashes(dist)}
    (dist / DISTRIBUTION_MARKER).write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def runtime_sources():
    for name in ("LICENSE", "CHANGELOG.md", "Launch-BeamWeft.bat", "backend/requirements-desktop.txt",
                 "backend/requirements.txt", "backend/alembic.ini", "docs/GUIDE_V2.md", "docs/CODEX_EXEC.md",
                 "docs/WORKBENCH.md", "docs/AGENT_FAILURES.md", "docs/WINDOWS_TRIAL.md"):
        yield REPO / name
    yield from source_files(REPO / "backend/app", {".py"})
    yield from (REPO / "backend/app" / name for name in RUNTIME_DATA)
    yield from source_files(REPO / "backend/alembic", {".py", ".mako"})
    yield from (REPO / "backend/scripts" / (name + ".py") for name in DESKTOP_SCRIPTS)
    yield from source_files(REPO / "examples", {".py", ".toml"})


def build_archive(destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Keep the clean build root beneath frontend so Node can resolve the maintainer's locked dependencies.
    with TemporaryDirectory(dir=REPO / "frontend", prefix=".trial-build-") as temp:
        stage = Path(temp)
        dist = stage / "dist"
        build_interface(stage, dist)
        with NamedTemporaryFile(dir=destination.parent, suffix=".zip", delete=False) as tmp:
            temp_archive = Path(tmp.name)
        try:
            with ZipFile(temp_archive, "w", ZIP_DEFLATED) as archive:
                for path in runtime_sources():
                    archive.write(path, path.relative_to(REPO).as_posix())
                for path in sorted(dist.rglob("*")):
                    if path.is_file():
                        archive.write(path, "frontend/dist/" + path.relative_to(dist).as_posix())
                archive.write(REPO / "docs/WINDOWS_TRIAL.md", "开始使用.md")
            with ZipFile(temp_archive) as archive:
                damaged = archive.testzip()
                if damaged:
                    raise ValueError(f"Invalid ZIP member: {damaged}")
            temp_archive.replace(destination)
        finally:
            temp_archive.unlink(missing_ok=True)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, nargs="?",
                        default=REPO / "output/acceptance/lab-handoff/BeamWeft-Windows-Pilot.zip")
    print(build_archive(parser.parse_args().destination.resolve()))

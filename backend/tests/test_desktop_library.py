from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from app.desktop_ui import DesktopFrontend, desktop_assets
from app.storage_paths import (
    PREVIOUS_DATABASE, PREVIOUS_DATA_ENV, PREVIOUS_DATA_ROOT, database_path, library_data_root,
)
from scripts.desktop_status import build_identity


def test_desktop_data_selection_keeps_an_existing_library_and_respects_explicit_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("BEAMWEFT_DATA_DIR", raising=False)
    monkeypatch.delenv(PREVIOUS_DATA_ENV, raising=False)
    current = tmp_path / "BeamWeft-Data"
    previous = tmp_path / PREVIOUS_DATA_ROOT
    assert library_data_root() == current
    assert database_path(current) == current / "beamweft.db"
    (current / "desktop-ui").mkdir(parents=True)
    previous.mkdir()
    old_db = previous / PREVIOUS_DATABASE
    old_db.write_bytes(b"previous lab")
    assert library_data_root() == previous
    assert database_path(previous) == old_db
    assert not (current / "beamweft.db").exists()
    (current / "beamweft.db").write_bytes(b"new lab")
    assert library_data_root() == current
    monkeypatch.setenv(PREVIOUS_DATA_ENV, str(previous))
    assert library_data_root() == previous
    monkeypatch.setenv("BEAMWEFT_DATA_DIR", str(current))
    assert library_data_root() == current
    assert old_db.read_bytes() == b"previous lab"


def test_reopening_uses_content_even_if_installers_rewrite_lockfile(tmp_path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    lock = frontend / "package-lock.json"
    lock.write_text('{"version":1}')
    original = build_identity(tmp_path)
    lock.write_text('{"version":1}')
    assert build_identity(tmp_path) == original
    lock.write_text('{"version":2}')
    assert build_identity(tmp_path) != original


def test_service_identity_separates_libraries_port_pairs_and_runtime_inputs(tmp_path):
    program = tmp_path / "program"
    catalog = program / "backend/app/physics/data/materials.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text('{"ordinary":1.5}')
    lab = tmp_path / "lab A"
    base = build_identity(program, lab, 4101, 4102)
    assert build_identity(program, tmp_path / "lab B", 4101, 4102) != base
    assert build_identity(program, lab, 4101, 4103) != base
    assert build_identity(program, lab / ".", 4101, 4102) == base
    catalog.write_text('{"ordinary":1.6}')
    assert build_identity(program, lab, 4101, 4102) != base
    dist = program / "frontend/dist"
    dist.mkdir(parents=True)
    (dist / "beamweft-distribution.json").write_text("{}")
    code = dist / "index.html"
    code.write_text("original")
    original = build_identity(program, lab)
    code.write_text("updated")
    assert build_identity(program, lab) != original


def test_desktop_build_can_be_shared_only_with_the_same_inputs(tmp_path, monkeypatch):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    for name in ("index.html", "package.json", "package-lock.json", "vite.config.js"):
        (frontend / name).write_text(name)
    calls = []

    def build(args, **kwargs):
        dist = Path(args[-1])
        dist.mkdir()
        (dist / "index.html").write_text("<head></head>")
        calls.append(kwargs["cwd"])

    monkeypatch.setattr("app.desktop_ui.subprocess.run", build)
    monkeypatch.setenv("VITE_API_URL", "http://127.0.0.1:8000")
    cache = tmp_path / "library"
    initial = desktop_assets(cache, frontend)
    assert desktop_assets(cache, frontend) == initial
    monkeypatch.setenv("VITE_API_URL", "http://127.0.0.1:8001")
    other = desktop_assets(cache, frontend)
    assert other != initial
    (frontend / ".env.production").write_text("VITE_FIXTURE=blue")
    assert desktop_assets(cache, frontend) not in (initial, other)
    assert calls == [frontend] * 3
    assert (initial / "index.html").is_file()


def test_library_and_native_document_share_assets_but_keep_their_own_route_context(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<head></head><main></main>")
    (tmp_path / "assets" / "plot.js").write_text("const plot = 1;")
    for head in ("", '<meta name="beamweft-case-copy" content="current">'):
        ui = DesktopFrontend(tmp_path, head=head)
        with TestClient(Starlette(routes=[Route("/{path:path}", ui.response)])) as client:
            page = client.get("/cases/23?model_revision=9")
            assert page.text == f"<head>{head}</head><main></main>"
            assert page.headers["cache-control"] == "no-store"
            script = client.get("/assets/plot.js")
            assert "javascript" in script.headers["content-type"]
            assert client.get("/assets/plot.js", headers={"If-None-Match": script.headers["etag"]}).status_code == 304
            assert client.get("/assets/missing.js").status_code == 404

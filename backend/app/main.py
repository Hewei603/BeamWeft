"""FastAPI application entry point."""
from __future__ import annotations

import logging
import os
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import admin, agent, attachments, case_modules, cases, collaboration, design, evals, generation, inventory, knowledge, literature, settings as settings_api, versioning
from .config import get_settings
from .database import Base, engine, sync_additive_schema
from .version import APP_NAME, APP_VERSION
from .api import workbench
from .api import inventory_label
from .api import desktop_documents
from . import models  # noqa: F401 - register SQLAlchemy models
from .attachment_evidence import OriginalRemoved

settings = get_settings()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("laserclaw")

# Must run before create_all: create_all would materialize an empty database at
# the new location, and the legacy-data copy only ever fills an empty target.
from .data_migration import migrate_legacy_data  # noqa: E402

if settings.migrate_legacy_on_start:
    for _note in migrate_legacy_data(settings):
        logger.info(_note)

if settings.auto_create_tables:
    Base.metadata.create_all(bind=engine)
    # create_all only creates missing tables; a database made by an earlier
    # release also needs the columns added since. See sync_additive_schema.
    sync_additive_schema()
os.makedirs(settings.upload_dir, exist_ok=True)

app = FastAPI(
    title=f"{APP_NAME} API",
    description="Optical system modeling, laser simulations, laboratory measurements and project-based Agent tools.",
    version=APP_VERSION,
)

@app.exception_handler(OriginalRemoved)
async def evidence_removed(request: Request, exc: OriginalRemoved):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Unhandled API error", extra={"request_id": request_id})
        return JSONResponse(status_code=500, content={"detail": "Internal server error", "request_id": request_id})
    response.headers["x-request-id"] = request_id
    return response


# Keep CORS outside the error middleware so even an unexpected failure is
# readable by the frontend, rather than being reported as an offline backend.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# NOTE: upload_dir is deliberately NOT mounted as a static route. Attachments
# are served through /api/attachments/{id} (attachments.py), which checks the
# principal's case ACL; a bare StaticFiles mount would expose every uploaded
# file — case attachments, lab documents, the inventory workbook — to anyone
# who can reach the port, even with REQUIRE_AUTH=true.

app.include_router(cases.router, prefix="/api/cases", tags=["cases"])
app.include_router(desktop_documents.router, prefix="/api/desktop", tags=["desktop"])
app.include_router(case_modules.router, prefix="/api/cases", tags=["case-modules"])
app.include_router(attachments.router, prefix="/api", tags=["attachments"])
app.include_router(generation.router, prefix="/api/cases", tags=["generation"])
app.include_router(knowledge.router, prefix="/api/knowledge", tags=["knowledge"])
app.include_router(agent.router, prefix="/api/agent", tags=["agent"])
app.include_router(admin.router, prefix="/api/admin", tags=["admin"])
app.include_router(collaboration.router, prefix="/api/collaboration", tags=["collaboration"])
app.include_router(versioning.router, prefix="/api/versioning", tags=["versioning"])
app.include_router(evals.router, prefix="/api/evals", tags=["evals"])
app.include_router(inventory.router, prefix="/api/inventory", tags=["inventory"])
app.include_router(inventory_label.router, prefix="/api/inventory", tags=["inventory"])
app.include_router(literature.router, prefix="/api/literature", tags=["literature"])
app.include_router(settings_api.router, prefix="/api/settings", tags=["settings"])
app.include_router(design.router, prefix="/api/cases", tags=["design"])
app.include_router(workbench.router, prefix="/api/cases", tags=["workbench"])


@app.get("/")
async def root():
    return {"message": f"{APP_NAME} API", "version": APP_VERSION, "docs": "/docs"}


_library_instance = os.environ.get("BEAMWEFT_LIBRARY_INSTANCE")
if _library_instance:
    from .desktop_ui import name_desktop_console
    name_desktop_console("BeamWeft API")

    @app.get("/.well-known/beamweft-library", include_in_schema=False)
    def desktop_library_instance():
        return JSONResponse({"instance": _library_instance, "service": "api"}, headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health():
    # The provider block lets the frontend show a "demo mode" banner: mock
    # output reads like real output, so the user must be told which is running.
    # The version lets it show a "stale backend, restart the launcher" banner:
    # the launcher-started process keeps serving old code until its window is
    # closed, and the user has no other way to notice.
    from .providers import provider_status

    return {"status": "healthy", "version": APP_VERSION, "provider": provider_status()}

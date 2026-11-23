"""Start the desktop library using the same production application as a native document."""
import argparse
import os
import sys
from html import escape
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.storage_paths import library_data_root
from app.desktop_ui import DesktopFrontend, desktop_assets, name_desktop_console


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5173)
    args = parser.parse_args()
    data_root = library_data_root()
    name_desktop_console("BeamWeft Web")
    api_origin = escape(os.environ.get("BEAMWEFT_API_ORIGIN", "http://127.0.0.1:8000"), quote=True)
    ui = DesktopFrontend(desktop_assets(data_root), head=f'<meta name="beamweft-api-origin" content="{api_origin}">')
    instance = os.environ.get("BEAMWEFT_LIBRARY_INSTANCE")
    async def library_status(request):
        return JSONResponse({"instance": instance, "service": "ui"}, headers={"Cache-Control": "no-store"})

    routes = [Route("/.well-known/beamweft-library", library_status)] if instance else []
    app = Starlette(routes=[*routes, Route("/{path:path}", ui.response)])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()

"""Serve the full numerical record without a second recursive JSON conversion."""
import gzip

from fastapi import Request, Response
from starlette.responses import JSONResponse


def scientific_json(request: Request, data):
    quality = {}
    for field in request.headers.getlist("accept-encoding"):
        for item in field.lower().split(","):
            coding, _, weight = item.strip().partition(";")
            try:
                q = float(weight.strip().removeprefix("q=")) if weight else 1.
            except ValueError:
                q = 0.
            quality[coding.strip()] = q if 0 <= q <= 1 else 0.
    zipped = quality.get("gzip", quality.get("*", 0.))
    identity = quality.get("identity", 0. if quality.get("*") == 0 else 1.)
    if zipped <= 0 and identity <= 0:
        return Response(status_code=406, headers={"Vary":"Accept-Encoding"})
    # A write can supply its computation lazily so rejected requests create no stored result.
    plain = JSONResponse(data() if callable(data) else data, headers={"Vary": "Accept-Encoding"})
    if zipped <= 0 or identity > zipped or (len(plain.body) < 16384 and identity > 0):
        return plain
    return Response(gzip.compress(plain.body, compresslevel=1, mtime=0), media_type="application/json",
                    headers={"Content-Encoding":"gzip", "Vary":"Accept-Encoding"})

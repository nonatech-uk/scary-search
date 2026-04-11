"""Immich search interceptor — routes smart search through PIF.

Called by nginx when it matches POST /api/search/smart.
Everything else is proxied by nginx directly to Immich.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

IMMICH_URL = os.environ.get("IMMICH_URL", "http://immich-server:2283")
IMMICH_KEY = os.environ["IMMICH_API_KEY"]
PIF_URL = os.environ.get("PIF_URL", "http://pif:8000")
LISTEN_PORT = int(os.environ.get("PROXY_PORT", "2285"))

_SEM = asyncio.Semaphore(10)
_EMPTY_ALBUMS = {"items": [], "count": 0, "total": 0, "facets": []}

_immich: httpx.AsyncClient
_pif: httpx.AsyncClient


@asynccontextmanager
async def _lifespan(app):
    global _immich, _pif
    _immich = httpx.AsyncClient(
        headers={"x-api-key": IMMICH_KEY},
        timeout=30.0,
    )
    _pif = httpx.AsyncClient(timeout=30.0)
    yield
    await _immich.aclose()
    await _pif.aclose()


async def _fetch_asset(asset_id: str) -> dict | None:
    async with _SEM:
        try:
            resp = await _immich.get(f"{IMMICH_URL}/api/assets/{asset_id}")
            if resp.status_code == 200:
                return resp.json()
            log.warning("Asset %s returned %d", asset_id, resp.status_code)
        except Exception:
            log.warning("Failed to fetch asset %s", asset_id, exc_info=True)
    return None


async def smart_search(request: Request) -> JSONResponse:
    body = await request.json()
    query = body.get("query", "")
    page = body.get("page", 1)
    size = min(body.get("size", 25), 100)

    if not query:
        return JSONResponse({"assets": {"items": [], "count": 0, "total": 0, "facets": [], "nextPage": None}, "albums": _EMPTY_ALBUMS})

    log.info("SEARCH query=%r page=%d size=%d", query, page, size)

    # Call PIF — prefix with "photos:" so PIF knows to prioritise immich
    pif_query = f"photos: {query}"

    # Call PIF — search all services, filter to photo results after
    try:
        pif_resp = await _pif.post(
            f"{PIF_URL}/api/v1/search",
            json={
                "query": pif_query,
                "synthesize": False,
                "limit": size * 3,  # over-fetch since we filter non-photo results
            },
        )
        pif_resp.raise_for_status()
        pif_data = pif_resp.json()
    except Exception:
        log.exception("PIF search failed")
        return JSONResponse({"assets": {"items": [], "count": 0, "total": 0, "facets": [], "nextPage": None}, "albums": _EMPTY_ALBUMS})

    all_results = pif_data.get("results", [])

    # Keep only immich results (have asset IDs we can fetch)
    results = [r for r in all_results if r.get("service_id") == "immich" and r.get("result_id")]

    if not results:
        log.info("SEARCH query=%r -> 0 photo results (%d total from PIF)", query, len(all_results))
        return JSONResponse({"assets": {"items": [], "count": 0, "total": 0, "facets": [], "nextPage": None}, "albums": _EMPTY_ALBUMS})

    # Trim to requested page size
    results = results[:size]
    asset_ids = [r["result_id"] for r in results]

    # Fetch full asset objects from Immich in parallel
    tasks = [_fetch_asset(aid) for aid in asset_ids]
    fetched = await asyncio.gather(*tasks)

    # Preserve PIF ranking order, skip missing assets
    id_to_asset = {}
    for asset in fetched:
        if asset:
            id_to_asset[asset["id"]] = asset
    items = [id_to_asset[aid] for aid in asset_ids if aid in id_to_asset]

    total = len(items)
    next_page = None  # PIF doesn't paginate, so single page

    log.info("SEARCH query=%r -> %d photo results (%d total from PIF)", query, len(items), len(all_results))
    return JSONResponse({
        "assets": {
            "items": items,
            "count": len(items),
            "total": total,
            "facets": [],
            "nextPage": next_page,
        },
        "albums": _EMPTY_ALBUMS,
    })


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


app = Starlette(
    routes=[
        Route("/api/search/smart", smart_search, methods=["POST"]),
        Route("/health", health),
    ],
    lifespan=_lifespan,
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=LISTEN_PORT)

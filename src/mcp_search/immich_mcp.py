"""MCP server for read-only Immich photo library access."""

import base64
import json
import os

import anthropic
import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.server.lifespan import lifespan

_IMMICH_URL = os.environ.get("IMMICH_URL", "http://immich-server:2283")
_IMMICH_KEY = os.environ["IMMICH_API_KEY"]
_MEILI_URL = os.environ.get("MEILISEARCH_URL", "http://meilisearch-docs:7700")
_MEILI_KEY = os.environ.get("MEILISEARCH_KEY", "")
_MEILI_INDEX = "immich_photos"
_ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


@lifespan
async def immich_lifespan(server):
    client = httpx.AsyncClient(
        headers={"x-api-key": _IMMICH_KEY},
        timeout=30.0,
    )
    meili = httpx.AsyncClient(
        headers={"Authorization": f"Bearer {_MEILI_KEY}"},
        timeout=15.0,
    )
    claude = anthropic.Anthropic(api_key=_ANTHROPIC_KEY) if _ANTHROPIC_KEY else None
    yield {"client": client, "meili": meili, "claude": claude}
    await client.aclose()
    await meili.aclose()


mcp = FastMCP("immich", lifespan=immich_lifespan)


def _client() -> httpx.AsyncClient:
    return get_context().lifespan_context["client"]


def _meili() -> httpx.AsyncClient:
    return get_context().lifespan_context["meili"]


def _claude() -> anthropic.Anthropic | None:
    return get_context().lifespan_context["claude"]


def _format_table(rows: list[dict], keys: list[str]) -> str:
    if not rows:
        return "No results."
    str_rows = [[str(row.get(k, "")) for k in keys] for row in rows]
    widths = [max(len(k), *(len(r[i]) for r in str_rows)) for i, k in enumerate(keys)]
    header = " | ".join(k.ljust(w) for k, w in zip(keys, widths))
    separator = "-+-".join("-" * w for w in widths)
    data_lines = [" | ".join(v.ljust(w) for v, w in zip(row, widths)) for row in str_rows]
    return "\n".join([header, separator, *data_lines, f"\n({len(rows)} rows)"])


def _format_asset(a: dict) -> dict:
    info = a.get("exifInfo") or {}
    taken = a.get("localDateTime") or a.get("createdAt") or ""
    if taken and len(taken) > 19:
        taken = taken[:19].replace("T", " ")
    city = info.get("city") or ""
    country = info.get("country") or ""
    location = ", ".join(filter(None, [city, country])) or "—"
    w = info.get("exifImageWidth") or ""
    h = info.get("exifImageHeight") or ""
    dims = f"{w}x{h}" if w and h else "—"
    return {
        "filename": a.get("originalFileName") or "—",
        "date": taken,
        "dims": dims,
        "location": location,
        "type": a.get("type") or "—",
        "id": a.get("id") or "",
    }


def _format_assets(assets: list[dict], header: str) -> str:
    rows = [_format_asset(a) for a in assets]
    table = _format_table(rows, ["filename", "date", "dims", "location", "type"])
    ids = [r["id"] for r in rows if r["id"]]
    footer = f"\nAsset IDs: {', '.join(ids)}" if ids else ""
    return header + table + footer


def _format_index_hit(hit: dict) -> dict:
    """Format a Meilisearch immich_photos hit for display."""
    taken = (hit.get("taken_at") or "")[:19].replace("T", " ")
    city = hit.get("city") or ""
    country = hit.get("country") or ""
    location = ", ".join(filter(None, [city, country])) or "—"
    people = hit.get("people") or []
    return {
        "filename": hit.get("original_filename") or "—",
        "date": taken,
        "location": location,
        "people": ", ".join(people) if people else "—",
        "scene": hit.get("scene_type") or "—",
        "id": hit.get("asset_id") or "",
    }


def _format_index_results(hits: list[dict], header: str) -> str:
    """Format Meilisearch results into a table."""
    rows = [_format_index_hit(h) for h in hits]
    table = _format_table(rows, ["filename", "date", "location", "people", "scene"])
    ids = [r["id"] for r in rows if r["id"]]
    footer = f"\nAsset IDs: {', '.join(ids)}" if ids else ""

    # Add descriptions for context
    descs = []
    for h in hits:
        desc = h.get("description")
        if desc:
            fname = h.get("original_filename") or h.get("asset_id", "")[:8]
            descs.append(f"  {fname}: {desc}")
    desc_block = "\n\nDescriptions:\n" + "\n".join(descs) if descs else ""

    return header + table + footer + desc_block


@mcp.tool
async def immich_search(
    query: str,
    limit: int = 25,
) -> str:
    """Search photos using natural language across Claude Vision descriptions, people, tags, locations, and activities.

    Finds photos matching queries like "sunset on the beach", "Fran skiing",
    "receipts from New York", "dog in the garden", or "golden hour mountains".

    Args:
        query: Natural language search text
        limit: Max results (default 25, max 100)
    """
    meili = _meili()
    resp = await meili.post(
        f"{_MEILI_URL}/indexes/{_MEILI_INDEX}/search",
        json={
            "q": query,
            "limit": min(limit, 100),
            "attributesToSearchOn": [
                "description", "people", "albums", "user_tags",
                "activities", "visual_tags", "city", "country", "text_content",
            ],
        },
    )
    resp.raise_for_status()
    data = resp.json()
    hits = data.get("hits", [])
    total = data.get("estimatedTotalHits", len(hits))

    if not hits:
        return f"No photos found matching '{query}'."

    header = f"Search for '{query}' ({len(hits)} of ~{total} results):\n\n"
    return _format_index_results(hits, header)


@mcp.tool
async def immich_smart_search(
    query: str,
    limit: int = 25,
) -> str:
    """Smart photo search — parses complex natural language into structured filters.

    Use for queries that combine people, activities, locations, dates, or counts.
    Examples: "all of us at dinner", "photos without people", "ski touring in Lyngen",
    "Fran on the ski slopes", "golden hour in the mountains".

    Falls back to basic search if Claude is unavailable.

    Args:
        query: Natural language search query
        limit: Max results (default 25, max 100)
    """
    claude = _claude()
    if not claude:
        # Fall back to basic search
        return await immich_search(query=query, limit=limit)

    # Ask Claude to extract structured filters
    try:
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": f"""Convert this photo search query into structured filters. Return ONLY valid JSON, no preamble:
Query: "{query}"
Return: {{"people": [], "activities": [], "scene_type": null, "people_count_min": null, "people_count_max": null, "season": null, "mood": null, "city": null, "country": null, "is_video": null, "camera": null, "text_search": "remaining natural language terms"}}
Only include fields that are clearly implied by the query. Set unused fields to null or empty array."""}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)
        filters = json.loads(text)
    except Exception:
        # Fall back to basic search on any parsing failure
        return await immich_search(query=query, limit=limit)

    # Build Meilisearch filter string
    filter_parts = []
    for field in ("people", "activities"):
        for val in filters.get(field) or []:
            filter_parts.append(f'{field} = "{val}"')
    for field in ("scene_type", "season", "mood", "city", "country", "camera"):
        val = filters.get(field)
        if val:
            meili_field = "season_hint" if field == "season" else field
            filter_parts.append(f'{meili_field} = "{val}"')
    if filters.get("is_video") is not None:
        filter_parts.append(f'is_video = {str(filters["is_video"]).lower()}')
    if filters.get("people_count_min") is not None:
        filter_parts.append(f'people_count >= {filters["people_count_min"]}')
    if filters.get("people_count_max") is not None:
        filter_parts.append(f'people_count <= {filters["people_count_max"]}')

    meili_filter = " AND ".join(filter_parts) if filter_parts else None
    text_query = filters.get("text_search", query) or ""

    meili = _meili()
    body: dict = {
        "q": text_query,
        "limit": min(limit, 100),
        "attributesToSearchOn": [
            "description", "people", "albums", "user_tags",
            "activities", "visual_tags", "city", "country", "text_content",
        ],
    }
    if meili_filter:
        body["filter"] = meili_filter

    resp = await meili.post(
        f"{_MEILI_URL}/indexes/{_MEILI_INDEX}/search",
        json=body,
    )
    resp.raise_for_status()
    data = resp.json()
    hits = data.get("hits", [])
    total = data.get("estimatedTotalHits", len(hits))

    if not hits:
        filter_info = f" (filters: {meili_filter})" if meili_filter else ""
        return f"No photos found matching '{query}'{filter_info}."

    filter_info = f"\nFilters applied: {meili_filter}" if meili_filter else ""
    header = f"Smart search for '{query}' ({len(hits)} of ~{total} results):{filter_info}\n\n"
    return _format_index_results(hits, header)


@mcp.tool
async def immich_search_metadata(
    original_file_name: str | None = None,
    city: str | None = None,
    state: str | None = None,
    country: str | None = None,
    make: str | None = None,
    model: str | None = None,
    taken_after: str | None = None,
    taken_before: str | None = None,
    type: str | None = None,
    page: int = 1,
    size: int = 25,
) -> str:
    """Search photos by metadata filters (date, location, camera, filename).

    Args:
        original_file_name: Filter by original filename (substring match)
        city: Filter by city name from EXIF GPS data
        state: Filter by state/region from EXIF GPS data
        country: Filter by country from EXIF GPS data
        make: Filter by camera manufacturer (e.g. "Apple", "Sony")
        model: Filter by camera model (e.g. "iPhone 15 Pro")
        taken_after: Only photos taken after this date (ISO format, e.g. "2024-01-01")
        taken_before: Only photos taken before this date (ISO format, e.g. "2024-12-31")
        type: Asset type — "IMAGE" or "VIDEO"
        page: Page number for pagination (default 1)
        size: Results per page (default 25, max 100)
    """
    field_map = {
        "original_file_name": "originalFileName",
        "city": "city",
        "state": "state",
        "country": "country",
        "make": "make",
        "model": "model",
        "taken_after": "takenAfter",
        "taken_before": "takenBefore",
        "type": "type",
    }
    params = locals()
    body: dict = {"page": page, "size": min(size, 100)}
    for param, api_field in field_map.items():
        val = params[param]
        if val is not None:
            body[api_field] = val

    client = _client()
    resp = await client.post(
        f"{_IMMICH_URL}/api/search/metadata",
        json=body,
    )
    resp.raise_for_status()
    data = resp.json()

    items = data.get("assets", {}).get("items", [])
    total = data.get("assets", {}).get("total", 0)
    count = len(items)

    if not items:
        return "No photos found matching the given filters."

    header = f"Metadata search (page {page}, {count} of {total}):\n\n"
    return _format_assets(items, header)


@mcp.tool
async def immich_albums(
    album_id: str | None = None,
) -> str:
    """List all albums or get details of a specific album.

    Args:
        album_id: Optional album UUID. Omit to list all albums; provide to get album details with assets.
    """
    client = _client()

    if album_id:
        resp = await client.get(f"{_IMMICH_URL}/api/albums/{album_id}")
        resp.raise_for_status()
        album = resp.json()

        created = (album.get("createdAt") or "")[:19].replace("T", " ")
        updated = (album.get("updatedAt") or "")[:19].replace("T", " ")
        header = (
            f"# {album.get('albumName', '—')}\n\n"
            f"**Assets:** {album.get('assetCount', 0)} | "
            f"**Created:** {created} | **Updated:** {updated}\n\n"
        )

        assets = album.get("assets", [])
        if not assets:
            return header + "No assets in this album."
        return _format_assets(assets, header)

    # List all albums
    resp = await client.get(f"{_IMMICH_URL}/api/albums")
    resp.raise_for_status()
    albums = resp.json()

    if not albums:
        return "No albums found."

    rows = []
    for a in albums:
        created = (a.get("createdAt") or "")[:19].replace("T", " ")
        updated = (a.get("updatedAt") or "")[:19].replace("T", " ")
        rows.append({
            "name": a.get("albumName") or "—",
            "assets": str(a.get("assetCount", 0)),
            "created": created,
            "updated": updated,
            "id": a.get("id") or "",
        })

    table = _format_table(rows, ["name", "assets", "created", "updated"])
    ids = "\n".join(f"  {r['name']}: {r['id']}" for r in rows if r["id"])
    return f"Albums ({len(rows)}):\n\n{table}\n\nAlbum IDs:\n{ids}"


@mcp.tool
async def immich_faces(
    person_id: str | None = None,
    name: str | None = None,
    page: int = 1,
    size: int = 25,
) -> str:
    """List recognized people or find photos of a specific person.

    Without arguments, lists all named people. With a person_id, finds their
    photos. With a name, searches for matching people.

    Args:
        person_id: Person UUID — returns their photos
        name: Search people by name (case-insensitive substring match)
        page: Page number when fetching a person's photos (default 1)
        size: Results per page for person's photos (default 25, max 100)
    """
    client = _client()

    if person_id:
        # Get person info + their assets via metadata search
        resp = await client.get(f"{_IMMICH_URL}/api/people/{person_id}")
        resp.raise_for_status()
        person = resp.json()

        resp = await client.post(
            f"{_IMMICH_URL}/api/search/metadata",
            json={"personIds": [person_id], "page": page, "size": min(size, 100)},
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("assets", {}).get("items", [])
        total = data.get("assets", {}).get("total", 0)

        pname = person.get("name") or "Unnamed"
        header = f"Photos of {pname} (page {page}, {len(items)} of {total}):\n\n"
        if not items:
            return header + "No photos found."
        return _format_assets(items, header)

    # List all people
    resp = await client.get(f"{_IMMICH_URL}/api/people?withHidden=false")
    resp.raise_for_status()
    data = resp.json()
    people = data.get("people", data) if isinstance(data, dict) else data

    if name:
        needle = name.lower()
        people = [p for p in people if needle in (p.get("name") or "").lower()]

    if not people:
        return "No matching people found."

    rows = []
    for p in people:
        pname = p.get("name") or "Unnamed"
        updated = (p.get("updatedAt") or "")[:19].replace("T", " ")
        rows.append({
            "name": pname,
            "hidden": "yes" if p.get("isHidden") else "",
            "updated": updated,
            "id": p.get("id") or "",
        })

    table = _format_table(rows, ["name", "hidden", "updated", "id"])
    return f"People ({len(rows)}):\n\n{table}"


@mcp.tool
async def immich_asset_info(
    asset_id: str,
) -> str:
    """Get detailed EXIF and metadata for a specific asset.

    Returns camera settings, GPS coordinates, file details, recognized faces,
    and all available EXIF data.

    Args:
        asset_id: Asset UUID (from search results or album listings)
    """
    client = _client()
    resp = await client.get(f"{_IMMICH_URL}/api/assets/{asset_id}")
    resp.raise_for_status()
    a = resp.json()

    exif = a.get("exifInfo") or {}
    lines = [f"# {a.get('originalFileName', '—')}"]
    lines.append("")

    # Basic info
    lines.append(f"**Type:** {a.get('type', '—')}")
    taken = (a.get("localDateTime") or a.get("fileCreatedAt") or "")[:19].replace("T", " ")
    if taken:
        lines.append(f"**Taken:** {taken}")
    if a.get("duration") and a.get("duration") != "00:00:00.000":
        lines.append(f"**Duration:** {a['duration']}")
    mime = a.get("originalMimeType") or "—"
    size_bytes = exif.get("fileSizeInByte")
    size_str = f"{size_bytes / 1048576:.1f} MB" if size_bytes else "—"
    lines.append(f"**File:** {mime}, {size_str}")
    w = exif.get("exifImageWidth") or a.get("width")
    h = exif.get("exifImageHeight") or a.get("height")
    if w and h:
        lines.append(f"**Dimensions:** {w} x {h}")
    lines.append("")

    # Camera / EXIF
    camera_fields = []
    if exif.get("make"):
        camera_fields.append(f"**Make:** {exif['make']}")
    if exif.get("model"):
        camera_fields.append(f"**Model:** {exif['model']}")
    if exif.get("lensModel"):
        camera_fields.append(f"**Lens:** {exif['lensModel']}")
    if exif.get("fNumber"):
        camera_fields.append(f"**Aperture:** f/{exif['fNumber']}")
    if exif.get("exposureTime"):
        camera_fields.append(f"**Exposure:** {exif['exposureTime']}s")
    if exif.get("iso"):
        camera_fields.append(f"**ISO:** {exif['iso']}")
    if exif.get("focalLength"):
        camera_fields.append(f"**Focal Length:** {exif['focalLength']}mm")
    if exif.get("orientation"):
        camera_fields.append(f"**Orientation:** {exif['orientation']}")
    if camera_fields:
        lines.append("## Camera")
        lines.extend(camera_fields)
        lines.append("")

    # Location
    loc_fields = []
    if exif.get("city"):
        loc_fields.append(f"**City:** {exif['city']}")
    if exif.get("state"):
        loc_fields.append(f"**State:** {exif['state']}")
    if exif.get("country"):
        loc_fields.append(f"**Country:** {exif['country']}")
    if exif.get("latitude") is not None and exif.get("longitude") is not None:
        loc_fields.append(f"**GPS:** {exif['latitude']}, {exif['longitude']}")
    if exif.get("timeZone"):
        loc_fields.append(f"**Timezone:** {exif['timeZone']}")
    if loc_fields:
        lines.append("## Location")
        lines.extend(loc_fields)
        lines.append("")

    # People / faces
    people = a.get("people") or []
    unassigned = a.get("unassignedFaces") or []
    if people or unassigned:
        lines.append("## Faces")
        for p in people:
            pname = p.get("name") or "Unnamed"
            lines.append(f"- {pname} ({p.get('id', '')})")
        if unassigned:
            lines.append(f"- {len(unassigned)} unassigned face(s)")
        lines.append("")

    # Tags
    tags = a.get("tags") or []
    if tags:
        tag_names = [t.get("name") or t.get("value") or str(t) for t in tags]
        lines.append(f"**Tags:** {', '.join(tag_names)}")
        lines.append("")

    # Description
    if exif.get("description"):
        lines.append(f"**Description:** {exif['description']}")
        lines.append("")

    if exif.get("rating"):
        lines.append(f"**Rating:** {exif['rating']}")

    lines.append(f"**Asset ID:** {a.get('id', '')}")

    return "\n".join(lines)


@mcp.tool
async def immich_thumbnail(
    asset_id: str,
    size: str = "thumbnail",
) -> dict:
    """Get a thumbnail image for an asset as base64.

    Args:
        asset_id: Asset UUID (from search results or album listings)
        size: Thumbnail size — "thumbnail" (small) or "preview" (larger)
    """
    client = _client()
    resp = await client.get(
        f"{_IMMICH_URL}/api/assets/{asset_id}/thumbnail",
        params={"size": size},
    )
    resp.raise_for_status()
    mime = resp.headers.get("content-type", "image/jpeg")
    return {"base64": base64.b64encode(resp.content).decode(), "mimeType": mime}


if __name__ == "__main__":
    from mcp_search.run import serve
    serve(mcp)

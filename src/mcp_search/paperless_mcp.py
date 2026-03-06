"""MCP server for read-only Paperless-ngx document access."""

import os

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.server.lifespan import lifespan

_PAPERLESS_URL = os.environ.get("PAPERLESS_URL", "http://paperless:8000")
_PAPERLESS_TOKEN = os.environ.get("PAPERLESS_TOKEN", "")


async def _fetch_all_pages(client: httpx.AsyncClient, path: str) -> list[dict]:
    """Fetch all pages from a paginated Paperless API endpoint."""
    results = []
    url = f"{_PAPERLESS_URL}{path}?page_size=100"
    while url:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("results", []))
        url = data.get("next")
    return results


@lifespan
async def paperless_lifespan(server):
    client = httpx.AsyncClient(
        headers={"Authorization": f"Token {_PAPERLESS_TOKEN}"},
        timeout=30.0,
    )

    # Cache name→ID mappings at startup
    tags = await _fetch_all_pages(client, "/api/tags/")
    doc_types = await _fetch_all_pages(client, "/api/document_types/")
    correspondents = await _fetch_all_pages(client, "/api/correspondents/")

    tag_map = {t["name"]: t["id"] for t in tags}
    type_map = {t["name"]: t["id"] for t in doc_types}
    corr_map = {c["name"]: c["id"] for c in correspondents}

    yield {
        "client": client,
        "tag_map": tag_map,
        "type_map": type_map,
        "corr_map": corr_map,
        "tags_by_id": {t["id"]: t["name"] for t in tags},
        "types_by_id": {t["id"]: t["name"] for t in doc_types},
        "corr_by_id": {c["id"]: c["name"] for c in correspondents},
    }

    await client.aclose()


mcp = FastMCP("paperless-search", lifespan=paperless_lifespan)


def _resolve_name(name: str, mapping: dict[str, int]) -> int | None:
    """Case-insensitive substring match to resolve name→ID."""
    lower = name.lower()
    for k, v in mapping.items():
        if lower in k.lower():
            return v
    return None


def _lc(ctx):
    return ctx.lifespan_context


@mcp.tool
async def paperless_search(
    query: str | None = None,
    tag: str | None = None,
    document_type: str | None = None,
    correspondent: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> str:
    """Search Paperless-ngx documents with optional filters.

    Args:
        query: Full-text search query
        tag: Filter by tag name (case-insensitive substring match)
        document_type: Filter by document type name
        correspondent: Filter by correspondent name
        page: Page number (default 1)
        page_size: Results per page (default 25, max 100)
    """
    ctx = get_context()
    lc = _lc(ctx)
    client: httpx.AsyncClient = lc["client"]

    params: dict = {"page": page, "page_size": min(page_size, 100)}

    if query:
        params["query"] = query
    if tag:
        tag_id = _resolve_name(tag, lc["tag_map"])
        if tag_id is None:
            return f"Tag '{tag}' not found."
        params["tags__id"] = tag_id
    if document_type:
        type_id = _resolve_name(document_type, lc["type_map"])
        if type_id is None:
            return f"Document type '{document_type}' not found."
        params["document_type__id"] = type_id
    if correspondent:
        corr_id = _resolve_name(correspondent, lc["corr_map"])
        if corr_id is None:
            return f"Correspondent '{correspondent}' not found."
        params["correspondent__id"] = corr_id

    resp = await client.get(f"{_PAPERLESS_URL}/api/documents/", params=params)
    resp.raise_for_status()
    data = resp.json()

    results = data.get("results", [])
    total = data.get("count", 0)

    if not results:
        return "No documents found."

    lines = [f"Found {total} documents (showing page {page}):\n"]
    for doc in results:
        tags_str = ", ".join(
            lc["tags_by_id"].get(tid, str(tid)) for tid in doc.get("tags", [])
        )
        dtype = lc["types_by_id"].get(doc.get("document_type"), "—")
        corr = lc["corr_by_id"].get(doc.get("correspondent"), "—")
        lines.append(
            f"  [{doc['id']}] {doc['title']}\n"
            f"       Date: {doc.get('created', '—')} | Type: {dtype} | Correspondent: {corr}\n"
            f"       Tags: {tags_str or '—'}"
        )

    return "\n".join(lines)


@mcp.tool
async def paperless_get_document(document_id: int) -> str:
    """Get full details and content of a specific document.

    Args:
        document_id: The document ID
    """
    ctx = get_context()
    lc = _lc(ctx)
    client: httpx.AsyncClient = lc["client"]

    resp = await client.get(f"{_PAPERLESS_URL}/api/documents/{document_id}/")
    resp.raise_for_status()
    doc = resp.json()

    tags_str = ", ".join(
        lc["tags_by_id"].get(tid, str(tid)) for tid in doc.get("tags", [])
    )
    dtype = lc["types_by_id"].get(doc.get("document_type"), "—")
    corr = lc["corr_by_id"].get(doc.get("correspondent"), "—")

    content = doc.get("content", "")
    if len(content) > 50000:
        content = content[:50000] + "\n... [truncated]"

    return (
        f"# {doc['title']}\n\n"
        f"**ID:** {doc['id']}\n"
        f"**Created:** {doc.get('created', '—')}\n"
        f"**Added:** {doc.get('added', '—')}\n"
        f"**Correspondent:** {corr}\n"
        f"**Document Type:** {dtype}\n"
        f"**Tags:** {tags_str or '—'}\n"
        f"**Archive Serial Number:** {doc.get('archive_serial_number', '—')}\n\n"
        f"## Content\n\n{content}"
    )


@mcp.tool
async def paperless_list_tags() -> str:
    """List all available tags with document counts."""
    ctx = get_context()
    lc = _lc(ctx)
    client: httpx.AsyncClient = lc["client"]

    tags = await _fetch_all_pages(client, "/api/tags/")
    tags.sort(key=lambda t: t["name"].lower())

    lines = [f"Tags ({len(tags)} total):\n"]
    for t in tags:
        lines.append(f"  [{t['id']}] {t['name']} ({t.get('document_count', 0)} docs)")

    return "\n".join(lines)


@mcp.tool
async def paperless_list_document_types() -> str:
    """List all available document types with document counts."""
    ctx = get_context()
    lc = _lc(ctx)
    client: httpx.AsyncClient = lc["client"]

    types = await _fetch_all_pages(client, "/api/document_types/")
    types.sort(key=lambda t: t["name"].lower())

    lines = [f"Document Types ({len(types)} total):\n"]
    for t in types:
        lines.append(f"  [{t['id']}] {t['name']} ({t.get('document_count', 0)} docs)")

    return "\n".join(lines)


@mcp.tool
async def paperless_list_correspondents() -> str:
    """List all available correspondents with document counts."""
    ctx = get_context()
    lc = _lc(ctx)
    client: httpx.AsyncClient = lc["client"]

    corrs = await _fetch_all_pages(client, "/api/correspondents/")
    corrs.sort(key=lambda c: c["name"].lower())

    lines = [f"Correspondents ({len(corrs)} total):\n"]
    for c in corrs:
        lines.append(f"  [{c['id']}] {c['name']} ({c.get('document_count', 0)} docs)")

    return "\n".join(lines)


@mcp.tool
async def paperless_get_suggestions(document_id: int) -> str:
    """Get auto-classification suggestions for a document.

    Args:
        document_id: The document ID
    """
    ctx = get_context()
    lc = _lc(ctx)
    client: httpx.AsyncClient = lc["client"]

    resp = await client.get(
        f"{_PAPERLESS_URL}/api/documents/{document_id}/suggestions/"
    )
    resp.raise_for_status()
    data = resp.json()

    lines = [f"Suggestions for document {document_id}:\n"]

    if data.get("correspondents"):
        lines.append("  Correspondents:")
        for s in data["correspondents"]:
            name = lc["corr_by_id"].get(s["id"], str(s["id"]))
            lines.append(f"    - {name}")

    if data.get("tags"):
        lines.append("  Tags:")
        for s in data["tags"]:
            name = lc["tags_by_id"].get(s["id"], str(s["id"]))
            lines.append(f"    - {name}")

    if data.get("document_types"):
        lines.append("  Document Types:")
        for s in data["document_types"]:
            name = lc["types_by_id"].get(s["id"], str(s["id"]))
            lines.append(f"    - {name}")

    if data.get("dates"):
        lines.append("  Dates:")
        for d in data["dates"]:
            lines.append(f"    - {d}")

    return "\n".join(lines)


@mcp.tool
async def paperless_download_url(document_id: int, original: bool = False) -> str:
    """Get the download URL for a document.

    Args:
        document_id: The document ID
        original: If True, return URL for the original file; otherwise the archived version
    """
    variant = "original" if original else "download"
    return f"{_PAPERLESS_URL}/api/documents/{document_id}/{variant}/"


if __name__ == "__main__":
    mcp.run()

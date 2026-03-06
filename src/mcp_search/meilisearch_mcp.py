"""MCP server for Meilisearch document search (keyword + hybrid/semantic)."""

import os

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.server.lifespan import lifespan

_MEILI_URL = os.environ.get("MEILISEARCH_URL", "http://meilisearch-docs:7700")
_MEILI_KEY = os.environ.get("MEILISEARCH_KEY", "")


@lifespan
async def meili_lifespan(server):
    client = httpx.AsyncClient(
        headers={"Authorization": f"Bearer {_MEILI_KEY}"},
        timeout=30.0,
    )
    yield {"client": client}
    await client.aclose()


mcp = FastMCP("meilisearch-search", lifespan=meili_lifespan)


def _client() -> httpx.AsyncClient:
    return get_context().lifespan_context["client"]


@mcp.tool
async def ms_list_indexes() -> str:
    """List all Meilisearch indexes with document counts and settings summary."""
    client = _client()
    resp = await client.get(f"{_MEILI_URL}/indexes")
    resp.raise_for_status()
    data = resp.json()

    indexes = data.get("results", data) if isinstance(data, dict) else data

    if not indexes:
        return "No indexes found."

    lines = ["Meilisearch Indexes:\n"]
    for idx in indexes:
        lines.append(
            f"  [{idx['uid']}] {idx.get('numberOfDocuments', '?')} documents, "
            f"primary key: {idx.get('primaryKey', '—')}, "
            f"created: {idx.get('createdAt', '—')}"
        )

    return "\n".join(lines)


@mcp.tool
async def ms_search(
    index: str,
    query: str,
    filter: str | None = None,
    sort: list[str] | None = None,
    limit: int = 20,
    offset: int = 0,
    hybrid: bool = True,
    semantic_ratio: float = 0.3,
    attributes_to_retrieve: list[str] | None = None,
) -> str:
    """Search a Meilisearch index with hybrid (keyword + semantic) search.

    Args:
        index: Index name (e.g. 'documents')
        query: Search query text
        filter: Optional Meilisearch filter expression (e.g. "created_year = 2024")
        sort: Optional list of sort expressions (e.g. ["created:desc"])
        limit: Max results to return (default 20)
        offset: Offset for pagination (default 0)
        hybrid: Enable hybrid search with semantic embeddings (default True)
        semantic_ratio: Ratio of semantic vs keyword results, 0-1 (default 0.3)
        attributes_to_retrieve: Optional list of fields to include in results
    """
    client = _client()

    body: dict = {
        "q": query,
        "limit": min(limit, 100),
        "offset": offset,
        "showRankingScore": True,
        "attributesToCrop": ["content"],
        "cropLength": 200,
        "attributesToHighlight": ["title", "content"],
    }

    if filter:
        body["filter"] = filter
    if sort:
        body["sort"] = sort
    if attributes_to_retrieve:
        body["attributesToRetrieve"] = attributes_to_retrieve

    if hybrid:
        body["hybrid"] = {"embedder": "openai", "semanticRatio": semantic_ratio}

    resp = await client.post(f"{_MEILI_URL}/indexes/{index}/search", json=body)

    # Fall back to keyword-only if hybrid fails (e.g. embedder not configured)
    if resp.status_code == 400 and hybrid:
        body.pop("hybrid", None)
        resp = await client.post(f"{_MEILI_URL}/indexes/{index}/search", json=body)

    resp.raise_for_status()
    data = resp.json()

    hits = data.get("hits", [])
    total = data.get("estimatedTotalHits", len(hits))

    if not hits:
        return "No results found."

    lines = [f"Found ~{total} results (showing {len(hits)}):\n"]
    for hit in hits:
        formatted = hit.get("_formatted", hit)
        score = hit.get("_rankingScore", "—")
        title = formatted.get("title", hit.get("title", "Untitled"))
        content_crop = formatted.get("content", "")
        if isinstance(content_crop, dict):
            content_crop = str(content_crop)
        # Truncate content preview
        if len(content_crop) > 300:
            content_crop = content_crop[:300] + "..."

        meta_parts = []
        if "correspondent_name" in hit:
            meta_parts.append(f"From: {hit['correspondent_name']}")
        if "document_type_name" in hit:
            meta_parts.append(f"Type: {hit['document_type_name']}")
        if "created" in hit:
            meta_parts.append(f"Date: {hit['created']}")
        if "tag_names" in hit and hit["tag_names"]:
            meta_parts.append(f"Tags: {', '.join(hit['tag_names'])}")

        meta = " | ".join(meta_parts)

        lines.append(
            f"  [{hit.get('id', '?')}] {title} (score: {score})\n"
            f"       {meta}\n"
            f"       {content_crop}\n"
        )

    return "\n".join(lines)


@mcp.tool
async def ms_get_document(index: str, document_id: str) -> str:
    """Get a specific document from a Meilisearch index.

    Args:
        index: Index name
        document_id: Document ID
    """
    client = _client()
    resp = await client.get(f"{_MEILI_URL}/indexes/{index}/documents/{document_id}")
    resp.raise_for_status()
    doc = resp.json()

    lines = [f"# {doc.get('title', 'Untitled')}\n"]

    for key in ["id", "correspondent_name", "document_type_name", "created", "added", "tag_names"]:
        if key in doc:
            val = doc[key]
            if isinstance(val, list):
                val = ", ".join(str(v) for v in val)
            lines.append(f"**{key}:** {val}")

    content = doc.get("content", "")
    if content:
        if len(content) > 50000:
            content = content[:50000] + "\n... [truncated]"
        lines.append(f"\n## Content\n\n{content}")

    return "\n".join(lines)


@mcp.tool
async def ms_index_settings(index: str) -> str:
    """Get the settings of a Meilisearch index.

    Args:
        index: Index name
    """
    client = _client()
    resp = await client.get(f"{_MEILI_URL}/indexes/{index}/settings")
    resp.raise_for_status()
    settings = resp.json()

    lines = [f"Settings for index '{index}':\n"]

    for key, val in sorted(settings.items()):
        if isinstance(val, (list, dict)):
            if isinstance(val, list) and len(val) <= 10:
                lines.append(f"  {key}: {val}")
            elif isinstance(val, dict):
                lines.append(f"  {key}:")
                for k2, v2 in val.items():
                    preview = str(v2)
                    if len(preview) > 100:
                        preview = preview[:100] + "..."
                    lines.append(f"    {k2}: {preview}")
            else:
                lines.append(f"  {key}: [{len(val)} items]")
        else:
            lines.append(f"  {key}: {val}")

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()

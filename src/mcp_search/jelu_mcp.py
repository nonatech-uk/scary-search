"""MCP server for read-only Jelu book library and reading tracker access."""

import os

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.server.lifespan import lifespan

_JELU_URL = os.environ.get("JELU_URL", "http://jelu:11111")
_JELU_USER = os.environ.get("JELU_USER", "stu")


@lifespan
async def jelu_lifespan(server):
    client = httpx.AsyncClient(
        headers={"Remote-User": _JELU_USER},
        timeout=15.0,
    )
    yield {"client": client}
    await client.aclose()


mcp = FastMCP("jelu", lifespan=jelu_lifespan)


def _client() -> httpx.AsyncClient:
    return get_context().lifespan_context["client"]


async def _get(path: str, params: dict | None = None) -> dict | list:
    resp = await _client().get(f"{_JELU_URL}{path}", params=params)
    resp.raise_for_status()
    return resp.json()


def _fmt_book(b: dict, detailed: bool = False) -> str:
    """Format a book from Jelu API response."""
    authors = ", ".join(a.get("name", "") for a in b.get("authors", []))
    tags = ", ".join(t.get("name", "") for t in b.get("tags", []))
    lines = [f"  [{b.get('id', '')[:8]}] {b.get('title', 'Untitled')}"]
    if authors:
        lines.append(f"      Author: {authors}")

    parts = []
    if b.get("publisher"):
        parts.append(b["publisher"])
    pub_date = b.get("publishedDate", "")
    if pub_date:
        parts.append(pub_date[:4])
    if parts:
        lines.append(f"      {' | '.join(parts)}")

    if tags and detailed:
        lines.append(f"      Tags: {tags}")

    # Reading status from nested userbook
    ub = b.get("userbook") or {}
    if ub:
        events = ub.get("readingEvents", [])
        if events:
            last = events[-1]
            lines.append(f"      Status: {last.get('eventType', 'unknown')}")

    return "\n".join(lines)


@mcp.tool()
async def jelu_search(
    query: str | None = None,
    tag: str | None = None,
    author: str | None = None,
    page: int = 0,
    limit: int = 20,
) -> str:
    """Search books in the Jelu library.

    Args:
        query: Search query (matches title, ISBN, etc.).
        tag: Filter by tag name.
        author: Filter by author name.
        page: Page number, 0-indexed (default 0).
        limit: Results per page (default 20, max 100).
    """
    limit = min(limit, 100)
    params: dict = {"page": page, "size": limit}
    if query:
        params["q"] = query
    if tag:
        params["tag"] = tag
    if author:
        params["author"] = author

    data = await _get("/api/v1/books", params=params)
    content = data.get("content", [])
    total = data.get("totalElements", 0)

    if not content:
        return "No books found."

    start = page * limit
    lines = [f"Books (showing {start + 1}-{start + len(content)} of {total}):\n"]
    for b in content:
        lines.append(_fmt_book(b))

    return "\n".join(lines)


@mcp.tool()
async def jelu_book_detail(book_id: str) -> str:
    """Get full details for a book by its Jelu UUID.

    Args:
        book_id: The Jelu book UUID.
    """
    b = await _get(f"/api/v1/books/{book_id}")

    authors = ", ".join(a.get("name", "") for a in b.get("authors", []))
    tags = ", ".join(t.get("name", "") for t in b.get("tags", []))
    series_list = b.get("series", [])

    lines = [f"# {b.get('title', 'Untitled')}\n"]
    lines.append(f"**Author:** {authors or 'Unknown'}")

    if b.get("publisher"):
        lines.append(f"**Publisher:** {b['publisher']}")

    pub_date = b.get("publishedDate", "")
    if pub_date:
        lines.append(f"**Published:** {pub_date[:10]}")

    if b.get("isbn13"):
        lines.append(f"**ISBN-13:** {b['isbn13']}")
    if b.get("isbn10"):
        lines.append(f"**ISBN-10:** {b['isbn10']}")

    if b.get("pageCount"):
        lines.append(f"**Pages:** {b['pageCount']}")

    if series_list:
        series_strs = [s.get("name", "") for s in series_list]
        lines.append(f"**Series:** {', '.join(series_strs)}")

    if tags:
        lines.append(f"**Tags:** {tags}")

    translators = ", ".join(t.get("name", "") for t in b.get("translators", []))
    if translators:
        lines.append(f"**Translators:** {translators}")

    narrators = ", ".join(n.get("name", "") for n in b.get("narrators", []))
    if narrators:
        lines.append(f"**Narrators:** {narrators}")

    # Reading status
    ub = b.get("userbook") or {}
    if ub:
        events = ub.get("readingEvents", [])
        if events:
            last = events[-1]
            lines.append(f"\n**Reading Status:** {last.get('eventType', 'unknown')}")
            if last.get("startDate"):
                lines.append(f"**Started:** {last['startDate'][:10]}")
            if last.get("endDate"):
                lines.append(f"**Finished:** {last['endDate'][:10]}")
        if ub.get("personalNotes"):
            lines.append(f"**Notes:** {ub['personalNotes']}")
        if ub.get("owned"):
            lines.append("**Owned:** Yes")

    if b.get("summary"):
        lines.append(f"\n{b['summary'][:2000]}")

    return "\n".join(lines)


@mcp.tool()
async def jelu_authors(
    query: str | None = None,
    page: int = 0,
    limit: int = 20,
) -> str:
    """List or search authors in the Jelu library.

    Args:
        query: Search by author name (substring match).
        page: Page number, 0-indexed (default 0).
        limit: Results per page (default 20, max 100).
    """
    limit = min(limit, 100)
    params: dict = {"page": page, "size": limit}
    if query:
        params["name"] = query

    data = await _get("/api/v1/authors", params=params)
    content = data.get("content", [])
    total = data.get("totalElements", 0)

    if not content:
        return "No authors found."

    start = page * limit
    lines = [f"Authors (showing {start + 1}-{start + len(content)} of {total}):\n"]
    for a in content:
        parts = [f"  [{a.get('id', '')[:8]}] {a.get('name', 'Unknown')}"]
        if a.get("dateOfBirth"):
            parts.append(f" (b. {a['dateOfBirth'][:4]})")
        if a.get("biography"):
            bio = a["biography"][:100]
            parts.append(f"\n      {bio}{'...' if len(a['biography']) > 100 else ''}")
        lines.append("".join(parts))

    return "\n".join(lines)


@mcp.tool()
async def jelu_reading_events(
    page: int = 0,
    limit: int = 20,
) -> str:
    """Get reading history — books started, finished, or dropped.

    Args:
        page: Page number, 0-indexed (default 0).
        limit: Results per page (default 20, max 100).
    """
    limit = min(limit, 100)
    data = await _get("/api/v1/reading-events/me", params={"page": page, "size": limit})
    content = data.get("content", [])
    total = data.get("totalElements", 0)

    if not content:
        return "No reading events found."

    start = page * limit
    lines = [f"Reading events (showing {start + 1}-{start + len(content)} of {total}):\n"]
    for e in content:
        event_type = e.get("eventType", "unknown")
        ub = e.get("userBook", {})
        book = ub.get("book", {})
        title = book.get("title", "Unknown")
        authors = ", ".join(a.get("name", "") for a in book.get("authors", []))

        date_parts = []
        if e.get("startDate"):
            date_parts.append(f"started {e['startDate'][:10]}")
        if e.get("endDate"):
            date_parts.append(f"ended {e['endDate'][:10]}")
        date_str = f" ({', '.join(date_parts)})" if date_parts else ""

        lines.append(f"  {event_type}: {title}")
        if authors:
            lines.append(f"      by {authors}")
        if date_str:
            lines.append(f"      {date_str.strip()}")

    return "\n".join(lines)


@mcp.tool()
async def jelu_tags() -> str:
    """List all tags in the Jelu library."""
    # Paginated endpoint — fetch all pages
    all_tags: list[str] = []
    page = 0
    while True:
        data = await _get("/api/v1/tags", params={"page": page, "size": 100})
        for t in data.get("content", []):
            all_tags.append(t.get("name", ""))
        if data.get("last", True):
            break
        page += 1

    if not all_tags:
        return "No tags found."

    names = sorted(all_tags)
    return f"Tags ({len(names)}):\n" + ", ".join(names)


if __name__ == "__main__":
    from mcp_search.run import serve
    serve(mcp)

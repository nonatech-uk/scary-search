"""MCP server for read-only Calibre ebook library access via SQLite."""

import os
from html.parser import HTMLParser
from io import StringIO

import aiosqlite
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.server.lifespan import lifespan

_DB_PATH = os.environ.get("CALIBRE_DB_PATH", "/data/metadata.db")


class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._text = StringIO()

    def handle_data(self, data):
        self._text.write(data)

    def get_text(self):
        return self._text.getvalue().strip()


def _strip_html(html: str) -> str:
    s = _HTMLStripper()
    s.feed(html)
    return s.get_text()


@lifespan
async def calibre_lifespan(server):
    db = await aiosqlite.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
    db.row_factory = aiosqlite.Row
    yield {"db": db}
    await db.close()


mcp = FastMCP("calibre", lifespan=calibre_lifespan)


def _db() -> aiosqlite.Connection:
    return get_context().lifespan_context["db"]


@mcp.tool()
async def calibre_search(
    query: str | None = None,
    author: str | None = None,
    tag: str | None = None,
    publisher: str | None = None,
    series: str | None = None,
    language: str | None = None,
    format: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> str:
    """Search the Calibre ebook library.

    Args:
        query: Search title or author name (substring match).
        author: Filter by author name (substring match).
        tag: Filter by tag/category (substring match).
        publisher: Filter by publisher (substring match).
        series: Filter by series name (substring match).
        language: Filter by language code, e.g. 'eng'.
        format: Filter by file format, e.g. 'EPUB', 'PDF', 'MOBI'.
        limit: Max results (default 20, max 100).
        offset: Offset for pagination (default 0).
    """
    limit = min(limit, 100)
    conditions = []
    params: list = []

    if query:
        conditions.append("(b.title LIKE ? OR b.author_sort LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])
    if author:
        conditions.append(
            "b.id IN (SELECT bal.book FROM books_authors_link bal "
            "JOIN authors a ON a.id = bal.author WHERE a.name LIKE ?)"
        )
        params.append(f"%{author}%")
    if tag:
        conditions.append(
            "b.id IN (SELECT btl.book FROM books_tags_link btl "
            "JOIN tags t ON t.id = btl.tag WHERE t.name LIKE ?)"
        )
        params.append(f"%{tag}%")
    if publisher:
        conditions.append(
            "b.id IN (SELECT bpl.book FROM books_publishers_link bpl "
            "JOIN publishers p ON p.id = bpl.publisher WHERE p.name LIKE ?)"
        )
        params.append(f"%{publisher}%")
    if series:
        conditions.append(
            "b.id IN (SELECT bsl.book FROM books_series_link bsl "
            "JOIN series s ON s.id = bsl.series WHERE s.name LIKE ?)"
        )
        params.append(f"%{series}%")
    if language:
        conditions.append(
            "b.id IN (SELECT bll.book FROM books_languages_link bll "
            "JOIN languages l ON l.id = bll.lang_code WHERE l.lang_code LIKE ?)"
        )
        params.append(f"%{language}%")
    if format:
        conditions.append(
            "b.id IN (SELECT d.book FROM data d WHERE d.format LIKE ?)"
        )
        params.append(f"%{format}%")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    # Get total count
    count_sql = f"SELECT COUNT(*) FROM books b {where}"
    db = _db()
    async with db.execute(count_sql, params) as cur:
        total = (await cur.fetchone())[0]

    sql = f"""
        SELECT b.id, b.title, b.author_sort, b.pubdate,
               (SELECT GROUP_CONCAT(d.format, ', ')
                FROM data d WHERE d.book = b.id) AS formats
        FROM books b
        {where}
        ORDER BY b.sort
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])

    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()

    if not rows:
        return "No books found."

    lines = [f"Books (showing {offset + 1}-{offset + len(rows)} of {total}):\n"]
    for r in rows:
        pub_year = r["pubdate"][:4] if r["pubdate"] and r["pubdate"][:4] != "0101" else ""
        fmt = r["formats"] or ""
        lines.append(f"  [{r['id']}] {r['title']}")
        lines.append(f"      Author: {r['author_sort'] or 'Unknown'}")
        detail_parts = []
        if pub_year:
            detail_parts.append(pub_year)
        if fmt:
            detail_parts.append(fmt)
        if detail_parts:
            lines.append(f"      {' | '.join(detail_parts)}")

    return "\n".join(lines)


@mcp.tool()
async def calibre_book_detail(book_id: int) -> str:
    """Get full details for a book by its Calibre ID.

    Args:
        book_id: The Calibre book ID (integer).
    """
    db = _db()

    # Core book info
    async with db.execute(
        "SELECT * FROM books WHERE id = ?", [book_id]
    ) as cur:
        book = await cur.fetchone()
    if not book:
        return f"No book found with ID {book_id}."

    # Authors
    async with db.execute(
        "SELECT a.name FROM authors a "
        "JOIN books_authors_link bal ON bal.author = a.id "
        "WHERE bal.book = ?",
        [book_id],
    ) as cur:
        authors = [r["name"] for r in await cur.fetchall()]

    # Tags
    async with db.execute(
        "SELECT t.name FROM tags t "
        "JOIN books_tags_link btl ON btl.tag = t.id "
        "WHERE btl.book = ?",
        [book_id],
    ) as cur:
        tags = [r["name"] for r in await cur.fetchall()]

    # Publisher
    async with db.execute(
        "SELECT p.name FROM publishers p "
        "JOIN books_publishers_link bpl ON bpl.publisher = p.id "
        "WHERE bpl.book = ?",
        [book_id],
    ) as cur:
        pub_row = await cur.fetchone()
        publisher = pub_row["name"] if pub_row else None

    # Series
    async with db.execute(
        "SELECT s.name FROM series s "
        "JOIN books_series_link bsl ON bsl.series = s.id "
        "WHERE bsl.book = ?",
        [book_id],
    ) as cur:
        series_row = await cur.fetchone()
        series_name = series_row["name"] if series_row else None

    # Rating
    async with db.execute(
        "SELECT r.rating FROM ratings r "
        "JOIN books_ratings_link brl ON brl.rating = r.id "
        "WHERE brl.book = ?",
        [book_id],
    ) as cur:
        rating_row = await cur.fetchone()
        rating = rating_row["rating"] if rating_row else None

    # Languages
    async with db.execute(
        "SELECT l.lang_code FROM languages l "
        "JOIN books_languages_link bll ON bll.lang_code = l.id "
        "WHERE bll.book = ?",
        [book_id],
    ) as cur:
        languages = [r["lang_code"] for r in await cur.fetchall()]

    # Formats
    async with db.execute(
        "SELECT format, uncompressed_size FROM data WHERE book = ?",
        [book_id],
    ) as cur:
        formats = [
            {"format": r["format"], "size_mb": round(r["uncompressed_size"] / 1048576, 1)}
            for r in await cur.fetchall()
        ]

    # Identifiers
    async with db.execute(
        "SELECT type, val FROM identifiers WHERE book = ?",
        [book_id],
    ) as cur:
        identifiers = {r["type"]: r["val"] for r in await cur.fetchall()}

    # Description
    async with db.execute(
        "SELECT text FROM comments WHERE book = ?",
        [book_id],
    ) as cur:
        comment_row = await cur.fetchone()
        description = _strip_html(comment_row["text"]) if comment_row else None

    # Format output
    lines = [f"# {book['title']}\n"]
    lines.append(f"**Author:** {', '.join(authors) if authors else 'Unknown'}")

    if publisher:
        lines.append(f"**Publisher:** {publisher}")

    pub_year = book["pubdate"][:4] if book["pubdate"] and book["pubdate"][:4] != "0101" else None
    if pub_year:
        lines.append(f"**Published:** {pub_year}")

    if series_name:
        idx = book["series_index"]
        idx_str = f" #{int(idx)}" if idx and idx == int(idx) else (f" #{idx}" if idx else "")
        lines.append(f"**Series:** {series_name}{idx_str}")

    if rating:
        stars = rating // 2  # Calibre stores rating as 2x (e.g. 8 = 4 stars)
        lines.append(f"**Rating:** {'*' * stars} ({stars}/5)")

    if languages:
        lines.append(f"**Language:** {', '.join(languages)}")

    if formats:
        fmt_strs = [f"{f['format']} ({f['size_mb']}MB)" for f in formats]
        lines.append(f"**Formats:** {', '.join(fmt_strs)}")

    if identifiers:
        id_strs = [f"{k}: {v}" for k, v in identifiers.items()]
        lines.append(f"**Identifiers:** {', '.join(id_strs)}")

    if tags:
        lines.append(f"**Tags:** {', '.join(tags)}")

    if description:
        lines.append(f"\n{description[:2000]}")

    return "\n".join(lines)


@mcp.tool()
async def calibre_stats() -> str:
    """Get an overview of the Calibre ebook library: totals, top publishers, format and language breakdowns."""
    db = _db()

    async def _count(table: str) -> int:
        async with db.execute(f"SELECT COUNT(*) FROM {table}") as cur:
            return (await cur.fetchone())[0]

    totals = {
        "books": await _count("books"),
        "authors": await _count("authors"),
        "tags": await _count("tags"),
        "publishers": await _count("publishers"),
        "series": await _count("series"),
    }

    lines = ["# Calibre Library Stats\n"]
    for k, v in totals.items():
        lines.append(f"  {k.capitalize()}: {v}")

    # Format breakdown
    async with db.execute(
        "SELECT format, COUNT(*) AS cnt FROM data GROUP BY format ORDER BY cnt DESC"
    ) as cur:
        fmt_rows = await cur.fetchall()

    if fmt_rows:
        lines.append("\n**Formats:**")
        for r in fmt_rows:
            lines.append(f"  {r['format']}: {r['cnt']}")

    # Language breakdown
    async with db.execute(
        "SELECT l.lang_code, COUNT(*) AS cnt FROM languages l "
        "JOIN books_languages_link bll ON bll.lang_code = l.id "
        "GROUP BY l.lang_code ORDER BY cnt DESC"
    ) as cur:
        lang_rows = await cur.fetchall()

    if lang_rows:
        lines.append("\n**Languages:**")
        for r in lang_rows:
            lines.append(f"  {r['lang_code']}: {r['cnt']}")

    # Top 10 publishers
    async with db.execute(
        "SELECT p.name, COUNT(*) AS cnt FROM publishers p "
        "JOIN books_publishers_link bpl ON bpl.publisher = p.id "
        "GROUP BY p.id ORDER BY cnt DESC LIMIT 10"
    ) as cur:
        pub_rows = await cur.fetchall()

    if pub_rows:
        lines.append("\n**Top Publishers:**")
        for r in pub_rows:
            lines.append(f"  {r['name']}: {r['cnt']}")

    return "\n".join(lines)


if __name__ == "__main__":
    from mcp_search.run import serve
    serve(mcp)

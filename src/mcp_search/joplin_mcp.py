"""MCP server for Joplin note management via the Joplin Data API."""

import os
from datetime import datetime, timezone

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.server.lifespan import lifespan

_JOPLIN_URL = os.environ.get("JOPLIN_URL", "http://joplin-cli:41184")
_JOPLIN_TOKEN = os.environ["JOPLIN_TOKEN"]

_NOTE_FIELDS = "id,title,body,parent_id,created_time,updated_time,is_todo,todo_completed,source_url"
_NOTE_LIST_FIELDS = "id,title,parent_id,created_time,updated_time,is_todo,todo_completed"


@lifespan
async def joplin_lifespan(server):
    client = httpx.AsyncClient(timeout=30.0)
    yield {"client": client}
    await client.aclose()


mcp = FastMCP("joplin", lifespan=joplin_lifespan)


def _client() -> httpx.AsyncClient:
    return get_context().lifespan_context["client"]


async def _api(method: str, path: str, **kwargs) -> httpx.Response:
    """Make an authenticated request to the Joplin Data API."""
    client = _client()
    params = kwargs.pop("params", {})
    params["token"] = _JOPLIN_TOKEN
    resp = await client.request(method, f"{_JOPLIN_URL}{path}", params=params, **kwargs)
    resp.raise_for_status()
    return resp


async def _fetch_all(path: str, fields: str, limit: int = 100) -> list[dict]:
    """Fetch all items from a paginated Joplin endpoint."""
    items = []
    page = 1
    while True:
        resp = await _api("GET", path, params={"fields": fields, "limit": limit, "page": page})
        data = resp.json()
        page_items = data.get("items", data) if isinstance(data, dict) else data
        if isinstance(page_items, list):
            items.extend(page_items)
        else:
            break
        if isinstance(data, dict) and data.get("has_more"):
            page += 1
        else:
            break
    return items


def _format_ts(ms: int) -> str:
    """Format a Joplin millisecond timestamp."""
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _format_table(rows: list[dict], keys: list[str]) -> str:
    if not rows:
        return "No results."
    str_rows = [[str(row.get(k, "")) for k in keys] for row in rows]
    widths = [max(len(k), *(len(r[i]) for r in str_rows)) for i, k in enumerate(keys)]
    header = " | ".join(k.ljust(w) for k, w in zip(keys, widths))
    separator = "-+-".join("-" * w for w in widths)
    data_lines = [" | ".join(v.ljust(w) for v, w in zip(row, widths)) for row in str_rows]
    return "\n".join([header, separator, *data_lines, f"\n({len(rows)} rows)"])


def _format_note_list(notes: list[dict]) -> str:
    """Format a list of notes as a table."""
    rows = []
    for n in notes:
        todo = ""
        if n.get("is_todo"):
            todo = "done" if n.get("todo_completed") else "todo"
        rows.append({
            "id": n["id"],
            "title": n.get("title", ""),
            "updated": _format_ts(n.get("updated_time", 0)),
            "todo": todo,
        })
    return _format_table(rows, ["id", "title", "updated", "todo"])


@mcp.tool
async def joplin_search(
    query: str,
    limit: int = 25,
    page: int = 1,
) -> str:
    """Full-text search across all Joplin notes.

    Args:
        query: Search query (supports Joplin search syntax)
        limit: Number of results (default 25, max 100)
        page: Page number for pagination (default 1)
    """
    limit = min(limit, 100)
    resp = await _api("GET", "/search", params={
        "query": query,
        "type": "note",
        "fields": _NOTE_LIST_FIELDS,
        "limit": limit,
        "page": page,
    })
    data = resp.json()
    items = data.get("items", [])
    if not items:
        return f"No notes found for query: {query}"

    has_more = data.get("has_more", False)
    result = f"Search results for '{query}' (page {page}):\n\n"
    result += _format_note_list(items)
    if has_more:
        result += f"\n\nMore results available — use page={page + 1}"
    return result


@mcp.tool
async def joplin_get_note(note_id: str) -> str:
    """Read a single note with its full content.

    Args:
        note_id: The note ID (full or prefix from list output)
    """
    resp = await _api("GET", f"/notes/{note_id}", params={"fields": _NOTE_FIELDS})
    n = resp.json()

    lines = [
        f"# {n.get('title', 'Untitled')}",
        "",
        f"**ID:** {n['id']}",
        f"**Notebook:** {n.get('parent_id', '—')}",
        f"**Created:** {_format_ts(n.get('created_time', 0))}",
        f"**Updated:** {_format_ts(n.get('updated_time', 0))}",
    ]

    if n.get("is_todo"):
        status = "Completed" if n.get("todo_completed") else "Pending"
        lines.append(f"**Todo:** {status}")
    if n.get("source_url"):
        lines.append(f"**Source:** {n['source_url']}")

    body = n.get("body", "")
    if len(body) > 50000:
        body = body[:50000] + "\n\n... (truncated)"

    lines.extend(["", "---", "", body])
    return "\n".join(lines)


@mcp.tool
async def joplin_list_notes(
    notebook_id: str | None = None,
    order_by: str = "updated_time",
    order_dir: str = "DESC",
    limit: int = 50,
    page: int = 1,
) -> str:
    """List notes, optionally filtered by notebook.

    Args:
        notebook_id: Filter by notebook ID (omit for all notes)
        order_by: Sort field — 'updated_time', 'created_time', or 'title'
        order_dir: Sort direction — 'ASC' or 'DESC'
        limit: Number of results (default 50, max 100)
        page: Page number (default 1)
    """
    limit = min(limit, 100)
    path = f"/folders/{notebook_id}/notes" if notebook_id else "/notes"
    resp = await _api("GET", path, params={
        "fields": _NOTE_LIST_FIELDS,
        "order_by": order_by,
        "order_dir": order_dir,
        "limit": limit,
        "page": page,
    })
    data = resp.json()
    items = data.get("items", data) if isinstance(data, dict) else data
    if not items:
        return "No notes found."

    has_more = data.get("has_more", False) if isinstance(data, dict) else False
    result = _format_note_list(items)
    if has_more:
        result += f"\n\nMore results available — use page={page + 1}"
    return result


@mcp.tool
async def joplin_list_notebooks() -> str:
    """List all notebooks as an indented tree."""
    notebooks = await _fetch_all("/folders", "id,title,parent_id")
    if not notebooks:
        return "No notebooks found."

    # Build tree
    by_parent: dict[str, list[dict]] = {}
    for nb in notebooks:
        pid = nb.get("parent_id", "")
        by_parent.setdefault(pid, []).append(nb)

    lines = []

    def _render(parent_id: str, depth: int = 0):
        for nb in sorted(by_parent.get(parent_id, []), key=lambda x: x.get("title", "")):
            indent = "  " * depth
            lines.append(f"{indent}- [{nb['id']}] {nb.get('title', 'Untitled')}")
            _render(nb["id"], depth + 1)

    _render("")
    return "Notebooks:\n\n" + "\n".join(lines)


@mcp.tool
async def joplin_list_tags() -> str:
    """List all tags."""
    tags = await _fetch_all("/tags", "id,title")
    if not tags:
        return "No tags found."

    rows = [{"id": t["id"], "tag": t.get("title", "")} for t in sorted(tags, key=lambda x: x.get("title", ""))]
    return _format_table(rows, ["id", "tag"])


@mcp.tool
async def joplin_notes_by_tag(tag_id: str) -> str:
    """List all notes with a specific tag.

    Args:
        tag_id: The tag ID (from joplin_list_tags output)
    """
    notes = await _fetch_all(f"/tags/{tag_id}/notes", _NOTE_LIST_FIELDS)
    if not notes:
        return "No notes found with this tag."
    return _format_note_list(notes)


@mcp.tool
async def joplin_create_note(
    title: str,
    body: str = "",
    parent_id: str | None = None,
    is_todo: bool = False,
) -> str:
    """Create a new note.

    Args:
        title: Note title
        body: Note body in Markdown
        parent_id: Notebook ID to create the note in (omit for default notebook)
        is_todo: Set to true to create as a todo item
    """
    payload: dict = {"title": title, "body": body}
    if parent_id:
        payload["parent_id"] = parent_id
    if is_todo:
        payload["is_todo"] = 1

    resp = await _api("POST", "/notes", json=payload)
    n = resp.json()
    return f"Note created: [{n['id']}] {n.get('title', title)}"


@mcp.tool
async def joplin_update_note(
    note_id: str,
    title: str | None = None,
    body: str | None = None,
    parent_id: str | None = None,
    is_todo: bool | None = None,
    todo_completed: bool | None = None,
) -> str:
    """Update an existing note.

    Args:
        note_id: The note ID to update
        title: New title (omit to keep current)
        body: New body in Markdown (omit to keep current)
        parent_id: Move to a different notebook (omit to keep current)
        is_todo: Change todo status (omit to keep current)
        todo_completed: Mark todo as completed/uncompleted (omit to keep current)
    """
    payload: dict = {}
    if title is not None:
        payload["title"] = title
    if body is not None:
        payload["body"] = body
    if parent_id is not None:
        payload["parent_id"] = parent_id
    if is_todo is not None:
        payload["is_todo"] = 1 if is_todo else 0
    if todo_completed is not None:
        payload["todo_completed"] = int(datetime.now(timezone.utc).timestamp() * 1000) if todo_completed else 0

    if not payload:
        return "Nothing to update — provide at least one field."

    resp = await _api("PUT", f"/notes/{note_id}", json=payload)
    n = resp.json()
    return f"Note updated: [{n['id']}] {n.get('title', '')}"


@mcp.tool
async def joplin_tag_note(tag_id: str, note_id: str) -> str:
    """Add a tag to a note.

    Args:
        tag_id: The tag ID
        note_id: The note ID to tag
    """
    await _api("POST", f"/tags/{tag_id}/notes", json={"id": note_id})
    return f"Tag {tag_id} added to note {note_id}."


@mcp.tool
async def joplin_untag_note(tag_id: str, note_id: str) -> str:
    """Remove a tag from a note.

    Args:
        tag_id: The tag ID
        note_id: The note ID to untag
    """
    await _api("DELETE", f"/tags/{tag_id}/notes/{note_id}")
    return f"Tag {tag_id} removed from note {note_id}."


@mcp.tool
async def joplin_sync() -> str:
    """Trigger an immediate sync between the Joplin CLI and the Joplin Server.

    Use after creating/updating notes to push changes, or to pull
    recent changes made in other Joplin clients.
    """
    sync_url = _JOPLIN_URL.rsplit(":", 1)[0] + ":41186"
    client = _client()
    resp = await client.get(f"{sync_url}/sync", timeout=90.0)
    return resp.text


if __name__ == "__main__":
    from mcp_search.run import serve
    serve(mcp)

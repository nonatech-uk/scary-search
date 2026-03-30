"""Shared factory for Healthchecks MCP servers with multi-project support."""

import os
from contextlib import asynccontextmanager

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context


def _get_api_keys() -> list[str]:
    """Read API keys from HEALTHCHECKS_API_KEYS (comma-sep) or HEALTHCHECKS_API_KEY."""
    keys_str = os.environ.get("HEALTHCHECKS_API_KEYS", "")
    if keys_str:
        return [k.strip() for k in keys_str.split(",") if k.strip()]
    single = os.environ.get("HEALTHCHECKS_API_KEY", "")
    return [single] if single else []


def create_healthchecks_server(name: str, prefix: str) -> FastMCP:
    """Create a Healthchecks MCP server with the given name and tool prefix.

    Reads HEALTHCHECKS_URL and HEALTHCHECKS_API_KEYS (or HEALTHCHECKS_API_KEY) from env.
    """
    hc_url = os.environ.get("HEALTHCHECKS_URL", "https://hc.mees.st")
    api_keys = _get_api_keys()

    @asynccontextmanager
    async def hc_lifespan(server):
        clients = [
            httpx.AsyncClient(headers={"X-Api-Key": key}, timeout=15.0)
            for key in api_keys
        ]
        yield {"clients": clients}
        for c in clients:
            await c.aclose()

    mcp = FastMCP(name, lifespan=hc_lifespan)

    def _clients() -> list[httpx.AsyncClient]:
        return get_context().lifespan_context["clients"]

    def _status_icon(status: str) -> str:
        return {"up": "UP", "down": "DOWN", "grace": "GRACE", "paused": "PAUSED", "new": "NEW"}.get(
            status, status.upper()
        )

    async def _fetch_all_checks(tag: str | None = None) -> list[dict]:
        """Fetch checks from all project keys and deduplicate by UUID."""
        seen: set[str] = set()
        all_checks: list[dict] = []
        params = {}
        if tag:
            params["tag"] = tag

        for client in _clients():
            resp = await client.get(f"{hc_url}/api/v3/checks/", params=params)
            resp.raise_for_status()
            for c in resp.json().get("checks", []):
                uid = c.get("uuid", "")
                if uid not in seen:
                    seen.add(uid)
                    all_checks.append(c)
        return all_checks

    @mcp.tool(name=f"{prefix}_list_checks")
    async def list_checks(
        tag: str | None = None,
        status: str | None = None,
    ) -> str:
        """List all healthcheck monitors with their current status.

        Args:
            tag: Filter by tag (case-insensitive substring)
            status: Filter by status: 'up', 'down', 'grace', 'paused'
        """
        checks = await _fetch_all_checks(tag)

        if status:
            checks = [c for c in checks if c.get("status") == status]

        if not checks:
            return "No checks found."

        order = {"down": 0, "grace": 1, "new": 2, "paused": 3, "up": 4}
        checks.sort(key=lambda c: (order.get(c.get("status", ""), 5), c.get("name", "")))

        lines = [f"Healthchecks ({len(checks)}):\n"]
        for c in checks:
            st = _status_icon(c.get("status", "?"))
            last = c.get("last_ping", "never")
            if last and last != "never":
                last = last[:19].replace("T", " ")
            tags = ", ".join(c.get("tags", "").split()) if c.get("tags") else ""
            dur = c.get("last_duration")
            dur_str = f" ({dur}s)" if dur else ""

            lines.append(
                f"  [{st:6s}] {c['name']}{dur_str}\n"
                f"           Last ping: {last} | Tags: {tags or '—'}"
            )

        return "\n".join(lines)

    @mcp.tool(name=f"{prefix}_check_status")
    async def check_status(name: str) -> str:
        """Get detailed status of a specific healthcheck by name.

        Args:
            name: Check name (case-insensitive substring match)
        """
        checks = await _fetch_all_checks()

        name_lower = name.lower()
        matches = [c for c in checks if name_lower in c.get("name", "").lower()]

        if not matches:
            return f"No check matching '{name}' found."

        lines = []
        for c in matches:
            st = _status_icon(c.get("status", "?"))
            lines.append(
                f"# {c['name']}\n\n"
                f"**Status:** {st}\n"
                f"**UUID:** {c.get('uuid', '—')}\n"
                f"**Tags:** {c.get('tags', '—')}\n"
                f"**Schedule:** {c.get('schedule', '—')} ({c.get('tz', 'UTC')})\n"
                f"**Grace period:** {c.get('grace', '—')}s\n"
                f"**Last ping:** {c.get('last_ping', 'never')}\n"
                f"**Next expected:** {c.get('next_ping', '—')}\n"
                f"**Last duration:** {c.get('last_duration', '—')}s\n"
                f"**Total pings:** {c.get('n_pings', '—')}\n"
                f"**Description:** {c.get('desc', '—') or '—'}"
            )

        return "\n\n---\n\n".join(lines)

    @mcp.tool(name=f"{prefix}_failing_checks")
    async def failing_checks() -> str:
        """Get all checks that are currently down or in grace period."""
        checks = await _fetch_all_checks()
        failing = [c for c in checks if c.get("status") in ("down", "grace")]

        if not failing:
            return "All checks are healthy."

        lines = [f"Failing Checks ({len(failing)}):\n"]
        for c in failing:
            st = _status_icon(c.get("status", "?"))
            last = c.get("last_ping", "never")
            if last and last != "never":
                last = last[:19].replace("T", " ")
            schedule = c.get("schedule", "—")

            lines.append(
                f"  [{st:6s}] {c['name']}\n"
                f"           Last ping: {last} | Schedule: {schedule}\n"
                f"           Expected: {c.get('next_ping', '—')}"
            )

        return "\n".join(lines)

    @mcp.tool(name=f"{prefix}_ping_history")
    async def ping_history(name: str, limit: int = 10) -> str:
        """Get recent ping history for a specific check.

        Args:
            name: Check name (case-insensitive substring match)
            limit: Number of pings to show (default 10)
        """
        checks = await _fetch_all_checks()

        name_lower = name.lower()
        matches = [c for c in checks if name_lower in c.get("name", "").lower()]

        if not matches:
            return f"No check matching '{name}' found."

        check = matches[0]
        uuid = check.get("uuid")

        # Find the client that has this check (try each until we get pings)
        pings = []
        for client in _clients():
            resp = await client.get(f"{hc_url}/api/v3/checks/{uuid}/pings/")
            if resp.status_code == 200:
                pings = resp.json().get("pings", [])[:limit]
                if pings:
                    break

        if not pings:
            return f"No ping history for '{check['name']}'."

        lines = [f"Ping history for '{check['name']}' (last {len(pings)}):\n"]
        for p in pings:
            kind = p.get("type", "?")
            dt = p.get("date", "?")
            if dt and len(dt) > 19:
                dt = dt[:19].replace("T", " ")
            duration = p.get("duration")
            dur_str = f" ({duration}s)" if duration else ""
            lines.append(f"  {dt} | {kind}{dur_str}")

        return "\n".join(lines)

    return mcp

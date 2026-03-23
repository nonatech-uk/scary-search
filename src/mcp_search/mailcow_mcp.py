"""MCP server for read-only Mailcow admin API access."""

import os

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.server.lifespan import lifespan

_MC_URL = os.environ.get("MAILCOW_URL", "https://mail.mees.st")
_MC_KEY = os.environ.get("MAILCOW_API_KEY", "")


@lifespan
async def mc_lifespan(server):
    client = httpx.AsyncClient(
        headers={"X-API-Key": _MC_KEY},
        timeout=15.0,
    )
    yield {"client": client}
    await client.aclose()


mcp = FastMCP("mailcow", lifespan=mc_lifespan)


def _client() -> httpx.AsyncClient:
    return get_context().lifespan_context["client"]


def _format_table(rows: list[dict], keys: list[str]) -> str:
    if not rows:
        return "No results."
    str_rows = [[str(row.get(k, "")) for k in keys] for row in rows]
    widths = [max(len(k), *(len(r[i]) for r in str_rows)) for i, k in enumerate(keys)]
    header = " | ".join(k.ljust(w) for k, w in zip(keys, widths))
    separator = "-+-".join("-" * w for w in widths)
    data_lines = [" | ".join(v.ljust(w) for v, w in zip(row, widths)) for row in str_rows]
    return "\n".join([header, separator, *data_lines, f"\n({len(rows)} rows)"])


def _format_bytes(b: int | float | str) -> str:
    b = int(b)
    if b < 1024:
        return f"{b}B"
    for unit in ("KB", "MB", "GB", "TB"):
        b /= 1024
        if b < 1024:
            return f"{b:.1f}{unit}"
    return f"{b:.1f}PB"


def _pct(used: int | float | str, total: int | float | str) -> str:
    used, total = int(used), int(total)
    if not total:
        return "—"
    return f"{used / total * 100:.0f}%"


@mcp.tool
async def mailcow_domains() -> str:
    """List all mail domains with quota usage and mailbox counts."""
    client = _client()
    resp = await client.get(f"{_MC_URL}/api/v1/get/domain/all")
    resp.raise_for_status()
    domains = resp.json()

    if not domains:
        return "No domains found."

    result = []
    for d in domains:
        result.append({
            "domain": d.get("domain_name", ""),
            "active": "yes" if d.get("active") == 1 else "no",
            "mailboxes": str(d.get("mboxes_in_domain", 0)),
            "aliases": str(d.get("aliases_in_domain", 0)),
            "quota_used": _format_bytes(d.get("bytes_total", 0)),
            "quota_max": _format_bytes(d.get("max_quota_for_domain", 0)),
            "usage": _pct(d.get("bytes_total", 0), d.get("max_quota_for_domain", 0)),
            "msgs": str(d.get("msgs_total", 0)),
        })

    return "Mail Domains:\n\n" + _format_table(
        result, ["domain", "active", "mailboxes", "aliases", "quota_used", "quota_max", "usage", "msgs"]
    )


@mcp.tool
async def mailcow_mailboxes(domain: str | None = None) -> str:
    """List mailboxes with quota usage, message counts, and last login.

    Args:
        domain: Filter by domain (e.g. 'mees.st'). Omit to list all.
    """
    client = _client()
    if domain:
        resp = await client.get(f"{_MC_URL}/api/v1/get/mailbox/all/{domain}")
    else:
        resp = await client.get(f"{_MC_URL}/api/v1/get/mailbox/all")
    resp.raise_for_status()
    mailboxes = resp.json()

    if not mailboxes:
        return "No mailboxes found."

    result = []
    for m in mailboxes:
        last_imap = m.get("last_imap_login", 0)
        last_smtp = m.get("last_smtp_login", 0)
        last_login = max(last_imap or 0, last_smtp or 0)

        from datetime import datetime, timezone
        last_str = "never"
        if last_login:
            last_str = datetime.fromtimestamp(last_login, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

        result.append({
            "mailbox": m.get("username", ""),
            "active": "yes" if m.get("active") == 1 else "no",
            "quota_used": _format_bytes(m.get("quota_used", 0)),
            "quota_max": _format_bytes(m.get("quota", 0)),
            "usage": _pct(m.get("quota_used", 0), m.get("quota", 0)),
            "msgs": str(m.get("messages", 0)),
            "last_login": last_str,
        })

    return "Mailboxes:\n\n" + _format_table(
        result, ["mailbox", "active", "quota_used", "quota_max", "usage", "msgs", "last_login"]
    )


@mcp.tool
async def mailcow_mailbox_status(mailbox: str) -> str:
    """Get detailed status of a specific mailbox.

    Args:
        mailbox: Full email address (e.g. 'user@mees.st')
    """
    client = _client()
    resp = await client.get(f"{_MC_URL}/api/v1/get/mailbox/{mailbox}")
    resp.raise_for_status()
    data = resp.json()

    if not data:
        return f"Mailbox '{mailbox}' not found."

    m = data if isinstance(data, dict) else data[0]

    from datetime import datetime, timezone

    def _ts(val):
        if not val:
            return "never"
        return datetime.fromtimestamp(val, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

    attrs = m.get("attributes", {})
    lines = [
        f"# {m.get('username', mailbox)}\n",
        f"**Active:** {'yes' if m.get('active') == 1 else 'no'}",
        f"**Name:** {m.get('name', '—')}",
        f"**Domain:** {m.get('domain', '—')}",
        f"**Quota:** {_format_bytes(m.get('quota_used', 0))} / {_format_bytes(m.get('quota', 0))} ({_pct(m.get('quota_used', 0), m.get('quota', 0))})",
        f"**Messages:** {m.get('messages', '—')}",
        f"**Last IMAP login:** {_ts(m.get('last_imap_login'))}",
        f"**Last SMTP login:** {_ts(m.get('last_smtp_login'))}",
        f"**Last POP3 login:** {_ts(m.get('last_pop3_login'))}",
        f"**SMTP access:** {'yes' if attrs.get('force_pw_update') != '1' else 'password update required'}",
        f"**IMAP access:** {'yes' if attrs.get('imap_access') == '1' else 'no'}",
        f"**POP3 access:** {'yes' if attrs.get('pop3_access') == '1' else 'no'}",
        f"**SOGo access:** {'yes' if attrs.get('sogo_access') == '1' else 'no'}",
        f"**Spam filter:** score={attrs.get('spam_score', '—')}, aliases={m.get('aliases_in_domain', '—')}",
    ]

    return "\n".join(lines)


@mcp.tool
async def mailcow_queue() -> str:
    """Show the current Postfix mail queue (pending/deferred messages)."""
    client = _client()
    resp = await client.get(f"{_MC_URL}/api/v1/get/mailq/all")
    resp.raise_for_status()
    data = resp.json()

    if not data:
        return "Mail queue is empty."

    # Mailcow returns queue entries — format depends on version
    if isinstance(data, dict) and not data:
        return "Mail queue is empty."

    if isinstance(data, list) and len(data) == 0:
        return "Mail queue is empty."

    # Try to format as table
    if isinstance(data, list):
        result = []
        for entry in data:
            result.append({
                "queue_id": str(entry.get("queue_id", "")),
                "sender": str(entry.get("sender", "")),
                "recipients": str(entry.get("recipients", "")),
                "size": str(entry.get("message_size", "")),
                "arrival": str(entry.get("arrival_time", "")),
                "reason": str(entry.get("reason", ""))[:60],
            })
        return "Mail Queue:\n\n" + _format_table(
            result, ["queue_id", "sender", "recipients", "size", "arrival", "reason"]
        )

    # Fallback for unexpected format
    import json
    return f"Mail Queue:\n\n```json\n{json.dumps(data, indent=2)[:3000]}\n```"


@mcp.tool
async def mailcow_logs(
    log_type: str = "postfix",
    count: int = 25,
) -> str:
    """Get recent mail server logs.

    Args:
        log_type: Log type — 'postfix', 'dovecot', 'rspamd', 'sogo', 'api', 'netfilter', 'autodiscover', 'watchdog'
        count: Number of log entries to return (default 25, max 200)
    """
    valid_types = ("postfix", "dovecot", "rspamd", "sogo", "api", "netfilter", "autodiscover", "watchdog")
    if log_type not in valid_types:
        return f"Invalid log type '{log_type}'. Valid: {', '.join(valid_types)}"

    client = _client()
    count = min(count, 200)
    resp = await client.get(f"{_MC_URL}/api/v1/get/logs/{log_type}/{count}")
    resp.raise_for_status()
    entries = resp.json()

    if not entries:
        return f"No {log_type} logs found."

    lines = [f"{log_type.title()} Logs (last {count}):\n"]
    for e in entries:
        ts = e.get("time", e.get("timestamp", ""))
        msg = e.get("message", e.get("msg", str(e)))
        if isinstance(msg, list):
            msg = " ".join(str(m) for m in msg)
        # Truncate very long messages
        if len(str(msg)) > 200:
            msg = str(msg)[:200] + "…"
        lines.append(f"  {ts} | {msg}")

    return "\n".join(lines)


if __name__ == "__main__":
    from mcp_search.run import serve
    serve(mcp)

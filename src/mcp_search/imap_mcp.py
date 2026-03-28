"""MCP server for IMAP email search and folder operations.

Supports multiple mailboxes. Configure via environment variables:
  IMAP_HOST       — IMAP server hostname (default: mail.mees.st)
  IMAP_PORT       — IMAP server port (default: 993)
  IMAP_ACCOUNTS   — JSON object mapping mailbox address to password, e.g.:
                     {"user@mees.st": "pass1", "info@mees.st": "pass2"}
"""

import email as emaillib
import email.header
import email.utils
import json
import os
import re
from datetime import datetime, timezone

import contextlib

import aioimaplib
from fastmcp import FastMCP

_IMAP_HOST = os.environ.get("IMAP_HOST", "mail.mees.st")
_IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
_ACCOUNTS: dict[str, str] = json.loads(os.environ.get("IMAP_ACCOUNTS", "{}"))

# Folders that moves are blocked to
_BLOCKED_FOLDERS = {"Deleted Items", "Deleted Messages", "Trash", "Junk", "Junk E-mail", "Spam"}

mcp = FastMCP("imap")


@contextlib.asynccontextmanager
async def _imap_client(mailbox: str):
    """Create a fresh IMAP connection for a single tool call, then close it."""
    if mailbox not in _ACCOUNTS:
        available = ", ".join(sorted(_ACCOUNTS.keys()))
        raise ValueError(f"Unknown mailbox '{mailbox}'. Available: {available}")
    client = aioimaplib.IMAP4_SSL(host=_IMAP_HOST, port=_IMAP_PORT)
    await client.wait_hello_from_server()
    await client.login(mailbox, _ACCOUNTS[mailbox])
    try:
        yield client
    finally:
        try:
            await client.logout()
        except Exception:
            pass


def _to_str(val: str | bytes | bytearray) -> str:
    if isinstance(val, (bytes, bytearray)):
        return val.decode("utf-8", errors="replace")
    return val


def _decode_header(raw: str | None) -> str:
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return " ".join(decoded)


def _parse_date(raw: str | None) -> str:
    if not raw:
        return ""
    parsed = email.utils.parsedate_to_datetime(raw)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _is_blocked_folder(folder: str) -> bool:
    normalised = folder.strip().strip('"')
    return normalised.lower() in {f.lower() for f in _BLOCKED_FOLDERS}


def _parse_search_ids(resp) -> list[str]:
    """Extract message sequence numbers from an IMAP SEARCH response."""
    for line in resp.lines:
        s = _to_str(line).strip()
        # Skip status lines like "Search completed ..."
        if s and not s.startswith("Search") and not s.startswith("OK"):
            return s.split()
    return []


def _parse_headers(resp) -> list[dict]:
    """Parse FETCH response for header fields, handling aioimaplib's line format.

    aioimaplib returns:
      line 0: b'N FETCH (FLAGS (...) BODY[HEADER.FIELDS ...] {size}'
      line 1: bytearray(b'From: ...\r\nSubject: ...\r\nDate: ...\r\n\r\n')
      line 2: b')'
      ...repeats for each message...
    """
    messages = []
    lines = resp.lines

    i = 0
    while i < len(lines):
        line = _to_str(lines[i])
        i += 1

        # Look for FETCH response line with FLAGS
        if "FETCH" not in line:
            continue

        flags = ""
        flag_match = re.search(r"FLAGS \(([^)]*)\)", line)
        if flag_match:
            flags = flag_match.group(1)

        # Next line should be the header data as bytearray
        if i < len(lines) and isinstance(lines[i], (bytes, bytearray)):
            header_data = _to_str(lines[i])
            i += 1

            msg = {"flags": flags}
            for header_line in header_data.split("\r\n"):
                header_line = header_line.strip()
                if not header_line:
                    continue
                upper = header_line.upper()
                if upper.startswith("FROM:"):
                    msg["from"] = _decode_header(header_line[5:].strip())
                elif upper.startswith("SUBJECT:"):
                    msg["subject"] = _decode_header(header_line[8:].strip())
                elif upper.startswith("DATE:"):
                    try:
                        msg["date"] = _parse_date(header_line[5:].strip())
                    except Exception:
                        msg["date"] = header_line[5:].strip()[:19]

            messages.append(msg)

    return messages


@mcp.tool
async def imap_accounts() -> str:
    """List all configured IMAP mailbox accounts."""
    accounts = sorted(_ACCOUNTS.keys())
    if not accounts:
        return "No IMAP accounts configured."
    lines = [f"Configured IMAP accounts ({len(accounts)}):\n"]
    for a in accounts:
        lines.append(f"  {a}")
    return "\n".join(lines)


@mcp.tool
async def imap_folders(mailbox: str) -> str:
    """List all IMAP folders for a mailbox account.

    Args:
        mailbox: Email address of the account (e.g. 'user@mees.st')
    """
    try:
        async with _imap_client(mailbox) as client:
            resp = await client.list('""', "*")
            if resp.result != "OK":
                return f"Failed to list folders: {resp.result}"

            folders = []
            for line in resp.lines:
                line_str = _to_str(line).strip()
                if not line_str:
                    continue
                match = re.match(r'\(([^)]*)\)\s+"([^"]+)"\s+"?([^"]+)"?', line_str)
                if match:
                    flags, _delim, name = match.groups()
                    name = name.strip('"')
                    folders.append(f"  {name}  [{flags}]")

            if not folders:
                return "No folders found."

            return f"IMAP Folders for {mailbox} ({len(folders)}):\n\n" + "\n".join(sorted(folders))
    except ValueError as e:
        return str(e)


@mcp.tool
async def imap_search(
    mailbox: str,
    folder: str = "INBOX",
    query: str | None = None,
    from_addr: str | None = None,
    subject: str | None = None,
    since: str | None = None,
    before: str | None = None,
    unseen: bool = False,
    limit: int = 25,
) -> str:
    """Search emails in a folder using IMAP SEARCH.

    Args:
        mailbox: Email address of the account (e.g. 'user@mees.st')
        folder: IMAP folder to search (default 'INBOX')
        query: Free text search (searches subject and body via TEXT)
        from_addr: Filter by sender address or name
        subject: Filter by subject line
        since: Emails since this date (YYYY-MM-DD)
        before: Emails before this date (YYYY-MM-DD)
        unseen: Only show unread messages
        limit: Max results to return (default 25, max 100)
    """
    try:
        async with _imap_client(mailbox) as client:
            resp = await client.select(folder)
            if resp.result != "OK":
                return f"Cannot open folder '{folder}': {resp.result}"

            criteria = []
            if query:
                criteria.append(f'TEXT "{query}"')
            if from_addr:
                criteria.append(f'FROM "{from_addr}"')
            if subject:
                criteria.append(f'SUBJECT "{subject}"')
            if since:
                try:
                    dt = datetime.strptime(since, "%Y-%m-%d")
                    criteria.append(f'SINCE {dt.strftime("%d-%b-%Y")}')
                except ValueError:
                    return "Invalid 'since' format — use YYYY-MM-DD."
            if before:
                try:
                    dt = datetime.strptime(before, "%Y-%m-%d")
                    criteria.append(f'BEFORE {dt.strftime("%d-%b-%Y")}')
                except ValueError:
                    return "Invalid 'before' format — use YYYY-MM-DD."
            if unseen:
                criteria.append("UNSEEN")

            search_str = " ".join(criteria) if criteria else "ALL"
            resp = await client.search(search_str)
            if resp.result != "OK":
                return f"Search failed: {resp.result}"

            seqs = _parse_search_ids(resp)
            if not seqs:
                return "No messages found."

            total = len(seqs)
            limit = min(limit, 100)
            seqs = seqs[-limit:]

            seq_set = ",".join(seqs)
            resp = await client.fetch(seq_set, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if resp.result != "OK":
                return f"Fetch failed: {resp.result}"

            messages = _parse_headers(resp)
            if not messages:
                return "No messages found."

            lines = [f"Search results in '{mailbox}:{folder}' ({len(messages)} shown, {total} matched):\n"]
            for m in reversed(messages):
                flags = m.get("flags", "")
                seen = "  " if "\\Seen" in flags else "* "
                date = m.get("date", "?")
                frm = m.get("from", "?")[:40]
                subj = m.get("subject", "(no subject)")[:60]
                lines.append(f"  {seen}{date} | {frm:40s} | {subj}")

            return "\n".join(lines)
    except ValueError as e:
        return str(e)


@mcp.tool
async def imap_read(
    mailbox: str,
    folder: str = "INBOX",
    sequence: int = 0,
    subject_match: str | None = None,
) -> str:
    """Read the full content of an email.

    Find messages by sequence number or by subject match (returns the latest match).

    Args:
        mailbox: Email address of the account (e.g. 'user@mees.st')
        folder: IMAP folder (default 'INBOX')
        sequence: Message sequence number (1-based). If 0, uses subject_match instead.
        subject_match: Subject substring to find (case-insensitive). Returns the latest match.
    """
    try:
        async with _imap_client(mailbox) as client:
            resp = await client.select(folder)
            if resp.result != "OK":
                return f"Cannot open folder '{folder}': {resp.result}"

            if sequence > 0:
                msg_id = str(sequence)
            elif subject_match:
                resp = await client.search(f'SUBJECT "{subject_match}"')
                if resp.result != "OK":
                    return f"Search failed: {resp.result}"
                seqs = _parse_search_ids(resp)
                if not seqs:
                    return f"No message matching subject '{subject_match}'."
                msg_id = seqs[-1]
            else:
                return "Provide either sequence number or subject_match."

            resp = await client.fetch(msg_id, "(FLAGS BODY.PEEK[])")
            if resp.result != "OK":
                return f"Failed to fetch message: {resp.result}"

            # Find the raw email data (bytearray from aioimaplib)
            raw_email = None
            for line in resp.lines:
                if isinstance(line, (bytes, bytearray)) and len(line) > 100:
                    raw_email = bytes(line)
                    break

            if not raw_email:
                return "Could not parse message."

    except ValueError as e:
        return str(e)

    msg = emaillib.message_from_bytes(raw_email)
    from_h = _decode_header(msg.get("From", ""))
    to_h = _decode_header(msg.get("To", ""))
    subj_h = _decode_header(msg.get("Subject", ""))
    date_h = msg.get("Date", "")
    cc_h = _decode_header(msg.get("Cc", ""))

    lines = [
        f"**From:** {from_h}",
        f"**To:** {to_h}",
    ]
    if cc_h:
        lines.append(f"**Cc:** {cc_h}")
    lines.extend([
        f"**Date:** {date_h}",
        f"**Subject:** {subj_h}",
        "",
        "---",
        "",
    ])

    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="replace")
                    break
        if not body:
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body = f"[HTML content]\n{payload.decode(charset, errors='replace')}"
                        break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")

    if len(body) > 5000:
        body = body[:5000] + "\n\n… [truncated]"

    lines.append(body or "(empty body)")
    return "\n".join(lines)


@mcp.tool
async def imap_move(
    mailbox: str,
    folder: str,
    destination: str,
    sequence: int = 0,
    subject_match: str | None = None,
) -> str:
    """Move an email to a different folder.

    Moves are blocked to Deleted/Junk/Trash/Spam folders for safety.

    Args:
        mailbox: Email address of the account (e.g. 'user@mees.st')
        folder: Source folder (e.g. 'INBOX')
        destination: Target folder to move to (e.g. 'Archive', 'Receipts')
        sequence: Message sequence number (1-based). If 0, uses subject_match.
        subject_match: Subject substring to find. Moves the latest match.
    """
    if _is_blocked_folder(destination):
        return f"Moving to '{destination}' is blocked for safety. Blocked folders: {', '.join(sorted(_BLOCKED_FOLDERS))}"

    try:
        async with _imap_client(mailbox) as client:
            resp = await client.select(folder)
            if resp.result != "OK":
                return f"Cannot open folder '{folder}': {resp.result}"

            if sequence > 0:
                msg_id = str(sequence)
            elif subject_match:
                resp = await client.search(f'SUBJECT "{subject_match}"')
                if resp.result != "OK":
                    return f"Search failed: {resp.result}"
                seqs = _parse_search_ids(resp)
                if not seqs:
                    return f"No message matching subject '{subject_match}'."
                msg_id = seqs[-1]
            else:
                return "Provide either sequence number or subject_match."

            # Use atomic MOVE (RFC 6851) — avoids ghost copies from client sync races
            resp = await client.move(msg_id, destination)
            if resp.result != "OK":
                # Fall back to copy+delete if MOVE not supported
                resp = await client.copy(msg_id, destination)
                if resp.result != "OK":
                    return f"Failed to copy to '{destination}': {resp.result}. Does the folder exist?"
                await client.store(msg_id, "+FLAGS", r"(\Deleted)")
                await client.expunge()

            return f"Moved message {msg_id} from '{mailbox}:{folder}' to '{destination}'."
    except ValueError as e:
        return str(e)


if __name__ == "__main__":
    from mcp_search.run import serve
    serve(mcp)

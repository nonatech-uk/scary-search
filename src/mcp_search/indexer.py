"""Standalone indexer: syncs Paperless-ngx documents into Meilisearch with OpenAI embeddings."""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

PAPERLESS_URL = os.environ.get("PAPERLESS_URL", "http://paperless:8000")
PAPERLESS_TOKEN = os.environ.get("PAPERLESS_TOKEN", "")
MEILI_URL = os.environ.get("MEILISEARCH_URL", "http://meilisearch-docs:7700")
MEILI_KEY = os.environ.get("MEILISEARCH_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
STATE_DIR = os.environ.get("STATE_DIR", "/state")
INDEX_NAME = "documents"
BATCH_SIZE = 100
CONTENT_MAX_CHARS = 50_000


def _state_path() -> Path:
    return Path(STATE_DIR) / "indexer_state.json"


def _load_state() -> dict:
    p = _state_path()
    if p.exists():
        return json.loads(p.read_text())
    return {}


def _save_state(state: dict):
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2))


async def _fetch_all_pages(
    client: httpx.AsyncClient, path: str, params: dict | None = None
) -> list[dict]:
    """Fetch all pages from a paginated Paperless API endpoint."""
    results = []
    url = f"{PAPERLESS_URL}{path}"
    req_params = {"page_size": 100, **(params or {})}
    while True:
        resp = await client.get(url, params=req_params)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("results", []))
        next_url = data.get("next")
        if not next_url:
            break
        url = next_url
        req_params = {}  # next URL already includes params
    return results


async def _ensure_index(meili: httpx.AsyncClient):
    """Create or update the Meilisearch index with correct settings."""
    # Create index if needed
    resp = await meili.post(
        f"{MEILI_URL}/indexes",
        json={"uid": INDEX_NAME, "primaryKey": "id"},
    )
    # 202 = created task, 409 = already exists — both fine
    if resp.status_code not in (200, 202, 409):
        resp.raise_for_status()

    # Wait for task if created
    if resp.status_code == 202:
        task = resp.json()
        await _wait_task(meili, task["taskUid"])

    # Update settings
    settings = {
        "searchableAttributes": [
            "title",
            "content",
            "correspondent_name",
            "document_type_name",
            "tag_names",
        ],
        "filterableAttributes": [
            "correspondent_name",
            "document_type_name",
            "tag_names",
            "created_year",
            "created",
        ],
        "sortableAttributes": ["created", "added", "title"],
        "displayedAttributes": ["*"],
    }
    resp = await meili.patch(f"{MEILI_URL}/indexes/{INDEX_NAME}/settings", json=settings)
    if resp.status_code == 202:
        await _wait_task(meili, resp.json()["taskUid"])

    # Configure OpenAI embedder
    embedder_settings = {
        "embedders": {
            "openai": {
                "source": "openAi",
                "apiKey": OPENAI_API_KEY,
                "model": "text-embedding-3-small",
                "documentTemplate": "A document titled '{{doc.title}}' from {{doc.correspondent_name}} ({{doc.document_type_name}}). Content: {{doc.content}}",
            }
        }
    }
    resp = await meili.patch(
        f"{MEILI_URL}/indexes/{INDEX_NAME}/settings", json=embedder_settings
    )
    if resp.status_code == 202:
        await _wait_task(meili, resp.json()["taskUid"])


async def _wait_task(meili: httpx.AsyncClient, task_uid: int, timeout: int = 300):
    """Wait for a Meilisearch task to complete."""
    start = time.time()
    while time.time() - start < timeout:
        resp = await meili.get(f"{MEILI_URL}/tasks/{task_uid}")
        resp.raise_for_status()
        task = resp.json()
        status = task.get("status")
        if status == "succeeded":
            return task
        if status == "failed":
            raise RuntimeError(f"Meilisearch task {task_uid} failed: {task.get('error')}")
        await asyncio.sleep(1)
    raise TimeoutError(f"Meilisearch task {task_uid} timed out after {timeout}s")


def _transform_doc(
    doc: dict,
    corr_by_id: dict[int, str],
    type_by_id: dict[int, str],
    tag_by_id: dict[int, str],
) -> dict:
    """Transform a Paperless document into a Meilisearch document."""
    content = doc.get("content", "") or ""
    if len(content) > CONTENT_MAX_CHARS:
        content = content[:CONTENT_MAX_CHARS]

    created = doc.get("created", "")
    created_year = None
    if created:
        try:
            created_year = int(created[:4])
        except (ValueError, IndexError):
            pass

    return {
        "id": doc["id"],
        "title": doc.get("title", ""),
        "content": content,
        "correspondent_name": corr_by_id.get(doc.get("correspondent"), ""),
        "document_type_name": type_by_id.get(doc.get("document_type"), ""),
        "tag_names": [tag_by_id.get(tid, "") for tid in doc.get("tags", []) if tid in tag_by_id],
        "created": created,
        "added": doc.get("added", ""),
        "created_year": created_year,
        "archive_serial_number": doc.get("archive_serial_number"),
    }


async def run_sync():
    state = _load_state()
    last_sync = state.get("last_sync")

    paperless = httpx.AsyncClient(
        headers={"Authorization": f"Token {PAPERLESS_TOKEN}"},
        timeout=60.0,
    )
    meili = httpx.AsyncClient(
        headers={"Authorization": f"Bearer {MEILI_KEY}"},
        timeout=60.0,
    )

    try:
        # Ensure index + settings
        print("Ensuring Meilisearch index and settings...")
        await _ensure_index(meili)

        # Fetch name mappings
        print("Fetching Paperless metadata...")
        tags = await _fetch_all_pages(paperless, "/api/tags/")
        doc_types = await _fetch_all_pages(paperless, "/api/document_types/")
        correspondents = await _fetch_all_pages(paperless, "/api/correspondents/")

        tag_by_id = {t["id"]: t["name"] for t in tags}
        type_by_id = {t["id"]: t["name"] for t in doc_types}
        corr_by_id = {c["id"]: c["name"] for c in correspondents}

        # Fetch documents
        params = {}
        if last_sync:
            params["modified__gt"] = last_sync
            print(f"Incremental sync since {last_sync}")
        else:
            print("Full sync (no previous state)")

        print("Fetching documents from Paperless...")
        documents = await _fetch_all_pages(paperless, "/api/documents/", params)
        print(f"Found {len(documents)} documents to sync")

        if not documents:
            print("No documents to sync")
            return

        # Transform and upload in batches
        total_uploaded = 0
        batch = []
        for doc in documents:
            transformed = _transform_doc(doc, corr_by_id, type_by_id, tag_by_id)
            batch.append(transformed)

            if len(batch) >= BATCH_SIZE:
                resp = await meili.post(
                    f"{MEILI_URL}/indexes/{INDEX_NAME}/documents",
                    json=batch,
                )
                resp.raise_for_status()
                task_uid = resp.json()["taskUid"]
                await _wait_task(meili, task_uid, timeout=600)
                total_uploaded += len(batch)
                print(f"  Uploaded {total_uploaded}/{len(documents)}")
                batch = []

        # Upload remaining
        if batch:
            resp = await meili.post(
                f"{MEILI_URL}/indexes/{INDEX_NAME}/documents",
                json=batch,
            )
            resp.raise_for_status()
            task_uid = resp.json()["taskUid"]
            await _wait_task(meili, task_uid, timeout=600)
            total_uploaded += len(batch)
            print(f"  Uploaded {total_uploaded}/{len(documents)}")

        # Save state
        state["last_sync"] = datetime.now(timezone.utc).isoformat()
        state["last_count"] = len(documents)
        _save_state(state)
        print(f"Sync complete: {total_uploaded} documents indexed")

    finally:
        await paperless.aclose()
        await meili.aclose()


def main():
    asyncio.run(run_sync())


if __name__ == "__main__":
    main()

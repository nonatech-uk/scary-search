# mcp-search

Three read-only MCP servers (FastMCP, stdio transport) for personal data search:

- **postgres_mcp** — Query finance and mylocation PostgreSQL databases
- **paperless_mcp** — Search and retrieve documents from Paperless-ngx
- **meilisearch_mcp** — Hybrid (keyword + semantic) search over indexed documents

Plus a standalone **indexer** that syncs Paperless documents into Meilisearch with OpenAI embeddings.

## Running

Each server runs containerized on `podman-frontend` network, launched via `podman run --rm -i`.

```bash
# Build
podman build -t mcp-search:latest .

# Run individual servers
podman run --rm -i --network podman-frontend --env-file /zfs/Apps/AppData/mcp-search/.env.postgres mcp-search:latest python -m mcp_search.postgres_mcp
podman run --rm -i --network podman-frontend --env-file /zfs/Apps/AppData/mcp-search/.env.paperless mcp-search:latest python -m mcp_search.paperless_mcp
podman run --rm -i --network podman-frontend --env-file /zfs/Apps/AppData/mcp-search/.env.meilisearch mcp-search:latest python -m mcp_search.meilisearch_mcp

# Run indexer
podman run --rm --network podman-frontend --env-file /zfs/Apps/AppData/mcp-search/.env.indexer -v /zfs/Apps/AppData/mcp-search:/state mcp-search:latest python -m mcp_search.indexer
```

## Git

Identity: `Stu Bevan <stu.bevan@nonatech.co.uk>`

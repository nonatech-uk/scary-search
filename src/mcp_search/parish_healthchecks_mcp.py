"""MCP server for Albury Parish Council Healthchecks monitoring."""

from mcp_search.healthchecks_base import create_healthchecks_server

mcp = create_healthchecks_server("parish-healthchecks", "parish_hc")

if __name__ == "__main__":
    from mcp_search.run import serve
    serve(mcp)

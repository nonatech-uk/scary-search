"""MCP server for read-only PostgreSQL access to finance and mylocation databases."""

import os
import re

import asyncpg
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context

DATABASES = ("finance", "mylocation")
DML_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|COPY)\b",
    re.IGNORECASE,
)
MAX_ROWS = 500


async def _create_pool(database: str) -> asyncpg.Pool:
    """Create an asyncpg pool for the given database."""

    async def _init_conn(conn):
        await conn.execute("SET default_transaction_read_only = on")

    return await asyncpg.create_pool(
        host=os.environ.get("POSTGRES_HOST", "postgres"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ.get("POSTGRES_USER", "mcp_readonly"),
        password=os.environ["POSTGRES_PASSWORD"],
        database=database,
        min_size=1,
        max_size=5,
        ssl=os.environ.get("POSTGRES_SSLMODE", "prefer"),
        init=_init_conn,
    )


from fastmcp.server.lifespan import lifespan


@lifespan
async def pg_lifespan(server):
    pools = {}
    for db in DATABASES:
        pools[db] = await _create_pool(db)
    yield {"pools": pools}
    for pool in pools.values():
        await pool.close()


mcp = FastMCP("postgres-search", lifespan=pg_lifespan)


def _get_pool(database: str) -> asyncpg.Pool:
    ctx = get_context()
    pools = ctx.lifespan_context["pools"]
    if database not in pools:
        raise ValueError(f"Unknown database: {database}. Must be one of: {', '.join(DATABASES)}")
    return pools[database]


@mcp.tool
async def pg_discover_schema(
    database: str,
    table_filter: str | None = None,
) -> str:
    """List tables and columns in a PostgreSQL database.

    Args:
        database: Database name — 'finance' or 'mylocation'
        table_filter: Optional substring to filter table names
    """
    pool = _get_pool(database)
    query = """
        SELECT table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public'
    """
    params = []
    if table_filter:
        query += " AND table_name ILIKE $1"
        params.append(f"%{table_filter}%")
    query += " ORDER BY table_name, ordinal_position"

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    if not rows:
        return "No tables found."

    current_table = None
    lines = []
    for row in rows:
        if row["table_name"] != current_table:
            current_table = row["table_name"]
            lines.append(f"\n## {current_table}")
        nullable = " (nullable)" if row["is_nullable"] == "YES" else ""
        lines.append(f"  - {row['column_name']}: {row['data_type']}{nullable}")

    return "\n".join(lines)


@mcp.tool
async def pg_query(
    database: str,
    query: str,
    params: list[str] | None = None,
    response_format: str = "text",
) -> str:
    """Execute a read-only parameterised SQL query.

    Args:
        database: Database name — 'finance' or 'mylocation'
        query: SQL SELECT query using $1, $2 for parameters
        params: Optional list of parameter values
        response_format: 'text' for aligned columns, 'csv' for CSV format
    """
    if DML_PATTERN.search(query):
        return "Error: Only SELECT queries are allowed."

    stripped = query.strip().rstrip(";")
    if not re.match(r"(?i)^(SELECT|WITH)\b", stripped):
        return "Error: Query must start with SELECT or WITH."

    # Enforce LIMIT
    if not re.search(r"\bLIMIT\b", stripped, re.IGNORECASE):
        stripped += f" LIMIT {MAX_ROWS}"

    pool = _get_pool(database)
    async with pool.acquire() as conn:
        rows = await conn.fetch(stripped, *(params or []))

    if not rows:
        return "No results."

    keys = list(rows[0].keys())
    if response_format == "csv":
        lines = [",".join(keys)]
        for row in rows:
            lines.append(",".join(str(row[k]) for k in keys))
        return "\n".join(lines)

    # Text table format
    str_rows = [[str(row[k]) for k in keys] for row in rows]
    widths = [max(len(k), *(len(r[i]) for r in str_rows)) for i, k in enumerate(keys)]
    header = " | ".join(k.ljust(w) for k, w in zip(keys, widths))
    separator = "-+-".join("-" * w for w in widths)
    data_lines = [" | ".join(v.ljust(w) for v, w in zip(row, widths)) for row in str_rows]
    return "\n".join([header, separator, *data_lines, f"\n({len(rows)} rows)"])


@mcp.tool
async def pg_financial_summary(
    database: str,
    table_name: str,
    date_column: str,
    amount_column: str,
    category_column: str | None = None,
    year: int | None = None,
    category_filter: str | None = None,
) -> str:
    """Get monthly spending summary with optional year-over-year comparison.

    Args:
        database: Database name (usually 'finance')
        table_name: Table containing transactions
        date_column: Column with transaction dates
        amount_column: Column with transaction amounts
        category_column: Optional column for category grouping
        year: Year to summarise (defaults to current year)
        category_filter: Optional category substring filter
    """
    pool = _get_pool(database)
    # Validate identifiers
    for ident in [table_name, date_column, amount_column]:
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", ident):
            return f"Error: Invalid identifier: {ident}"
    if category_column and not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", category_column):
        return f"Error: Invalid identifier: {category_column}"

    year_val = year or 2026
    prev_year = year_val - 1

    group_col = f", {category_column}" if category_column else ""
    select_cat = f", {category_column} AS category" if category_column else ""
    where_cat = ""
    params: list = []
    param_idx = 1

    if category_filter and category_column:
        where_cat = f" AND {category_column} ILIKE ${param_idx}"
        params.append(f"%{category_filter}%")
        param_idx += 1

    query = f"""
        SELECT
            EXTRACT(YEAR FROM {date_column})::int AS year,
            EXTRACT(MONTH FROM {date_column})::int AS month,
            ROUND(SUM({amount_column})::numeric, 2) AS total,
            COUNT(*) AS txn_count
            {select_cat}
        FROM {table_name}
        WHERE EXTRACT(YEAR FROM {date_column}) IN ({year_val}, {prev_year})
            {where_cat}
        GROUP BY year, month{group_col}
        ORDER BY year, month{group_col}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    if not rows:
        return "No data found."

    lines = [f"Monthly summary for {year_val} (with {prev_year} comparison):\n"]
    keys = list(rows[0].keys())
    str_rows = [[str(row[k]) for k in keys] for row in rows]
    widths = [max(len(k), *(len(r[i]) for r in str_rows)) for i, k in enumerate(keys)]
    lines.append(" | ".join(k.ljust(w) for k, w in zip(keys, widths)))
    lines.append("-+-".join("-" * w for w in widths))
    for sr in str_rows:
        lines.append(" | ".join(v.ljust(w) for v, w in zip(sr, widths)))
    lines.append(f"\n({len(rows)} rows)")
    return "\n".join(lines)


@mcp.tool
async def pg_location_days(
    database: str,
    table_name: str,
    date_column: str,
    location_column: str,
    location_name: str,
    year: int | None = None,
    compare_year: int | None = None,
) -> str:
    """Count days at a specific location, with optional year comparison.

    Args:
        database: Database name (usually 'mylocation')
        table_name: Table containing location data
        date_column: Column with dates
        location_column: Column with location names
        location_name: Location to search for (case-insensitive substring)
        year: Primary year to count
        compare_year: Optional second year for comparison
    """
    pool = _get_pool(database)
    for ident in [table_name, date_column, location_column]:
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", ident):
            return f"Error: Invalid identifier: {ident}"

    year_val = year or 2026
    years = [year_val]
    if compare_year:
        years.append(compare_year)

    query = f"""
        SELECT
            EXTRACT(YEAR FROM {date_column})::int AS year,
            COUNT(DISTINCT {date_column}) AS days
        FROM {table_name}
        WHERE {location_column} ILIKE $1
            AND EXTRACT(YEAR FROM {date_column}) = ANY($2)
        GROUP BY year
        ORDER BY year
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, f"%{location_name}%", years)

    if not rows:
        return f"No days found for '{location_name}'."

    lines = [f"Days at locations matching '{location_name}':\n"]
    for row in rows:
        lines.append(f"  {row['year']}: {row['days']} days")

    if len(rows) == 2:
        diff = rows[1]["days"] - rows[0]["days"]
        sign = "+" if diff >= 0 else ""
        lines.append(f"\n  Change: {sign}{diff} days")

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()

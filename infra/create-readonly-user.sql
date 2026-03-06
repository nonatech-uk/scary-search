-- Create read-only role for MCP search servers
-- Run via: podman exec postgres psql -U postgres -f /tmp/create-readonly-user.sql

-- Create the role (idempotent)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcp_readonly') THEN
        CREATE ROLE mcp_readonly WITH LOGIN PASSWORD '@@PASSWORD@@';
    END IF;
END
$$;

-- Belt-and-braces: force read-only transactions
ALTER ROLE mcp_readonly SET default_transaction_read_only = on;

-- Grant access to finance database
\connect finance
GRANT CONNECT ON DATABASE finance TO mcp_readonly;
GRANT USAGE ON SCHEMA public TO mcp_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mcp_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO mcp_readonly;

-- Grant access to mylocation database
\connect mylocation
GRANT CONNECT ON DATABASE mylocation TO mcp_readonly;
GRANT USAGE ON SCHEMA public TO mcp_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mcp_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO mcp_readonly;

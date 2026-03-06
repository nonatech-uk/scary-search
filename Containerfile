FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy everything and install (source needed for hatch build)
COPY pyproject.toml .
COPY src/ src/
RUN uv pip install --system --no-cache .

ENTRYPOINT ["python", "-m"]
CMD ["mcp_search.postgres_mcp"]

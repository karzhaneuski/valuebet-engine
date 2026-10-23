# Multi-stage build; must run on linux/arm64 (Oracle Cloud ARM host, SPEC §9).
# Build (from repo root):
#   docker build --platform linux/arm64 -t valuebet-engine:dev .
FROM python:3.12-slim AS builder

# uv version pinned to match local development (uv.lock compatibility)
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv

# cffi has no linux wheels in our lock (sdist fallback via soccerdata ->
# selenium -> trio -> cffi), so the builder needs a compiler toolchain
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libffi-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Install dependencies first for layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy the rest of the application
COPY core/ core/
COPY api/ api/
COPY mcp_server/ mcp_server/
COPY dashboard/ dashboard/
COPY scripts/ scripts/
COPY alembic/ alembic/
COPY alembic.ini config.yaml ./

FROM python:3.12-slim

WORKDIR /app
# Non-root user; /app/data is the volume mountpoint for the SQLite database
RUN useradd --create-home --shell /usr/sbin/nologin valuebet \
    && mkdir -p /app/data && chown valuebet:valuebet /app/data
COPY --from=builder --chown=valuebet:valuebet /app /app
USER valuebet

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Default command is the API; compose overrides per service
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]

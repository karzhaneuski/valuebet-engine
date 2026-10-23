# Multi-stage build; must run on linux/arm64 (Oracle Cloud ARM host, SPEC §9).
# Build (from repo root):
#   docker build --platform linux/arm64 -t valuebet-engine:dev .
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Install dependencies first for layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy the rest of the application
COPY core/ core/
COPY api/ api/
COPY mcp/ mcp/
COPY dashboard/ dashboard/
COPY alembic/ alembic/
COPY alembic.ini config.yaml ./

FROM python:3.12-slim

WORKDIR /app
# Non-root user; the venv from the builder stage carries everything
RUN useradd --create-home --shell /usr/sbin/nologin valuebet
COPY --from=builder /app /app
USER valuebet

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Default command is the API; compose overrides per service
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]

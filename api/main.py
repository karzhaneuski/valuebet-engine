"""ValueBet Engine REST API (FastAPI).

SPEC v3 §9:
- Endpoints for matches, fair market prices, Fortuna evaluation, user bet tracking, performance.
- Bearer token authentication via API_SECRET_TOKEN on all endpoints except /health.
- Team disambiguation with candidate suggestions.
- Strict analytical framing without betting recommendations.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes.fortuna import router as fortuna_router
from api.routes.health import router as health_router
from api.routes.matches import router as matches_router
from api.routes.tracking import router as tracking_router
from core.http import register_runtime_secrets
from core.logging import configure, get_logger

configure()
register_runtime_secrets()
logger = get_logger("api")

enable_docs = os.environ.get("ENABLE_API_DOCS", "true").lower() in ("true", "1")

app = FastAPI(
    title="ValueBet Engine API",
    version="0.3.0",
    description="REST API for fair market pricing, Fortuna evaluation, and bet tracking (SPEC v3).",
    docs_url="/docs" if enable_docs else None,
    redoc_url="/redoc" if enable_docs else None,
    openapi_url="/openapi.json" if enable_docs else None,
)

# Enable CORS for dashboard / web clients
cors_env = os.environ.get("CORS_ORIGINS", "").strip()
if cors_env:
    allowed_origins = [o.strip() for o in cors_env.split(",") if o.strip()]
else:
    allowed_origins = ["http://localhost:8501", "http://127.0.0.1:8501"]

is_wildcard = "*" in allowed_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=not is_wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(matches_router)
app.include_router(fortuna_router)
app.include_router(tracking_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True)

"""ValueBet Engine REST API (FastAPI).

SPEC v3 §9:
- Endpoints for matches, fair market prices, Fortuna evaluation, user bet tracking, performance.
- Bearer token authentication via API_SECRET_TOKEN on all endpoints except /health.
- Team disambiguation with candidate suggestions.
- Strict analytical framing without betting recommendations.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes.fortuna import router as fortuna_router
from api.routes.health import router as health_router
from api.routes.matches import router as matches_router
from api.routes.tracking import router as tracking_router
from core.logging import configure, get_logger

configure()
logger = get_logger("api")

app = FastAPI(
    title="ValueBet Engine API",
    version="0.3.0",
    description="REST API for fair market pricing, Fortuna evaluation, and bet tracking (SPEC v3).",
)

# Enable CORS for dashboard / web clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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

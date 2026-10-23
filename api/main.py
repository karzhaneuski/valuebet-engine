"""REST API service (FastAPI). Phase 1: health-check stub only.

Real endpoints (value-bets, manual-odds, analysis, performance,
notifications) arrive in phase 8 per SPEC §9.
"""

from __future__ import annotations

from fastapi import FastAPI

from core.logging import configure, get_logger

configure()
logger = get_logger("api")

app = FastAPI(title="ValueBet Engine API", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    logger.info("health.check")
    return {"status": "ok"}

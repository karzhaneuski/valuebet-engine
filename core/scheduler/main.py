"""Scheduler service stub. Phase 1: health-check only.

APScheduler jobs (data refresh, odds snapshots, news windows, settlement)
arrive in phase 7 per SPEC §9.
"""

from __future__ import annotations

from fastapi import FastAPI

from core.logging import configure, get_logger

configure()
logger = get_logger("scheduler")

app = FastAPI(title="ValueBet Engine Scheduler", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    logger.info("health.check")
    return {"status": "ok", "service": "scheduler"}

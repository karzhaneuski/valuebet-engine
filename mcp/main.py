"""MCP server stub for Hermes. Phase 1: health-check only.

Real MCP tools (get_value_bets, evaluate_manual_odds, get_match_analysis,
get_performance, get_new_notifications) arrive in phase 8 per SPEC §9.
"""

from __future__ import annotations

from fastapi import FastAPI

from core.logging import configure, get_logger

configure()
logger = get_logger("mcp")

app = FastAPI(title="ValueBet Engine MCP", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    logger.info("health.check")
    return {"status": "ok", "service": "mcp"}

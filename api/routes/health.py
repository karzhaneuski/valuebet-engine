"""FastAPI public health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from api.db import get_db
from api.schemas import HealthResponse
from core.config import load_config
from core.http import redact_text
from core.news.search import get_current_period_utc
from core.orm import OddsSnapshot, QuotaCounter

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health_check(db: Session = Depends(get_db)) -> HealthResponse:
    """Public health-check endpoint: database connection, snapshot timestamp, and quota counters.

    Does NOT require authentication.
    """
    cfg = load_config()
    db_connected = False
    try:
        db.execute(text("SELECT 1"))
        db_connected = True
    except Exception:
        db_connected = False

    last_snap = db.scalar(select(func.max(OddsSnapshot.captured_at_utc)))

    curr_period = get_current_period_utc()
    toa_used = db.scalar(
        select(QuotaCounter.credits_used).where(
            QuotaCounter.provider == "toa",
            QuotaCounter.period == curr_period,
        )
    )
    toa_remaining = 500 - (toa_used or 0)

    # Search quotas
    tavily_used = db.scalar(
        select(QuotaCounter.credits_used).where(
            QuotaCounter.provider == "tavily",
            QuotaCounter.period == curr_period,
        )
    )
    brave_used = db.scalar(
        select(QuotaCounter.credits_used).where(
            QuotaCounter.provider == "brave",
            QuotaCounter.period == curr_period,
        )
    )

    return HealthResponse(
        status="ok",
        version="0.3.0",
        database_connected=db_connected,
        database_url_redacted=redact_text(cfg.database.url),
        last_odds_snapshot_utc=last_snap,
        toa_credits_remaining=toa_remaining,
        search_quota={
            "period": curr_period,
            "tavily_credits_used": tavily_used or 0,
            "brave_credits_used": brave_used or 0,
        },
    )

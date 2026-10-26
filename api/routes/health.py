"""FastAPI public health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from api.db import get_db
from api.schemas import HealthResponse
from core.config import load_config
from core.news.search import get_current_period_utc
from core.orm import Match, OddsSnapshot, QuotaCounter
from core.scheduler.pacing import evaluate_degradation_state

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health_check(db: Session = Depends(get_db)) -> HealthResponse:
    """Public health-check endpoint: database connection, snapshot timestamp, and quota counters.

    Does NOT require authentication.
    """
    cfg = load_config()
    db_connected = False
    status_str = "ok"

    try:
        db.execute(text("SELECT 1"))
        db_connected = True
    except Exception:
        db_connected = False
        status_str = "degraded"

    last_snap = None
    toa_remaining: int | None = None
    curr_period = get_current_period_utc()
    tavily_used: int = 0
    brave_used: int = 0
    pacing_stage: int = 0
    pacing_degraded: bool = False
    pacing_actions: list[str] = []

    if db_connected:
        try:
            # Check key tables accessibility
            _ = db.scalar(select(func.count(Match.id)))
            last_snap = db.scalar(select(func.max(OddsSnapshot.captured_at_utc)))
        except Exception:
            status_str = "degraded"
            last_snap = None

        try:
            toa_used = db.scalar(
                select(QuotaCounter.credits_used).where(
                    QuotaCounter.provider == "toa",
                    QuotaCounter.period == curr_period,
                )
            )
            monthly_quota = cfg.odds_policy.monthly_quota
            used_val = toa_used if toa_used is not None else 0
            toa_remaining = max(0, monthly_quota - used_val)

            # Evaluate pacing state from backend
            p_state = evaluate_degradation_state(db, config=cfg)
            active_deg = p_state.get("active_degradations")
            pacing_actions = [str(s) for s in active_deg] if isinstance(active_deg, list) else []
            hard_stop = bool(p_state.get("hard_stop_active", False))
            pacing_degraded = bool(pacing_actions) or hard_stop
            pacing_stage = 3 if hard_stop else len(pacing_actions)
        except Exception:
            # When quota calculation fails, return null (never 0)
            toa_remaining = None

        try:
            tav_val = db.scalar(
                select(QuotaCounter.credits_used).where(
                    QuotaCounter.provider == "tavily",
                    QuotaCounter.period == curr_period,
                )
            )
            tavily_used = tav_val or 0
        except Exception:
            tavily_used = 0

        try:
            brv_val = db.scalar(
                select(QuotaCounter.credits_used).where(
                    QuotaCounter.provider == "brave",
                    QuotaCounter.period == curr_period,
                )
            )
            brave_used = brv_val or 0
        except Exception:
            brave_used = 0

    return HealthResponse(
        status=status_str,
        version="0.3.0",
        database_connected=db_connected,
        database_url_redacted=None,
        last_odds_snapshot_utc=last_snap,
        toa_credits_remaining=toa_remaining,
        search_quota={
            "period": curr_period,
            "tavily_credits_used": tavily_used,
            "brave_credits_used": brave_used,
        },
        pacing_stage=pacing_stage,
        pacing_degraded=pacing_degraded,
        pacing_actions=pacing_actions,
    )

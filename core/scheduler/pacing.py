"""TOA quota pacing guard and degradation ladder logic (SPEC §5, decisions S3).

Features:
- Pacing projected burn: credits_used + spent_per_day * days_left
- Compares against hard_stop (450) and safety_margin (25)
- Dynamically applies steps from config.odds_policy.degradation_ladder:
    1. cap_correct_score_5_week
    2. disable_t3_h2h
    3. t3_slot_to_day
- Hard emergency stop prevents any billable calls when monthly quota cap is breached.
"""

from __future__ import annotations

import calendar
import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import AppConfig, DegradationStep, load_config
from core.orm import QuotaCounter

logger = logging.getLogger(__name__)


def get_current_month_days(now: dt.datetime | None = None) -> tuple[int, int, int]:
    """Return (current_day, total_days_in_month, days_left)."""
    current_dt = now or dt.datetime.now(dt.UTC)
    _, total_days = calendar.monthrange(current_dt.year, current_dt.month)
    day = current_dt.day
    days_left = max(0, total_days - day)
    return day, total_days, days_left


def get_toa_credits_used(session: Session, now: dt.datetime | None = None) -> int:
    """Query credits used for The Odds API in the current month."""
    current_dt = now or dt.datetime.now(dt.UTC)
    period = current_dt.strftime("%Y-%m")
    counter = session.scalar(
        select(QuotaCounter).where(
            QuotaCounter.provider == "toa",
            QuotaCounter.period == period,
        )
    )
    return counter.credits_used if counter else 0


def evaluate_degradation_state(
    session: Session,
    config: AppConfig | None = None,
    now: dt.datetime | None = None,
) -> dict[str, object]:
    """Evaluate current quota burn and determine active degradation steps from config."""
    cfg = config or load_config()
    odds_cfg = cfg.odds_policy
    pacing_cfg = odds_cfg.pacing

    credits_used = get_toa_credits_used(session, now=now)
    hard_stop = odds_cfg.hard_stop_credits  # 450
    safety_margin = pacing_cfg.safety_margin_credits  # 25

    if credits_used >= hard_stop:
        logger.warning(
            "EMERGENCY HARD STOP: TOA quota reached (%s >= %s). All paid snapshots disabled.",
            credits_used,
            hard_stop,
        )
        return {
            "hard_stop_active": True,
            "credits_used": credits_used,
            "active_degradations": list(odds_cfg.degradation_ladder),
            "effective_pinnacle_cs_cap": 0,
            "disable_t3_h2h": True,
            "t3_granularity": "day",
        }

    day, _total_days, days_left = get_current_month_days(now)
    daily_rate = credits_used / max(1, day)
    projected_burn = credits_used + (daily_rate * days_left)

    burn_threshold = hard_stop - safety_margin  # 425 credits
    degradation_needed = pacing_cfg.enabled and (projected_burn > burn_threshold)

    active_steps: list[DegradationStep] = []
    effective_cs_cap = odds_cfg.pinnacle_correct_score_max_weekly  # default 10
    disable_t3_h2h = False
    t3_granularity = odds_cfg.t3_granularity  # default "slot"

    if degradation_needed:
        # Apply ladder steps progressively based on burn severity
        ladder = odds_cfg.degradation_ladder
        # 1st step: cap correct score to 5/week
        if "cap_correct_score_5_week" in ladder:
            active_steps.append("cap_correct_score_5_week")
            effective_cs_cap = min(5, effective_cs_cap)

        # 2nd step: disable T-3 h2h if projection exceeds hard_stop - 10
        if projected_burn > (hard_stop - 10) and "disable_t3_h2h" in ladder:
            active_steps.append("disable_t3_h2h")
            disable_t3_h2h = True

        # 3rd step: degrade T-3 slot to day if projection exceeds hard_stop
        if projected_burn >= hard_stop and "t3_slot_to_day" in ladder:
            active_steps.append("t3_slot_to_day")
            t3_granularity = "day"

        logger.info(
            "Pacing triggered: used=%s, projected=%s > threshold=%s. Active degradations: %s",
            credits_used,
            projected_burn,
            burn_threshold,
            active_steps,
        )

    return {
        "hard_stop_active": False,
        "credits_used": credits_used,
        "projected_burn": round(projected_burn, 1),
        "active_degradations": active_steps,
        "effective_pinnacle_cs_cap": effective_cs_cap,
        "disable_t3_h2h": disable_t3_h2h,
        "t3_granularity": t3_granularity,
    }

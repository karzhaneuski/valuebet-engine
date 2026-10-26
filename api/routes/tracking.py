"""FastAPI routes for user bet tracking and performance analytics."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.auth import verify_bearer_token
from api.db import get_db
from api.schemas import (
    BootstrapCI,
    PerformanceResponse,
    UserBetCreate,
    UserBetItem,
    UserBetListResponse,
)
from core.orm import Match, UserBet
from core.tracking.stats import compute_performance_metrics

router = APIRouter(tags=["tracking"])

DISCLAIMER_TEXT = (
    "Сервис предоставляет исключительно справочно-аналитическую информацию. "
    "Ставки на спорт сопряжены с риском потери средств. "
    "Инструмент не даёт гарантий доходности. "
    "Прошлые результаты не гарантируют будущих."
)


def _bet_to_item(bet: UserBet, db: Session) -> UserBetItem:
    m = db.get(Match, bet.match_id)
    if m:
        h = m.home_team.name if m.home_team else f"Team_{m.home_team_id}"
        a = m.away_team.name if m.away_team else f"Team_{m.away_team_id}"
        m_name = f"{h} - {a}"
    else:
        m_name = f"Match_{bet.match_id}"

    return UserBetItem(
        id=bet.id,
        match_id=bet.match_id,
        match_name=m_name,
        market=bet.market,
        outcome=bet.selection_key,
        line=float(bet.line) if bet.line is not None else None,
        odds=float(bet.odds),
        stake=float(bet.stake_units),
        bookmaker=bet.bookmaker,
        status=bet.status,
        pnl=float(bet.pnl_units) if bet.pnl_units is not None else None,
        clv=float(bet.clv_fair) if bet.clv_fair is not None else None,
        clv_source=bet.clv_source,
        placed_at_utc=bet.placed_at_utc,
        settled_at_utc=bet.settled_at_utc,
    )


@router.post("/user-bets", response_model=UserBetItem, status_code=status.HTTP_201_CREATED)
def record_user_bet(
    request: UserBetCreate,
    db: Session = Depends(get_db),
    auth_token: str = Depends(verify_bearer_token),
) -> UserBetItem:
    """Record a bet placed by the user."""
    match = db.get(Match, request.match_id)
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Матч с ID {request.match_id} не найден в базе данных",
        )

    now = dt.datetime.now(dt.UTC)
    bet = UserBet(
        match_id=request.match_id,
        market=request.market,
        selection_key=request.outcome,
        line=Decimal(str(request.line)) if request.line is not None else None,
        odds=Decimal(str(request.odds)),
        stake_units=Decimal(str(request.stake)),
        bookmaker=request.bookmaker,
        status="open",
        placed_at_utc=request.placed_at_utc or now,
        created_at_utc=now,
    )
    db.add(bet)
    db.flush()
    db.refresh(bet)

    return _bet_to_item(bet, db)


@router.get("/user-bets", response_model=UserBetListResponse)
def list_user_bets(
    status_filter: str | None = Query(
        None, alias="status", description="Filter by status (open, won, lost, void, push)"
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    auth_token: str = Depends(verify_bearer_token),
) -> UserBetListResponse:
    """List tracked user bets with pagination, settlement status, and CLV."""
    stmt = select(UserBet)
    if status_filter:
        stmt = stmt.where(UserBet.status == status_filter)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    stmt = stmt.order_by(UserBet.placed_at_utc.desc()).offset(offset).limit(limit)
    bets = list(db.scalars(stmt).all())

    items = [_bet_to_item(b, db) for b in bets]
    return UserBetListResponse(bets=items, total=total)


@router.get("/performance", response_model=PerformanceResponse)
def get_user_performance(
    period: str = Query("all", description="Period filter (all, month, season)"),
    db: Session = Depends(get_db),
    auth_token: str = Depends(verify_bearer_token),
) -> PerformanceResponse:
    """Calculate transparent performance metrics (P/L, ROI, average CLV, 95% bootstrap CIs).

    Strictly displays negative ROI and sample size warnings when sample size < 100 bets.
    """
    stmt = select(UserBet)
    now = dt.datetime.now(dt.UTC)

    if period == "month":
        month_ago = now - dt.timedelta(days=30)
        stmt = stmt.where(UserBet.placed_at_utc >= month_ago)
    elif period == "season":
        curr_year = now.year if now.month >= 7 else now.year - 1
        season_start = dt.datetime(curr_year, 7, 1, tzinfo=dt.UTC)
        stmt = stmt.where(UserBet.placed_at_utc >= season_start)

    stmt = stmt.order_by(UserBet.placed_at_utc.asc())
    bets = list(db.scalars(stmt).all())

    metrics = compute_performance_metrics(bets)

    ci_roi = (
        BootstrapCI(lower=metrics.roi_ci_95[0], upper=metrics.roi_ci_95[1])
        if metrics.roi_ci_95
        else None
    )
    ci_clv = (
        BootstrapCI(lower=metrics.clv_ci_95[0], upper=metrics.clv_ci_95[1])
        if metrics.clv_ci_95
        else None
    )

    return PerformanceResponse(
        total_bets=metrics.total_bets,
        settled_bets=metrics.settled_bets,
        pending_bets=metrics.open_bets,
        turnover=float(metrics.total_stake),
        total_profit=float(metrics.total_pnl),
        roi=metrics.roi,
        hit_rate=metrics.hit_rate,
        avg_clv=metrics.avg_clv or 0.0,
        clv_positive_share=metrics.positive_clv_share or 0.0,
        brier_score=metrics.brier_score,
        bootstrap_ci_roi_95=ci_roi,
        bootstrap_ci_clv_95=ci_clv,
        sample_size_warning=metrics.sample_size_warning,
        disclaimer=DISCLAIMER_TEXT,
    )

"""Standby Dixon-Coles model integration & lineup risk checks (SPEC §5, §6).

Features:
1. Dynamic fitting of Dixon-Coles on historical matches up to cutoff (with result_lag_minutes).
2. Calculation of model home share for anomaly checking against the fair market matrix.
3. Reference-only score matrix generation when market is unavailable.
4. Key players detection from match_lineups (>=70% starts in last 8 matches).
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.config import AppConfig, load_config
from core.orm import Match, MatchLineup

if TYPE_CHECKING:
    from core.model.dixon_coles import FittedModel

logger = logging.getLogger(__name__)


def get_model_home_share_and_model(
    db_url: str,
    league: str,
    as_of: dt.datetime,
    home_team: str,
    away_team: str,
    config: AppConfig | None = None,
) -> tuple[float | None, FittedModel | None]:
    """Dynamically train Dixon-Coles on historical matches before as_of and return rates share.

    Strictly reference-only: Dixon-Coles does not generate value bet recommendations,
    it only serves as an anomaly flag (alpha = 0) or standby when no market exists.
    """
    from core.model.dixon_coles import fit_league

    cfg = config or load_config()

    # Ensure as_of is timezone-aware UTC
    as_of_utc = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=dt.UTC)

    try:
        model, _stats = fit_league(
            db_url=db_url,
            league=league,
            as_of=as_of_utc,
            config=cfg.model,
        )
        lh, la = model._rates(home_team, away_team)
        lt = lh + la
        share = float(lh / lt) if lt > 0.0 else 0.5
        return share, model
    except Exception as err:
        logger.warning(
            "Could not fit standby Dixon-Coles model for %s (%s vs %s): %s",
            league,
            home_team,
            away_team,
            err,
        )
        return None, None


def get_key_players_for_team(
    session: Session,
    team_id: int,
    before_date: dt.date,
    last_n: int = 8,
    min_starts_share: float = 0.70,
) -> set[str]:
    """Query match_lineups to identify key players (>= 70% starts in last 8 matches).

    Per SPEC §6 and decisions S10:
    - Queries the last N completed matches for team_id before before_date.
    - Counts starts per player.
    - Returns set of player names meeting the threshold.
    """
    stmt_matches = (
        select(Match.id)
        .where(
            (Match.home_team_id == team_id) | (Match.away_team_id == team_id),
            Match.date_utc < before_date,
            Match.status == "finished",
        )
        .order_by(Match.date_utc.desc())
        .limit(last_n)
    )
    match_ids = list(session.scalars(stmt_matches).all())
    if not match_ids:
        return set()

    stmt_starts = (
        select(MatchLineup.player, func.count(MatchLineup.id))
        .where(
            MatchLineup.match_id.in_(match_ids),
            MatchLineup.team_id == team_id,
            MatchLineup.started.is_(True),
        )
        .group_by(MatchLineup.player)
    )
    min_starts = math.ceil(len(match_ids) * min_starts_share)
    key_players = {
        player for player, cnt in session.execute(stmt_starts) if cnt >= min_starts
    }
    return key_players

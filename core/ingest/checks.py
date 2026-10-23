"""Invariant check for the rebuilt database (review B5/R).

No match with a FUTURE kickoff may carry match_stats, closing_odds or a
score — data about the future cannot exist yet.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import exists, select

from core.db import session_scope
from core.orm import ClosingOdds, Match, MatchStat, Result


def future_kickoff_data_violations(db_url: str,
                                   now: dt.datetime | None = None
                                   ) -> list[str]:
    now = now or dt.datetime.now(dt.UTC)
    violations: list[str] = []
    with session_scope(db_url) as s:
        future = select(Match).where(Match.commence_time_utc > now)
        for m in s.scalars(future).all():
            if s.scalar(select(exists().where(
                    MatchStat.match_id == m.id))):
                violations.append(f"match#{m.id}: future kickoff has stats")
            if s.scalar(select(exists().where(
                    ClosingOdds.match_id == m.id))):
                violations.append(
                    f"match#{m.id}: future kickoff has closing odds")
            if s.scalar(select(exists().where(Result.match_id == m.id))):
                violations.append(f"match#{m.id}: future kickoff has result")
    return violations

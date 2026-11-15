"""Populate Result rows from MatchStat for finished matches."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.orm import Match, MatchStat, Result


def populate_results(
    session: Session,
    touched_match_ids: set[int] | None = None,
    now: dt.datetime | None = None,
) -> tuple[int, int]:
    """Populate Result rows from MatchStat for finished matches.

    Updates Match.status to 'finished' where a result exists.
    Returns (inserted, updated).
    """
    curr_now = now or dt.datetime.now(dt.UTC)
    results_inserted = 0
    results_updated = 0
    touched = touched_match_ids if touched_match_ids is not None else set()

    stats = list(
        session.scalars(
            select(MatchStat).where(
                MatchStat.home_goals.is_not(None),
                MatchStat.away_goals.is_not(None),
            )
        ).all()
    )
    for st in stats:
        res = session.scalar(select(Result).where(Result.match_id == st.match_id))
        if res is None:
            session.add(
                Result(
                    match_id=st.match_id,
                    home_goals=st.home_goals,
                    away_goals=st.away_goals,
                    source=st.source,
                    settled_at_utc=curr_now,
                )
            )
            results_inserted += 1
        elif st.match_id in touched:
            assert st.home_goals is not None and st.away_goals is not None
            new_home = int(st.home_goals)
            new_away = int(st.away_goals)
            score_changed = res.home_goals != new_home or res.away_goals != new_away
            source_changed = res.source != st.source
            if score_changed or source_changed:
                res.home_goals = new_home
                res.away_goals = new_away
                res.source = st.source
                res.settled_at_utc = curr_now
                results_updated += 1

    scheduled_matches = list(
        session.scalars(
            select(Match).where(Match.status.in_(["scheduled", "postponed_suspected"]))
        ).all()
    )
    for match in scheduled_matches:
        res = session.scalar(select(Result).where(Result.match_id == match.id))
        if res is not None and res.home_goals is not None and res.away_goals is not None:
            match.status = "finished"

    return results_inserted, results_updated

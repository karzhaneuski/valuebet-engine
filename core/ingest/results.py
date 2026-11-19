"""Populate Result rows from MatchStat for finished matches."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.orm import Match, MatchStat, Result

_CHUNK_SIZE = 500


def populate_results(
    session: Session,
    touched_match_ids: set[int] | None = None,
    now: dt.datetime | None = None,
) -> tuple[int, int]:
    """Populate Result rows from MatchStat for finished matches.

    Updates Match.status to 'finished' where a result exists.
    Returns (inserted, updated).

    When a match has multiple MatchStat rows with non-null goals (e.g. from
    different sources), rows are ordered by ``MatchStat.id`` ascending:
    - If no Result exists yet, the first row creates the Result; if the match is
      also in ``touched_match_ids``, subsequent rows may update it if their
      score or source differs, so the last row (highest ``id``) wins.
    - If a Result already exists and the match is in ``touched_match_ids``, each
      row is applied in ``id`` order, counting each change and leaving the last
      row's values in place.
    """
    curr_now = now or dt.datetime.now(dt.UTC)
    results_inserted = 0
    results_updated = 0
    touched = touched_match_ids if touched_match_ids is not None else set()

    # 1. Insert missing results: one query selecting MatchStat rows with a score
    # that have no Result yet.
    missing_stats = list(
        session.scalars(
            select(MatchStat)
            .outerjoin(Result, Result.match_id == MatchStat.match_id)
            .where(
                MatchStat.home_goals.is_not(None),
                MatchStat.away_goals.is_not(None),
                Result.id.is_(None),
            )
            .order_by(MatchStat.id.asc())
        ).all()
    )

    new_results_by_match: dict[int, Result] = {}
    for st in missing_stats:
        assert st.home_goals is not None and st.away_goals is not None
        new_home = int(st.home_goals)
        new_away = int(st.away_goals)
        existing = new_results_by_match.get(st.match_id)
        if existing is None:
            new_results_by_match[st.match_id] = Result(
                match_id=st.match_id,
                home_goals=new_home,
                away_goals=new_away,
                source=st.source,
                settled_at_utc=curr_now,
            )
            results_inserted += 1
        elif st.match_id in touched:
            score_changed = existing.home_goals != new_home or existing.away_goals != new_away
            source_changed = existing.source != st.source
            if score_changed or source_changed:
                existing.home_goals = new_home
                existing.away_goals = new_away
                existing.source = st.source
                existing.settled_at_utc = curr_now
                results_updated += 1

    # 2. Update changed results: only for match ids in touched_match_ids that
    # already had a Result before step 1. Chunk by 500 to stay under SQLite's
    # parameter limit.
    existing_touched_ids = sorted(touched - new_results_by_match.keys())
    for i in range(0, len(existing_touched_ids), _CHUNK_SIZE):
        chunk = existing_touched_ids[i : i + _CHUNK_SIZE]
        rows = session.execute(
            select(MatchStat, Result)
            .join(Result, Result.match_id == MatchStat.match_id)
            .where(
                MatchStat.match_id.in_(chunk),
                MatchStat.home_goals.is_not(None),
                MatchStat.away_goals.is_not(None),
            )
            .order_by(MatchStat.id.asc())
        ).all()
        for st, res in rows:
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

    if new_results_by_match:
        max_id = session.scalar(select(func.max(Result.id))) or 0
        for offset, res_obj in enumerate(new_results_by_match.values(), start=1):
            res_obj.id = max_id + offset
        session.add_all(new_results_by_match.values())

    # 3. Status update: one query selecting scheduled / postponed_suspected
    # matches that have a Result with both goals not null.
    matches_to_finish = list(
        session.scalars(
            select(Match)
            .join(Result, Result.match_id == Match.id)
            .where(
                Match.status.in_(["scheduled", "postponed_suspected"]),
                Result.home_goals.is_not(None),
                Result.away_goals.is_not(None),
            )
        ).all()
    )
    for match in matches_to_finish:
        match.status = "finished"

    return results_inserted, results_updated


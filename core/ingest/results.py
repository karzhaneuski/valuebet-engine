"""Populate Result rows from MatchStat for finished matches."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.orm import Match, MatchStat, Result

_CHUNK_SIZE = 500
SOURCE_PRIORITY: tuple[str, ...] = ("football_data", "understat")
_SOURCE_RANK = {src: idx for idx, src in enumerate(SOURCE_PRIORITY)}


def _stat_sort_key(st: MatchStat) -> tuple[int, int]:
    return (_SOURCE_RANK.get(st.source, len(SOURCE_PRIORITY)), st.id)


def _select_winner(stats: list[MatchStat]) -> MatchStat:
    return min(stats, key=_stat_sort_key)


def populate_results(
    session: Session,
    touched_match_ids: set[int] | None = None,
    now: dt.datetime | None = None,
) -> tuple[int, int]:
    """Populate Result rows from MatchStat for finished matches.

    Updates Match.status to 'finished' where a result exists.
    Returns (inserted, updated).

    When a match has multiple MatchStat rows with non-null goals, exactly one
    winning row is chosen per match according to ``SOURCE_PRIORITY``
    (``("football_data", "understat")``, where football-data is the official
    result feed and Understat is the fallback). Unknown sources rank after
    listed ones, and ties are broken by the lowest ``MatchStat.id``.
    - Insert: a missing Result is created from the winning MatchStat row.
    - Update: for matches in ``touched_match_ids`` that already have a Result,
      the existing Result is compared against the winning MatchStat row only,
      performing at most one update per match when the score or source differs.
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

    missing_by_match: dict[int, list[MatchStat]] = {}
    for st in missing_stats:
        missing_by_match.setdefault(st.match_id, []).append(st)

    new_results_by_match: dict[int, Result] = {}
    for match_id, stats in missing_by_match.items():
        winner = _select_winner(stats)
        assert winner.home_goals is not None and winner.away_goals is not None
        new_results_by_match[match_id] = Result(
            match_id=match_id,
            home_goals=int(winner.home_goals),
            away_goals=int(winner.away_goals),
            source=winner.source,
            settled_at_utc=curr_now,
        )
        results_inserted += 1

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
        grouped: dict[int, tuple[Result, list[MatchStat]]] = {}
        for st, res in rows:
            entry = grouped.get(st.match_id)
            if entry is None:
                grouped[st.match_id] = (res, [st])
            else:
                entry[1].append(st)

        for res, stats in grouped.values():
            winner = _select_winner(stats)
            assert winner.home_goals is not None and winner.away_goals is not None
            new_home = int(winner.home_goals)
            new_away = int(winner.away_goals)
            score_changed = res.home_goals != new_home or res.away_goals != new_away
            source_changed = res.source != winner.source
            if score_changed or source_changed:
                res.home_goals = new_home
                res.away_goals = new_away
                res.source = winner.source
                res.settled_at_utc = curr_now
                results_updated += 1

    if new_results_by_match:
        session.add_all(new_results_by_match.values())

    # 3. Status update: one query selecting scheduled / postponed_suspected
    # matches that have a Result with both goals not null.
    with session.no_autoflush:
        matches_to_finish = list(
            session.scalars(
                select(Match)
                .join(MatchStat, MatchStat.match_id == Match.id)
                .where(
                    Match.status.in_(["scheduled", "postponed_suspected"]),
                    MatchStat.home_goals.is_not(None),
                    MatchStat.away_goals.is_not(None),
                )
                .distinct()
            ).all()
        )
    for match in matches_to_finish:
        match.status = "finished"

    return results_inserted, results_updated


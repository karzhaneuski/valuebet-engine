"""Tests for bulk populate_results implementation."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from core.ingest.results import populate_results
from core.orm import Base, Match, MatchStat, Result, Team

FIXED_NOW = dt.datetime(2026, 11, 15, 12, 0, tzinfo=dt.UTC)
LATER_NOW = dt.datetime(2026, 11, 16, 12, 0, tzinfo=dt.UTC)


def make_test_db(tmp_path: Path) -> tuple[Engine, sessionmaker[Session]]:
    db_file = tmp_path / "test_populate_results.db"
    url = f"sqlite:///{db_file}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def seed_matches_and_stats(
    session: Session,
    count: int,
    *,
    with_stats: bool = True,
    status: str = "scheduled",
    prefix: str = "Team",
    understat_base: int = 10000,
) -> list[int]:
    teams = [Team(name=f"{prefix}_{i}", league="la_liga") for i in range(count * 2)]
    session.add_all(teams)
    session.flush()

    matches = [
        Match(
            league="la_liga",
            season=2026,
            date_utc=dt.date(2026, 11, 1),
            kickoff_source="understat_placeholder",
            status=status,
            home_team_id=teams[2 * i].id,
            away_team_id=teams[2 * i + 1].id,
            understat_id=understat_base + i,
        )
        for i in range(count)
    ]
    session.add_all(matches)
    session.flush()

    if with_stats:
        stats = [
            MatchStat(
                match_id=m.id,
                source="understat",
                home_goals=i % 4,
                away_goals=(i + 1) % 3,
            )
            for i, m in enumerate(matches)
        ]
        session.add_all(stats)
        session.flush()

    match_ids = [m.id for m in matches]
    session.commit()
    return match_ids


def test_300_matches_insert_and_idempotent_second_call(tmp_path: Path) -> None:
    """a) 300 matches with MatchStat and no Result -> 300 inserted, 0 updated;
    second call -> (0, 0)."""
    _, sm = make_test_db(tmp_path)
    with sm() as session:
        seed_matches_and_stats(session, 300)

    with sm() as session:
        ins1, upd1 = populate_results(session, now=FIXED_NOW)
        session.commit()
        assert (ins1, upd1) == (300, 0)

        total_results = len(session.scalars(select(Result)).all())
        assert total_results == 300

    with sm() as session:
        ins2, upd2 = populate_results(session, now=LATER_NOW)
        session.commit()
        assert (ins2, upd2) == (0, 0)


def test_touched_ids_updates_only_touched_with_changed_score_or_source(tmp_path: Path) -> None:
    """b) touched ids with changed score -> updated only those;
    untouched ids with changed score -> not updated."""
    _, sm = make_test_db(tmp_path)
    with sm() as session:
        match_ids = seed_matches_and_stats(session, 5)
        ins, upd = populate_results(session, now=FIXED_NOW)
        session.commit()
        assert (ins, upd) == (5, 0)

    # Modify scores for match 0 (touched), match 1 (untouched), source for match 2 (touched),
    # and leave match 3 (touched) unchanged.
    with sm() as session:
        st0 = session.scalar(select(MatchStat).where(MatchStat.match_id == match_ids[0]))
        st1 = session.scalar(select(MatchStat).where(MatchStat.match_id == match_ids[1]))
        st2 = session.scalar(select(MatchStat).where(MatchStat.match_id == match_ids[2]))
        assert st0 is not None and st1 is not None and st2 is not None
        st0.home_goals = 9
        st1.home_goals = 8
        st2.source = "football_data"
        session.commit()

    touched = {match_ids[0], match_ids[2], match_ids[3]}
    with sm() as session:
        ins2, upd2 = populate_results(session, touched_match_ids=touched, now=LATER_NOW)
        session.commit()
        assert (ins2, upd2) == (0, 2)

        r0 = session.scalar(select(Result).where(Result.match_id == match_ids[0]))
        r1 = session.scalar(select(Result).where(Result.match_id == match_ids[1]))
        r2 = session.scalar(select(Result).where(Result.match_id == match_ids[2]))
        r3 = session.scalar(select(Result).where(Result.match_id == match_ids[3]))
        assert r0 is not None and r1 is not None and r2 is not None and r3 is not None

        assert r0.home_goals == 9
        assert r0.settled_at_utc == LATER_NOW.replace(tzinfo=None) or r0.settled_at_utc == LATER_NOW

        # Untouched match 1 kept its initial score
        assert r1.home_goals == 1
        assert r1.settled_at_utc == FIXED_NOW.replace(tzinfo=None) or r1.settled_at_utc == FIXED_NOW

        # Touched match 2 had source updated
        assert r2.source == "football_data"
        assert r2.settled_at_utc == LATER_NOW.replace(tzinfo=None) or r2.settled_at_utc == LATER_NOW

        # Touched match 3 had no change
        assert r3.settled_at_utc == FIXED_NOW.replace(tzinfo=None) or r3.settled_at_utc == FIXED_NOW


def test_scheduled_match_status_transitions(tmp_path: Path) -> None:
    """c) scheduled match with a result becomes 'finished';
    scheduled match without a result stays 'scheduled'."""
    _, sm = make_test_db(tmp_path)
    with sm() as session:
        with_res_ids = seed_matches_and_stats(
            session, 2, with_stats=True, status="scheduled", prefix="Sched", understat_base=10000
        )
        postponed_ids = seed_matches_and_stats(
            session,
            1,
            with_stats=True,
            status="postponed_suspected",
            prefix="Postp",
            understat_base=20000,
        )
        # Create teams/matches without stats
        t_a = Team(name="NoResHome", league="la_liga")
        t_b = Team(name="NoResAway", league="la_liga")
        session.add_all([t_a, t_b])
        session.flush()
        m_no_res = Match(
            league="la_liga",
            season=2026,
            date_utc=dt.date(2026, 11, 2),
            kickoff_source="understat_placeholder",
            status="scheduled",
            home_team_id=t_a.id,
            away_team_id=t_b.id,
            understat_id=99999,
        )
        session.add(m_no_res)
        session.flush()
        # Also a MatchStat row with null goals
        session.add(
            MatchStat(
                match_id=m_no_res.id,
                source="understat",
                home_goals=None,
                away_goals=None,
            )
        )
        no_res_id = m_no_res.id
        session.commit()

    with sm() as session:
        ins, upd = populate_results(session, now=FIXED_NOW)
        session.commit()
        assert (ins, upd) == (3, 0)

        for mid in with_res_ids + postponed_ids:
            m = session.get(Match, mid)
            assert m is not None
            assert m.status == "finished"

        m_stay = session.get(Match, no_res_id)
        assert m_stay is not None
        assert m_stay.status == "scheduled"


def test_query_count_under_20_for_300_matches(tmp_path: Path) -> None:
    """d) count executed SQL statements with a SQLAlchemy 'before_cursor_execute'
    event listener and assert populate_results on 300 matches issues fewer than 20 statements."""
    engine, sm = make_test_db(tmp_path)
    with sm() as session:
        match_ids = seed_matches_and_stats(session, 300)

    statements: list[str] = []

    def _before_cursor_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: Any,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    try:
        with sm() as session:
            ins, upd = populate_results(
                session,
                touched_match_ids=set(match_ids),
                now=FIXED_NOW,
            )
            assert (ins, upd) == (300, 0)
    finally:
        event.remove(engine, "before_cursor_execute", _before_cursor_execute)

    assert len(statements) < 20, f"Expected < 20 SQL statements, got {len(statements)}"


def test_more_than_500_touched_ids_chunking(tmp_path: Path) -> None:
    """e) more than 500 touched ids works (chunking)."""
    _, sm = make_test_db(tmp_path)
    with sm() as session:
        match_ids = seed_matches_and_stats(session, 1200)
        ins, upd = populate_results(session, now=FIXED_NOW)
        session.commit()
        assert (ins, upd) == (1200, 0)

    # Update score on 3 matches spread across chunks
    changed_ids = {match_ids[0], match_ids[600], match_ids[1199]}
    with sm() as session:
        for mid in changed_ids:
            st = session.scalar(select(MatchStat).where(MatchStat.match_id == mid))
            assert st is not None
            st.home_goals = 7
            st.away_goals = 7
        session.commit()

    with sm() as session:
        ins2, upd2 = populate_results(
            session,
            touched_match_ids=set(match_ids),
            now=LATER_NOW,
        )
        session.commit()
        assert (ins2, upd2) == (0, 3)

        for mid in changed_ids:
            r = session.scalar(select(Result).where(Result.match_id == mid))
            assert r is not None
            assert (r.home_goals, r.away_goals) == (7, 7)


def test_multiple_match_stat_rows_per_match_deterministic(tmp_path: Path) -> None:
    """When a match has both sources with different scores, football_data wins;
    a match with only understat gets the understat score; a second call with the
    same rows and the match in touched_match_ids returns (0, 0) and does not
    change settled_at_utc."""
    _, sm = make_test_db(tmp_path)
    with sm() as session:
        match_ids = seed_matches_and_stats(session, 2, with_stats=False)
        m1, m2 = match_ids[0], match_ids[1]

        session.add_all(
            [
                MatchStat(match_id=m1, source="understat", home_goals=1, away_goals=0),
                MatchStat(match_id=m1, source="football_data", home_goals=2, away_goals=1),
                MatchStat(match_id=m2, source="understat", home_goals=3, away_goals=2),
            ]
        )
        session.commit()

    with sm() as session:
        ins, upd = populate_results(session, touched_match_ids={m1, m2}, now=FIXED_NOW)
        session.commit()
        assert (ins, upd) == (2, 0)

        r1 = session.scalar(select(Result).where(Result.match_id == m1))
        r2 = session.scalar(select(Result).where(Result.match_id == m2))
        assert r1 is not None and r2 is not None
        assert (r1.home_goals, r1.away_goals, r1.source) == (2, 1, "football_data")
        assert (r2.home_goals, r2.away_goals, r2.source) == (3, 2, "understat")
        initial_settled_1 = r1.settled_at_utc
        initial_settled_2 = r2.settled_at_utc

    with sm() as session:
        ins2, upd2 = populate_results(session, touched_match_ids={m1, m2}, now=LATER_NOW)
        session.commit()
        assert (ins2, upd2) == (0, 0)

        r1_after = session.scalar(select(Result).where(Result.match_id == m1))
        r2_after = session.scalar(select(Result).where(Result.match_id == m2))
        assert r1_after is not None and r2_after is not None
        assert (r1_after.home_goals, r1_after.away_goals, r1_after.source) == (
            2,
            1,
            "football_data",
        )
        assert r1_after.settled_at_utc == initial_settled_1
        assert r2_after.settled_at_utc == initial_settled_2


def test_separate_sessions_insert_distinct_ids(tmp_path: Path) -> None:
    """Two populate_results calls on separate sessions inserting results for
    different matches both succeed with distinct autoincrement ids."""
    _, sm = make_test_db(tmp_path)
    with sm() as session:
        batch1_ids = seed_matches_and_stats(
            session, 3, with_stats=True, prefix="BatchA", understat_base=10000
        )
        batch2_ids = seed_matches_and_stats(
            session, 4, with_stats=False, prefix="BatchB", understat_base=20000
        )

    with sm() as session1:
        ins1, upd1 = populate_results(session1, touched_match_ids=set(batch1_ids), now=FIXED_NOW)
        session1.commit()
        assert (ins1, upd1) == (3, 0)

    with sm() as session_seed:
        session_seed.add_all(
            [
                MatchStat(
                    match_id=mid,
                    source="football_data",
                    home_goals=2,
                    away_goals=0,
                )
                for mid in batch2_ids
            ]
        )
        session_seed.commit()

    with sm() as session2:
        ins2, upd2 = populate_results(session2, touched_match_ids=set(batch2_ids), now=LATER_NOW)
        session2.commit()
        assert (ins2, upd2) == (4, 0)

    with sm() as verify_session:
        all_results = list(verify_session.scalars(select(Result)).all())
        assert len(all_results) == 7
        result_ids = [r.id for r in all_results]
        assert all(rid is not None for rid in result_ids)
        assert len(set(result_ids)) == 7
        assert {r.match_id for r in all_results} == set(batch1_ids + batch2_ids)

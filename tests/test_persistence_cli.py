"""Tests for recommendations persistence, market loading, and CLI commands."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from core.orm import Base, Match, PrefilterDecision, PrematchOdds, Recommendation, Team
from core.value.__main__ import main
from core.value.engine import (
    EvaluatedOutcome,
    EvaluationResult,
)
from core.value.fortuna_input import FortunaPrice
from core.value.persistence import (
    find_matches_by_query,
    load_market_inputs_for_match,
    save_recommendations_to_db,
)


def _setup_test_db() -> tuple[Session, int]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)

    # Teams
    t_home = Team(id=1, name="Arsenal", league="premier_league")
    t_away = Team(id=2, name="Everton", league="premier_league")
    session.add_all([t_home, t_away])
    session.flush()

    now = dt.datetime.now(dt.UTC)
    future_time = now + dt.timedelta(days=3)

    # Match
    m = Match(
        id=101,
        league="premier_league",
        season=2026,
        date_utc=future_time.date(),
        commence_time_utc=future_time,
        kickoff_source="toa",
        status="scheduled",
        home_team_id=1,
        away_team_id=2,
    )
    session.add(m)
    session.flush()

    # Prematch odds (Pinnacle)
    p_odds = PrematchOdds(
        id=1,
        match_id=101,
        bookmaker="P",
        over_price=Decimal("1.85"),
        under_price=Decimal("2.05"),
        home_price=Decimal("1.50"),
        draw_price=Decimal("4.50"),
        away_price=Decimal("6.50"),
    )
    session.add(p_odds)
    session.commit()
    return session, 101


def test_load_market_inputs_for_match() -> None:
    session, match_id = _setup_test_db()
    inputs = load_market_inputs_for_match(session, match_id)

    assert inputs.source == "fd_prematch"
    assert "pinnacle" in inputs.totals_by_book
    assert "pinnacle" in inputs.h2h_by_book
    assert inputs.totals_by_book["pinnacle"][0].line == 2.5
    assert inputs.h2h_by_book["pinnacle"].home == 1.50


def test_find_matches_by_query() -> None:
    session, _ = _setup_test_db()

    # Exact match
    matches = find_matches_by_query(session, "Arsenal - Everton")
    assert len(matches) == 1
    assert matches[0].id == 101

    # Case insensitive substring
    matches_case = find_matches_by_query(session, "arsenal vs everton")
    assert len(matches_case) == 1

    # Non-existent
    matches_none = find_matches_by_query(session, "Chelsea - Spurs")
    assert len(matches_none) == 0


def test_save_recommendations_idempotency() -> None:
    session, match_id = _setup_test_db()

    # Build dummy EvaluationResult with 1 recommended and 1 rejected outcome
    raw1 = FortunaPrice("exact_score", (2, 1), 9.50, "2:1", 1)
    raw2 = FortunaPrice("exact_score", (1, 0), 6.00, "1:0", 2)

    out1 = EvaluatedOutcome(
        market="exact_score",
        selection_display="2:1",
        selection_key="score:2:1",
        odds_fortuna=9.50,
        p_fair=0.15,
        fair_odds=6.67,
        edge=0.425,
        recommended=True,
        rejection_reason="",
        stake_flat=1.0,
        kelly_quarter_capped=0.015,
        flags=["value"],
        raw_price=raw1,
    )
    out2 = EvaluatedOutcome(
        market="exact_score",
        selection_display="1:0",
        selection_key="score:1:0",
        odds_fortuna=6.00,
        p_fair=0.10,
        fair_odds=10.0,
        edge=-0.40,
        recommended=False,
        rejection_reason="Ниже порога",
        stake_flat=0.0,
        kelly_quarter_capped=0.0,
        flags=[],
        raw_price=raw2,
    )

    res = EvaluationResult(
        match_name="Arsenal - Everton",
        snapshot_time=dt.datetime(2026, 10, 24, 12, 0),
        source="fd_prematch",
        books_used=["pinnacle"],
        input_hash="hash_12345",
        fortuna_overround=1.12,
        outcomes=[out1, out2],
        risk_flags=[],
        model_anomaly=False,
        is_excluded=False,
        recommendations_count=1,
    )

    # 1. First save
    saved = save_recommendations_to_db(session, match_id, res)
    session.commit()
    assert saved == 1

    # Verify DB row
    rows = list(session.scalars(select(Recommendation)).all())
    assert len(rows) == 1
    rec = rows[0]
    assert rec.match_id == match_id
    assert rec.market == "exact_score"
    assert rec.selection_key == "score:2:1"
    assert rec.status == "active"
    assert rec.outcome == "2:1"
    assert rec.odds == Decimal("9.500")
    assert rec.variant == "base"
    assert rec.p_model is None  # Pure market matrix has p_model = None
    assert rec.p_market_devig == Decimal("0.1500")
    assert rec.final_odds_source == "fortuna_manual"
    assert rec.input_hash == "hash_12345"
    assert rec.matrix_source == "fd_prematch"

    # 2. Re-run save (idempotency: row count must remain 1, created_at unchanged)
    created_at_first = rec.created_at_utc
    saved_again = save_recommendations_to_db(session, match_id, res)
    session.commit()
    assert saved_again == 1

    rows_again = list(session.scalars(select(Recommendation)).all())
    assert len(rows_again) == 1
    assert rows_again[0].created_at_utc == created_at_first


def test_recommendation_lifecycle_superseded_and_withdrawn() -> None:
    """Test recommendation lifecycle: active -> superseded (on price change) and -> withdrawn."""
    session, match_id = _setup_test_db()

    raw1 = FortunaPrice("exact_score", (2, 1), 9.50, "2:1", 1)
    out_initial = EvaluatedOutcome(
        market="exact_score",
        selection_display="2:1",
        selection_key="score:2:1",
        odds_fortuna=9.50,
        p_fair=0.15,
        fair_odds=6.67,
        edge=0.425,
        recommended=True,
        rejection_reason="",
        stake_flat=1.0,
        kelly_quarter_capped=0.015,
        raw_price=raw1,
    )
    res_initial = EvaluationResult(
        match_name="Arsenal - Everton",
        snapshot_time=dt.datetime(2026, 10, 24, 12, 0),
        source="fd_prematch",
        books_used=["pinnacle"],
        input_hash="hash_v1",
        fortuna_overround=1.12,
        outcomes=[out_initial],
        recommendations_count=1,
    )

    t0 = dt.datetime(2026, 10, 24, 12, 0, tzinfo=dt.UTC)
    save_recommendations_to_db(session, match_id, res_initial, created_at_utc=t0)
    session.commit()

    rows = list(session.scalars(select(Recommendation)).all())
    assert len(rows) == 1
    assert rows[0].status == "active"
    assert rows[0].odds == Decimal("9.500")
    assert rows[0].created_at_utc == t0

    # Step 2: Price changes to 10.50 (still passes) -> old becomes superseded, new is active
    t1 = dt.datetime(2026, 10, 24, 13, 0, tzinfo=dt.UTC)
    raw2 = FortunaPrice("exact_score", (2, 1), 10.50, "2:1", 1)
    out_updated = EvaluatedOutcome(
        market="exact_score",
        selection_display="2:1",
        selection_key="score:2:1",
        odds_fortuna=10.50,
        p_fair=0.15,
        fair_odds=6.67,
        edge=0.575,
        recommended=True,
        rejection_reason="",
        stake_flat=1.0,
        kelly_quarter_capped=0.020,
        raw_price=raw2,
    )
    res_updated = EvaluationResult(
        match_name="Arsenal - Everton",
        snapshot_time=dt.datetime(2026, 10, 24, 13, 0),
        source="fd_prematch",
        books_used=["pinnacle"],
        input_hash="hash_v2",
        fortuna_overround=1.10,
        outcomes=[out_updated],
        recommendations_count=1,
    )
    save_recommendations_to_db(session, match_id, res_updated, created_at_utc=t1)
    session.commit()

    rows2 = list(session.scalars(select(Recommendation).order_by(Recommendation.id)).all())
    assert len(rows2) == 2
    # Old row: superseded, odds preserved as 9.50, created_at preserved as t0
    assert rows2[0].status == "superseded"
    assert rows2[0].odds == Decimal("9.500")
    assert rows2[0].created_at_utc == t0
    # New row: active, odds 10.50, created_at t1
    assert rows2[1].status == "active"
    assert rows2[1].odds == Decimal("10.500")
    assert rows2[1].created_at_utc == t1

    # Step 3: Price drops to 6.00 (fails edge threshold) -> active becomes withdrawn
    t2 = dt.datetime(2026, 10, 24, 14, 0, tzinfo=dt.UTC)
    raw3 = FortunaPrice("exact_score", (2, 1), 6.00, "2:1", 1)
    out_dropped = EvaluatedOutcome(
        market="exact_score",
        selection_display="2:1",
        selection_key="score:2:1",
        odds_fortuna=6.00,
        p_fair=0.15,
        fair_odds=6.67,
        edge=-0.10,
        recommended=False,
        rejection_reason="Ниже порога",
        stake_flat=0.0,
        kelly_quarter_capped=0.0,
        raw_price=raw3,
    )
    res_dropped = EvaluationResult(
        match_name="Arsenal - Everton",
        snapshot_time=dt.datetime(2026, 10, 24, 14, 0),
        source="fd_prematch",
        books_used=["pinnacle"],
        input_hash="hash_v3",
        fortuna_overround=1.15,
        outcomes=[out_dropped],
        recommendations_count=0,
    )
    save_recommendations_to_db(session, match_id, res_dropped, created_at_utc=t2)
    session.commit()

    rows3 = list(session.scalars(select(Recommendation).order_by(Recommendation.id)).all())
    assert len(rows3) == 2
    assert rows3[0].status == "superseded"
    assert rows3[1].status == "withdrawn"
    assert rows3[1].odds == Decimal("10.500")  # odds preserved
    assert rows3[1].created_at_utc == t1      # created_at preserved


def test_cli_evaluate_and_candidates(tmp_path: Path, monkeypatch) -> None:
    # Use memory or file db for CLI
    db_file = tmp_path / "cli_test.db"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", db_url)

    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    now = dt.datetime.now(dt.UTC)
    future_time = now + dt.timedelta(days=3)
    past_time = now - dt.timedelta(days=3)

    with Session(engine) as session:
        t1 = Team(id=1, name="Arsenal", league="premier_league")
        t2 = Team(id=2, name="Everton", league="premier_league")
        t3 = Team(id=3, name="Chelsea", league="premier_league")
        t4 = Team(id=4, name="Spurs", league="premier_league")
        session.add_all([t1, t2, t3, t4])
        session.flush()
        m = Match(
            id=101,
            league="premier_league",
            season=2026,
            date_utc=future_time.date(),
            commence_time_utc=future_time,
            kickoff_source="toa",
            status="scheduled",
            home_team_id=1,
            away_team_id=2,
        )
        # Past match (commence time in the past)
        m_past = Match(
            id=102,
            league="premier_league",
            season=2026,
            date_utc=past_time.date(),
            commence_time_utc=past_time,
            kickoff_source="toa",
            status="scheduled",
            home_team_id=3,
            away_team_id=4,
        )
        session.add_all([m, m_past])
        session.flush()
        p_odds = PrematchOdds(
            id=1,
            match_id=101,
            bookmaker="P",
            over_price=Decimal("1.85"),
            under_price=Decimal("2.05"),
            home_price=Decimal("1.50"),
            draw_price=Decimal("4.50"),
            away_price=Decimal("6.50"),
        )
        p_odds_past = PrematchOdds(
            id=2,
            match_id=102,
            bookmaker="P",
            over_price=Decimal("1.85"),
            under_price=Decimal("2.05"),
            home_price=Decimal("1.50"),
            draw_price=Decimal("4.50"),
            away_price=Decimal("6.50"),
        )
        session.add_all([p_odds, p_odds_past])
        session.commit()

    # Create dummy Fortuna input file
    fortuna_file = tmp_path / "fortuna.txt"
    fortuna_file.write_text(
        """
        1:0 @ 7.50
        2:0 @ 8.00
        2:1 @ 9.50
        1:0, 2:0, 2:1 @ 3.40
        2-4 gole @ 1.80
        over 2.5 1.95
        """,
        encoding="utf-8",
    )

    # Test evaluate command (future match -> success)
    code_eval = main([
        "evaluate",
        "--match-id", "101",
        "--input", str(fortuna_file),
    ])
    assert code_eval == 0

    # Test evaluate command with post-commence match -> fails saving with exit code 1
    code_past = main([
        "evaluate",
        "--match-id", "102",
        "--input", str(fortuna_file),
        "--save",
    ])
    assert code_past == 1

    # Test candidates command
    code_cand = main([
        "candidates",
        "--days", "7",
        "--from-date", str((now - dt.timedelta(days=1)).date()),
    ])
    assert code_cand == 0


def test_cli_evaluate_save_transitions_to_withdrawn_on_dropped_price(
    tmp_path: Path, monkeypatch
) -> None:
    """Run 1: price passes edge -> active. Run 2: dropped price -> withdrawn.

    Original created_at & initial odds must be kept.
    """
    db_file = tmp_path / "lifecycle_cli.db"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    now = dt.datetime.now(dt.UTC)
    future_time = now + dt.timedelta(days=2)

    with Session(engine) as session:
        t1 = Team(id=1, name="Arsenal", league="premier_league")
        t2 = Team(id=2, name="Chelsea", league="premier_league")
        session.add_all([t1, t2])
        session.flush()

        m = Match(
            id=201,
            league="premier_league",
            season=2026,
            date_utc=future_time.date(),
            commence_time_utc=future_time,
            kickoff_source="toa",
            status="scheduled",
            home_team_id=1,
            away_team_id=2,
        )
        session.add(m)
        session.flush()

        p_odds = PrematchOdds(
            id=1,
            match_id=201,
            bookmaker="P",
            over_price=Decimal("1.85"),
            under_price=Decimal("2.05"),
            home_price=Decimal("1.50"),
            draw_price=Decimal("4.50"),
            away_price=Decimal("6.50"),
        )
        session.add(p_odds)
        session.commit()

    # Run 1: High odds (1:0 @ 10.00 passes edge threshold of 12%)
    input_file_pass = tmp_path / "input_pass.txt"
    input_file_pass.write_text("1:0 @ 10.00\n", encoding="utf-8")

    code1 = main([
        "evaluate",
        "--match-id", "201",
        "--input", str(input_file_pass),
        "--save",
    ])
    assert code1 == 0

    with Session(engine) as session:
        q = select(Recommendation).where(Recommendation.match_id == 201)
        rows1 = list(session.scalars(q).all())
        assert len(rows1) == 1
        assert rows1[0].status == "active"
        assert rows1[0].odds == Decimal("10.000")
        first_created_at = rows1[0].created_at_utc

    # Run 2: Dropped odds (1:0 @ 2.00 fails edge threshold; 0 outcomes pass)
    input_file_drop = tmp_path / "input_drop.txt"
    input_file_drop.write_text("1:0 @ 2.00\n", encoding="utf-8")

    code2 = main([
        "evaluate",
        "--match-id", "201",
        "--input", str(input_file_drop),
        "--save",
    ])
    assert code2 == 0

    with Session(engine) as session:
        q = select(Recommendation).where(Recommendation.match_id == 201)
        rows2 = list(session.scalars(q).all())
        assert len(rows2) == 1
        # Recommendation was transitioned to 'withdrawn' even though 0 outcomes passed
        assert rows2[0].status == "withdrawn"
        # Initial odds and initial created_at_utc are strictly preserved!
        assert rows2[0].odds == Decimal("10.000")
        assert rows2[0].created_at_utc == first_created_at


def test_cli_evaluate_save_past_match_null_commence_fallback_date(
    tmp_path: Path, monkeypatch
) -> None:
    """When commence_time_utc is NULL, fallback to date_utc prohibits saving for past matches."""
    db_file = tmp_path / "past_null_commence.db"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    now = dt.datetime.now(dt.UTC)
    past_date = (now - dt.timedelta(days=2)).date()

    with Session(engine) as session:
        t1 = Team(id=1, name="Arsenal", league="premier_league")
        t2 = Team(id=2, name="Chelsea", league="premier_league")
        session.add_all([t1, t2])
        session.flush()

        m = Match(
            id=202,
            league="premier_league",
            season=2026,
            date_utc=past_date,
            commence_time_utc=None,  # NULL commence time!
            kickoff_source="understat_placeholder",
            status="finished",
            home_team_id=1,
            away_team_id=2,
        )
        session.add(m)
        session.commit()

    inp = tmp_path / "input.txt"
    inp.write_text("1:0 @ 7.50\n", encoding="utf-8")

    code = main([
        "evaluate",
        "--match-id", "202",
        "--input", str(inp),
        "--save",
    ])
    # Must exit with code 1 due to past date fallback
    assert code == 1


def test_cli_evaluate_all_three_real_fixtures_exit_code_zero(
    tmp_path: Path, monkeypatch
) -> None:
    """Verify all 3 real Fortuna fixtures run through CLI evaluate --no-save with exit code 0."""
    db_file = tmp_path / "fixtures_cli.db"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    now = dt.datetime.now(dt.UTC)
    future_time = now + dt.timedelta(days=3)

    fixtures_data = [
        (301, "Barcelona", "Real Madrid", "la_liga", "fortuna_barcelona_real_2026-10-25.txt"),
        (302, "Udinese", "AC Milan", "serie_a", "fortuna_udinese_milan_2026-10-25.txt"),
        (303, "Nice", "Rennes", "ligue_1", "fortuna_nice_rennes_2026-10-25.txt"),
    ]

    with Session(engine) as session:
        for mid, home, away, league, _fname in fixtures_data:
            t1 = Team(id=mid * 2, name=home, league=league)
            t2 = Team(id=mid * 2 + 1, name=away, league=league)
            session.add_all([t1, t2])
            session.flush()

            m = Match(
                id=mid,
                league=league,
                season=2026,
                date_utc=future_time.date(),
                commence_time_utc=future_time,
                kickoff_source="toa",
                status="scheduled",
                home_team_id=t1.id,
                away_team_id=t2.id,
            )
            session.add(m)
            session.flush()

            p_odds = PrematchOdds(
                id=mid,
                match_id=mid,
                bookmaker="P",
                over_price=Decimal("1.85"),
                under_price=Decimal("2.05"),
                home_price=Decimal("2.10"),
                draw_price=Decimal("3.40"),
                away_price=Decimal("3.50"),
            )
            session.add(p_odds)
        session.commit()

    for mid, _home, _away, _league, fname in fixtures_data:
        fixture_path = f"tests/fixtures/{fname}"
        code = main([
            "evaluate",
            "--match-id", str(mid),
            "--input", fixture_path,
        ])
        assert code == 0


def test_cli_candidates_idempotency_same_day_reruns(
    tmp_path: Path, monkeypatch
) -> None:
    """Repeated runs of candidates --save on same day must NOT flip accepted from True to False."""
    db_file = tmp_path / "candidates_idempotent.db"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    # Use fixed Wednesday
    today = dt.date(2026, 10, 21)  # Wednesday of ISO week 43
    kickoff_w1 = dt.datetime(2026, 10, 24, 15, 0, tzinfo=dt.UTC)  # Saturday of week 43
    kickoff_w2 = dt.datetime(2026, 10, 31, 15, 0, tzinfo=dt.UTC)  # Saturday of week 44

    with Session(engine) as session:
        # Create teams
        t1 = Team(id=1, name="Arsenal", league="premier_league")
        t2 = Team(id=2, name="Everton", league="premier_league")
        t3 = Team(id=3, name="Chelsea", league="premier_league")
        t4 = Team(id=4, name="Spurs", league="premier_league")
        session.add_all([t1, t2, t3, t4])
        session.flush()

        # Match in Week 1
        m1 = Match(
            id=10,
            league="premier_league",
            season=2026,
            date_utc=kickoff_w1.date(),
            commence_time_utc=kickoff_w1,
            kickoff_source="toa",
            status="scheduled",
            home_team_id=1,
            away_team_id=2,
        )
        # Match in Week 2
        m2 = Match(
            id=20,
            league="premier_league",
            season=2026,
            date_utc=kickoff_w2.date(),
            commence_time_utc=kickoff_w2,
            kickoff_source="toa",
            status="scheduled",
            home_team_id=3,
            away_team_id=4,
        )
        session.add_all([m1, m2])
        session.flush()

        # Add prematch odds for both matches so fair matrix builds
        p1 = PrematchOdds(
            id=1,
            match_id=10,
            bookmaker="P",
            over_price=Decimal("1.85"),
            under_price=Decimal("2.05"),
            home_price=Decimal("2.10"),
            draw_price=Decimal("3.40"),
            away_price=Decimal("3.50"),
        )
        p2 = PrematchOdds(
            id=2,
            match_id=20,
            bookmaker="P",
            over_price=Decimal("1.85"),
            under_price=Decimal("2.05"),
            home_price=Decimal("2.10"),
            draw_price=Decimal("3.40"),
            away_price=Decimal("3.50"),
        )
        session.add_all([p1, p2])
        session.commit()

    # Run 1: candidates on today
    code1 = main([
        "candidates",
        "--days", "14",
        "--from-date", str(today),
        "--save",
    ])
    assert code1 == 0

    with Session(engine) as session:
        decisions1 = {
            d.match_id: d.accepted
            for d in session.scalars(select(PrefilterDecision)).all()
        }
        assert decisions1[10] is True
        assert decisions1[20] is True

    # Run 2: repeat on the SAME day!
    code2 = main([
        "candidates",
        "--days", "14",
        "--from-date", str(today),
        "--save",
    ])
    assert code2 == 0

    with Session(engine) as session:
        decisions2 = {
            d.match_id: d.accepted
            for d in session.scalars(select(PrefilterDecision)).all()
        }
        # Decisions must remain accepted=True! (Must not flip to False)
        assert decisions2[10] is True
        assert decisions2[20] is True

    # Run 3: third rerun on same day
    code3 = main([
        "candidates",
        "--days", "14",
        "--from-date", str(today),
        "--save",
    ])
    assert code3 == 0

    with Session(engine) as session:
        decisions3 = {
            d.match_id: d.accepted
            for d in session.scalars(select(PrefilterDecision)).all()
        }
        assert decisions3[10] is True
        assert decisions3[20] is True

    # Run 4: rerun on NEXT day (Thursday) in same ISO week:
    # Must carry over accepted=True and NOT flip match 10 to rejected due to budget limit!
    next_day = today + dt.timedelta(days=1)
    code4 = main([
        "candidates",
        "--days", "14",
        "--from-date", str(next_day),
        "--cap", "1",  # Even with cap=1 where match 10 already consumes the cap!
        "--save",
    ])
    assert code4 == 0

    with Session(engine) as session:
        # Check decision row created for next_day
        q_next = (
            select(PrefilterDecision)
            .where(PrefilterDecision.decision_date == next_day)
        )
        decisions_next = {d.match_id: d.accepted for d in session.scalars(q_next).all()}
        assert decisions_next[10] is True


def test_cli_evaluate_save_past_match_null_commence_today_finished(
    tmp_path: Path, monkeypatch
) -> None:
    """When commence_time_utc is NULL but match date is today and status is finished,
    save must be blocked.
    """
    db_file = tmp_path / "today_finished.db"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    today = dt.date.today()

    with Session(engine) as session:
        t1 = Team(id=1, name="Arsenal", league="premier_league")
        t2 = Team(id=2, name="Chelsea", league="premier_league")
        session.add_all([t1, t2])
        session.flush()

        m = Match(
            id=303,
            league="premier_league",
            season=2026,
            date_utc=today,
            commence_time_utc=None,  # NULL commence time!
            kickoff_source="understat_placeholder",
            status="finished",  # Already finished!
            home_team_id=1,
            away_team_id=2,
        )
        session.add(m)
        session.commit()

    inp = tmp_path / "input.txt"
    inp.write_text("1:0 @ 7.50\n", encoding="utf-8")

    code = main([
        "evaluate",
        "--match-id", "303",
        "--input", str(inp),
        "--save",
    ])
    # Must exit with code 1 due to match already finished
    assert code == 1

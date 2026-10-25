"""Tests for recommendations persistence, market loading, and CLI commands."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from core.orm import Base, Match, PrematchOdds, Recommendation, Team
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

"""Tests for Polish bookmakers line shopping script (SPEC v3, Step A5)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.orm import Base, Match, OddsSnapshot, Team
from scripts.pl_line_shopping_check import (
    ParsedMatchBlock,
    ParsedQuote,
    devig_two_way,
    evaluate_line_shopping,
    format_shopping_markdown,
    get_pinnacle_totals_for_match,
    parse_pl_books_text,
)


def test_parse_pl_books_text() -> None:
    raw_text = """
# 2026-10-24 12:00

Arsenal - Everton
Fortuna 1.85 1.95
STS 1.90 1.90
Superbet 1.93 1.92

Celta Vigo - Real Betis
Betclic 1.82 2.02
LVBet 1.83 2.10
"""
    blocks = parse_pl_books_text(raw_text)
    assert len(blocks) == 2

    b1 = blocks[0]
    assert b1.home_team == "Arsenal"
    assert b1.away_team == "Everton"
    assert b1.timestamp == dt.datetime(2026, 10, 24, 12, 0)
    assert len(b1.quotes) == 3
    assert b1.quotes[0] == ParsedQuote("Fortuna", 1.85, 1.95)
    assert b1.quotes[1] == ParsedQuote("STS", 1.90, 1.90)

    b2 = blocks[1]
    assert b2.home_team == "Celta Vigo"
    assert b2.away_team == "Real Betis"
    assert len(b2.quotes) == 2
    assert b2.quotes[1] == ParsedQuote("LVBet", 1.83, 2.10)


def test_devig_two_way() -> None:
    # Balanced 1.90 / 1.90
    p_o, p_u, fair_o, fair_u = devig_two_way(1.90, 1.90)
    assert p_o == pytest.approx(0.50, abs=1e-6)
    assert p_u == pytest.approx(0.50, abs=1e-6)
    assert fair_o == pytest.approx(2.00, abs=1e-6)
    assert fair_u == pytest.approx(2.00, abs=1e-6)

    # Skewed 1.50 / 2.50
    # inv: 0.6667, 0.4000 -> sum = 1.0667
    p_o2, p_u2, fair_o2, fair_u2 = devig_two_way(1.50, 2.50)
    assert p_o2 + p_u2 == pytest.approx(1.0, abs=1e-6)
    assert fair_o2 == pytest.approx(1.60, abs=1e-3)
    assert fair_u2 == pytest.approx(2.667, abs=1e-3)


def test_evaluate_line_shopping_end_to_end() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        t1 = Team(id=1, name="Arsenal", league="premier_league")
        t2 = Team(id=2, name="Everton", league="premier_league")
        session.add_all([t1, t2])
        session.flush()

        m = Match(
            id=100,
            league="premier_league",
            season=2026,
            date_utc=dt.date(2026, 10, 24),
            commence_time_utc=dt.datetime(2026, 10, 24, 15, 0, tzinfo=dt.UTC),
            kickoff_source="toa",
            status="scheduled",
            home_team_id=1,
            away_team_id=2,
        )
        session.add(m)
        session.flush()

        # Add Pinnacle totals snapshot: Over 1.94, Under 1.94
        s_o = OddsSnapshot(
            id=1,
            toa_sport_key="soccer_epl",
            toa_event_id="evt1",
            match_id=100,
            bookmaker="pinnacle",
            market="totals",
            line=2.5,
            outcome="Over",
            price=1.94,
            tier="T-24",
            planned_offset_min=1080,
            actual_offset_min=None,
            t3_day_degraded=False,
            source="toa",
            captured_at_utc=dt.datetime(2026, 10, 23, 22, 0, tzinfo=dt.UTC),
        )
        s_u = OddsSnapshot(
            id=2,
            toa_sport_key="soccer_epl",
            toa_event_id="evt1",
            match_id=100,
            bookmaker="pinnacle",
            market="totals",
            line=2.5,
            outcome="Under",
            price=1.94,
            tier="T-24",
            planned_offset_min=1080,
            actual_offset_min=None,
            t3_day_degraded=False,
            source="toa",
            captured_at_utc=dt.datetime(2026, 10, 23, 22, 0, tzinfo=dt.UTC),
        )
        session.add_all([s_o, s_u])
        session.commit()

        # Check Pinnacle totals lookup
        pin_totals = get_pinnacle_totals_for_match(session, 100, 2.5)
        assert pin_totals is not None
        assert pin_totals[0] == 1.94
        assert pin_totals[1] == 1.94

        block = ParsedMatchBlock(
            timestamp=dt.datetime(2026, 10, 24, 12, 0),
            home_team="Arsenal",
            away_team="Everton",
            quotes=[
                ParsedQuote("Fortuna", 1.85, 1.95),
                ParsedQuote("STS", 1.90, 1.90),
                ParsedQuote("Superbet", 1.93, 1.92),
                ParsedQuote("LVBet", 2.02, 1.85),  # 2.02 exceeds fair price 2.00!
            ],
        )

        summary = evaluate_line_shopping([block], session)
        assert summary.total_matches == 1
        assert summary.total_quotes == 8
        assert summary.quotes_above_fair_count == 1  # LVBet Over 2.02
        assert summary.maxc_above_fair_count == 1   # MaxC Over 2.02

        # Check MaxC calculation
        m_eval = summary.matches[0]
        assert m_eval.maxc_over_book == "LVBet"
        assert m_eval.maxc_over_odds == 2.02
        assert m_eval.maxc_under_book == "Fortuna"
        assert m_eval.maxc_under_odds == 1.95
        # MaxC margin: 1/2.02 + 1/1.95 - 1 = 0.49505 + 0.51282 - 1 = 0.00787 (~0.79%)
        assert m_eval.maxc_margin < 0.01

        # Check markdown format
        md = format_shopping_markdown(summary)
        assert "Arsenal - Everton" in md
        assert "LVBet" in md
        assert "Сжатие маржи" in md


def test_synthetic_fixture_file_evaluates(tmp_path: Path) -> None:
    fixture = Path("tests/fixtures/pl_books_synthetic.txt")
    assert fixture.exists()
    content = fixture.read_text(encoding="utf-8")
    blocks = parse_pl_books_text(content)
    assert len(blocks) == 2
    assert blocks[0].home_team == "Arsenal"
    assert blocks[1].home_team == "Celta Vigo"

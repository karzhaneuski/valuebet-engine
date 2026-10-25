"""Tests for Candidate Prefilter (SPEC §5, docs/decisions.md B6.4)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from core.orm import Base, Match, PrefilterDecision, Team
from core.value.fair_matrix import (
    FairMatrix,
    MarketInputs,
    MarketOdds1X2,
    MarketTotalsLine,
    build_fair_matrix,
)
from core.value.prefilter import (
    CandidateMatch,
    compute_high_draws_mass,
    filter_candidates,
    log_prefilter_decisions_to_db,
)


def _make_matrix(
    total_line: float = 2.5,
    over: float = 1.90,
    under: float = 1.95,
    home: float = 2.0,
    draw: float = 3.5,
    away: float = 3.8,
) -> FairMatrix:
    inputs = MarketInputs(
        source="synthetic",
        snapshot_time=dt.datetime(2026, 10, 24, 12, 0),
        totals_by_book={"pinnacle": [MarketTotalsLine(line=total_line, over=over, under=under)]},
        h2h_by_book={"pinnacle": MarketOdds1X2(home=home, draw=draw, away=away)},
    )
    return build_fair_matrix(inputs)


def test_compute_high_draws_mass() -> None:
    # Balanced match with high total -> higher high-draws mass
    fm_high = _make_matrix(home=2.6, draw=3.4, away=2.7)
    fm_low = _make_matrix(over=2.40, under=1.60, home=1.3, draw=5.5, away=10.0)

    mass_high = compute_high_draws_mass(fm_high)
    mass_low = compute_high_draws_mass(fm_low)

    assert mass_high > mass_low
    # Sum of 2:2, 3:3, 4:4 should be within reasonable soccer range (3% - 10%)
    assert 0.02 < mass_high < 0.15


def test_fortuna_grid_absence_and_no_market() -> None:
    fm = _make_matrix()
    no_mkt = build_fair_matrix(
        MarketInputs(
            source="synthetic",
            snapshot_time=dt.datetime(2026, 10, 24, 12, 0),
            totals_by_book={},
            h2h_by_book={},
        )
    )

    t_kick = dt.datetime(2026, 10, 25, 15, 0)
    c1 = CandidateMatch(1, "A vs B", "PL", t_kick, fm, has_fortuna_grid=False)
    c2 = CandidateMatch(2, "C vs D", "PL", t_kick, no_mkt, has_fortuna_grid=True)
    c3 = CandidateMatch(3, "E vs F", "PL", t_kick, fm, has_fortuna_grid=None)

    rep = filter_candidates([c1, c2, c3], weekly_cap=5)
    assert rep.total_evaluated == 3
    assert rep.total_accepted == 1
    assert rep.candidates[0].match_id == 3

    # Check rejection reasons
    reasons = {item.match_id: item.reason for item in rep.items}
    assert "Нет сетки точного счёта Fortuna" in reasons[1]
    assert "Отсутствуют рыночные котировки" in reasons[2]


def test_league_rotation_and_weekly_cap() -> None:
    # Create 4 matches in PL, 3 in BL1, 2 in SA
    # PL matches have slightly higher draws mass, but rotation ensures fair representation
    matches: list[CandidateMatch] = []
    mid = 100
    t_kick = dt.datetime(2026, 10, 25, 15, 0)

    # PL matches: high draws ~ 6%
    for i in range(4):
        mid += 1
        fm = _make_matrix(home=2.5, draw=3.3, away=2.7)
        matches.append(CandidateMatch(mid, f"PL Match {i}", "PL", t_kick, fm))

    # BL1 matches: high draws ~ 5.5%
    for i in range(3):
        mid += 1
        fm = _make_matrix(home=2.3, draw=3.4, away=3.0)
        matches.append(CandidateMatch(mid, f"BL1 Match {i}", "BL1", t_kick, fm))

    # SA matches: high draws ~ 5.0%
    for i in range(2):
        mid += 1
        fm = _make_matrix(home=2.1, draw=3.5, away=3.5)
        matches.append(CandidateMatch(mid, f"SA Match {i}", "SA", t_kick, fm))

    # Total 9 matches. With weekly_cap=5, max_per_league=2:
    # Round 1: 1 PL, 1 BL1, 1 SA (3 matches)
    # Round 2: 1 PL, 1 BL1 (hits cap 5)
    rep = filter_candidates(matches, weekly_cap=5, max_per_league=2)
    assert rep.total_accepted == 5
    assert rep.accepted_by_league["PL"] == 2
    assert rep.accepted_by_league["BL1"] == 2
    assert rep.accepted_by_league["SA"] == 1


def test_model_anomaly_does_not_boost_ranking() -> None:
    # Match 1: High draw mass 5.5%, NO model anomaly
    fm_good = _make_matrix(home=2.5, draw=3.3, away=2.7)
    c1 = CandidateMatch(
        1, "Good Match", "PL", dt.datetime(2026, 10, 25, 15, 0),
        fm_good, model_home_share=fm_good.home_share,
    )

    # Match 2: Lower draw mass 4.0%, HUGE model anomaly (divergence > 15%)
    fm_low = _make_matrix(over=2.2, under=1.7, home=1.6, draw=4.0, away=5.5)
    c2 = CandidateMatch(
        2, "Anomaly Match", "PL", dt.datetime(2026, 10, 25, 15, 0),
        fm_low, model_home_share=0.10,  # massive divergence
    )

    # Weekly cap 1: only top-ranked match gets accepted
    rep = filter_candidates([c2, c1], weekly_cap=1)
    assert rep.total_accepted == 1
    # Match 1 MUST be accepted because of higher draw mass, despite Match 2 having model anomaly!
    assert rep.candidates[0].match_id == 1

    # Check that Match 2 received the anomaly flag
    item_c2 = next(it for it in rep.items if it.match_id == 2)
    assert any("Аномалия модели" in flag for flag in item_c2.flags)


def test_prefilter_report_table_output() -> None:
    fm1 = _make_matrix(home=2.5, draw=3.3, away=2.7)
    c1 = CandidateMatch(1, "Liverpool - Chelsea", "PL", dt.datetime(2026, 10, 25, 16, 30), fm1)
    c2 = CandidateMatch(2, "Real Madrid - Barcelona", "PD", dt.datetime(2026, 10, 25, 20, 0), fm1)

    rep = filter_candidates([c1, c2], weekly_cap=2)
    table = rep.format_table()

    assert "ПРЕФИЛЬТР КАНДИДАТОВ" in table
    assert "Liverpool - Chelsea" in table
    assert "Real Madrid - Barcelona" in table
    assert "#1" in table
    assert "#2" in table
    assert "ОТОБРАН" in table


def test_log_prefilter_decisions_to_db() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        # Create team and match
        t1 = Team(id=1, name="Team 1", league="PL")
        t2 = Team(id=2, name="Team 2", league="PL")
        session.add_all([t1, t2])
        m = Match(
            id=10,
            league="PL",
            season=2026,
            date_utc=dt.date(2026, 10, 25),
            kickoff_source="toa",
            status="scheduled",
            home_team_id=1,
            away_team_id=2,
        )
        session.add(m)
        session.commit()

        fm = _make_matrix()
        c = CandidateMatch(10, "Team 1 - Team 2", "PL", dt.datetime(2026, 10, 25, 15, 0), fm)
        rep = filter_candidates([c], weekly_cap=5)

        saved = log_prefilter_decisions_to_db(session, rep, decision_date=dt.date(2026, 10, 25))
        assert saved == 1

        # Check DB row
        rows = list(session.scalars(select(PrefilterDecision)).all())
        assert len(rows) == 1
        assert rows[0].match_id == 10
        assert rows[0].accepted is True
        assert rows[0].decision_date == dt.date(2026, 10, 25)

        # Re-run (idempotency check: should update, not create duplicate)
        saved2 = log_prefilter_decisions_to_db(session, rep, decision_date=dt.date(2026, 10, 25))
        assert saved2 == 1
        rows2 = list(session.scalars(select(PrefilterDecision)).all())
        assert len(rows2) == 1

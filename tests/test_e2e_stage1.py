"""E2E Stage 1 integration test: full lifecycle from pre-match to settlement (SPEC v3 §9).

Lifecycle steps:
1. T-48: /events creates scheduled match (Barcelona — Real Madrid).
2. T-24: poll snapshots (totals + h2h).
3. T-24: build fair matrix from pre-kickoff market inputs.
4. User evaluates real Fortuna odds fixture (tests/fixtures/fortuna_barcelona_real_2026-10-25.txt).
5. User registers bet (UserBet before kickoff).
6. T-3: poll final closing snapshot (totals + h2h).
7. Kickoff: commence time passes.
8. Match finish: official result recorded (Result: 2-1).
9. job_settle_matches_and_clv:
   - bet settled as won.
   - pre-kickoff CLV calculated from latest pre-kickoff snapshot (source: fair_market_matrix).
10. Portfolio performance statistics verified.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.config import load_config
from core.orm import Base, Match, OddsSnapshot, Result, Team, TeamAlias, UserBet
from core.scheduler.tasks import job_settle_matches_and_clv
from core.tracking.clv import calculate_user_bet_clv
from core.tracking.stats import compute_performance_metrics
from core.value.engine import evaluate_fortuna_prices
from core.value.fair_matrix import build_fair_matrix
from core.value.fortuna_input import parse_fortuna_text
from core.value.persistence import load_market_inputs_for_match

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
REAL_FORTUNA_FILE = FIXTURES_DIR / "fortuna_barcelona_real_2026-10-25.txt"


def test_e2e_stage1_full_lifecycle() -> None:
    # 0. Setup in-memory isolated database
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)
    cfg = load_config()

    with sm() as session:
        # Seed teams
        barca = Team(name="Barcelona", league="la_liga")
        real = Team(name="Real Madrid", league="la_liga")
        session.add_all([barca, real])
        session.flush()

        session.add_all([
            TeamAlias(team_id=barca.id, source="toa", alias="Barcelona"),
            TeamAlias(team_id=real.id, source="toa", alias="Real Madrid"),
        ])
        session.commit()

        # Step 1 (T-48h): Create scheduled match
        kickoff_utc = dt.datetime(2026, 10, 25, 20, 0, tzinfo=dt.UTC)
        match = Match(
            league="la_liga",
            season="2026-2027",
            home_team_id=barca.id,
            away_team_id=real.id,
            home_team=barca,
            away_team=real,
            date_utc=kickoff_utc.date(),
            commence_time_utc=kickoff_utc,
            kickoff_source="toa",
            status="scheduled",
            toa_event_id="ev_barca_real_20261025",
        )
        session.add(match)
        session.commit()
        match_id = match.id

        # Step 2 (T-24h): Poll snapshots (totals + h2h)
        t_24 = kickoff_utc - dt.timedelta(hours=24)
        snaps_t24 = [
            # Pinnacle totals 2.5
            OddsSnapshot(
                toa_sport_key="soccer_spain_la_liga",
                toa_event_id=match.toa_event_id,
                match_id=match_id,
                bookmaker="pinnacle",
                market="totals",
                line=Decimal("2.5"),
                outcome="Over",
                price=Decimal("1.952"),
                tier="T-24",
                planned_offset_min=1440,
                actual_offset_min=1440,
                source="toa",
                captured_at_utc=t_24,
            ),
            OddsSnapshot(
                toa_sport_key="soccer_spain_la_liga",
                toa_event_id=match.toa_event_id,
                match_id=match_id,
                bookmaker="pinnacle",
                market="totals",
                line=Decimal("2.5"),
                outcome="Under",
                price=Decimal("1.952"),
                tier="T-24",
                planned_offset_min=1440,
                actual_offset_min=1440,
                source="toa",
                captured_at_utc=t_24,
            ),
            # Pinnacle h2h
            OddsSnapshot(
                toa_sport_key="soccer_spain_la_liga",
                toa_event_id=match.toa_event_id,
                match_id=match_id,
                bookmaker="pinnacle",
                market="h2h",
                line=None,
                outcome="Home",
                price=Decimal("2.100"),
                tier="T-24",
                planned_offset_min=1440,
                actual_offset_min=1440,
                source="toa",
                captured_at_utc=t_24,
            ),
            OddsSnapshot(
                toa_sport_key="soccer_spain_la_liga",
                toa_event_id=match.toa_event_id,
                match_id=match_id,
                bookmaker="pinnacle",
                market="h2h",
                line=None,
                outcome="Draw",
                price=Decimal("3.500"),
                tier="T-24",
                planned_offset_min=1440,
                actual_offset_min=1440,
                source="toa",
                captured_at_utc=t_24,
            ),
            OddsSnapshot(
                toa_sport_key="soccer_spain_la_liga",
                toa_event_id=match.toa_event_id,
                match_id=match_id,
                bookmaker="pinnacle",
                market="h2h",
                line=None,
                outcome="Away",
                price=Decimal("3.400"),
                tier="T-24",
                planned_offset_min=1440,
                actual_offset_min=1440,
                source="toa",
                captured_at_utc=t_24,
            ),
        ]
        session.add_all(snaps_t24)
        session.commit()

        # Step 3 (T-24h): Calculate fair prices from pre-kickoff market inputs
        inputs = load_market_inputs_for_match(session, match_id, as_of=t_24)
        assert not inputs.is_empty()
        assert inputs.source == "toa_snapshot"

        matrix = build_fair_matrix(
            inputs, home_team="Barcelona", away_team="Real Madrid", config=cfg
        )
        assert matrix.status == "ok"
        assert matrix.exact_score(2, 1) > 0.05

        # Step 4: Evaluate real Fortuna fixture text
        raw_text = REAL_FORTUNA_FILE.read_text(encoding="utf-8")
        prices = parse_fortuna_text(raw_text)
        assert len(prices) > 20

        eval_res = evaluate_fortuna_prices(
            fair_matrix=matrix,
            fortuna_prices=prices,
            match_name="Barcelona - Real Madrid",
            home_team="Barcelona",
            away_team="Real Madrid",
            config=cfg,
        )
        assert len(eval_res.outcomes) > 20

        # Step 5: User registers bet on exact_score '2:1' at odds 10.00
        # Verification: placement strictly prior to kickoff
        placed_at = t_24 + dt.timedelta(hours=1)
        assert placed_at < kickoff_utc

        bet = UserBet(
            match_id=match_id,
            market="exact_score",
            selection_key="score:2:1",
            line=None,
            odds=Decimal("10.00"),
            stake_units=Decimal("1.00"),
            bookmaker="fortuna",
            p_fair_at_placement=Decimal(str(round(matrix.exact_score(2, 1), 4))),
            placed_at_utc=placed_at,
            created_at_utc=placed_at,
            status="open",
        )
        session.add(bet)
        session.commit()
        bet_id = bet.id

        # Step 6 (T-3h): Poll final closing snapshot
        t_3 = kickoff_utc - dt.timedelta(hours=3)
        snaps_t3 = [
            # Closing totals 2.5
            OddsSnapshot(
                toa_sport_key="soccer_spain_la_liga",
                toa_event_id=match.toa_event_id,
                match_id=match_id,
                bookmaker="pinnacle",
                market="totals",
                line=Decimal("2.5"),
                outcome="Over",
                price=Decimal("1.930"),
                tier="T-3",
                planned_offset_min=180,
                actual_offset_min=180,
                source="toa",
                captured_at_utc=t_3,
            ),
            OddsSnapshot(
                toa_sport_key="soccer_spain_la_liga",
                toa_event_id=match.toa_event_id,
                match_id=match_id,
                bookmaker="pinnacle",
                market="totals",
                line=Decimal("2.5"),
                outcome="Under",
                price=Decimal("1.970"),
                tier="T-3",
                planned_offset_min=180,
                actual_offset_min=180,
                source="toa",
                captured_at_utc=t_3,
            ),
            # Closing h2h
            OddsSnapshot(
                toa_sport_key="soccer_spain_la_liga",
                toa_event_id=match.toa_event_id,
                match_id=match_id,
                bookmaker="pinnacle",
                market="h2h",
                line=None,
                outcome="Home",
                price=Decimal("2.050"),
                tier="T-3",
                planned_offset_min=180,
                actual_offset_min=180,
                source="toa",
                captured_at_utc=t_3,
            ),
            OddsSnapshot(
                toa_sport_key="soccer_spain_la_liga",
                toa_event_id=match.toa_event_id,
                match_id=match_id,
                bookmaker="pinnacle",
                market="h2h",
                line=None,
                outcome="Draw",
                price=Decimal("3.550"),
                tier="T-3",
                planned_offset_min=180,
                actual_offset_min=180,
                source="toa",
                captured_at_utc=t_3,
            ),
            OddsSnapshot(
                toa_sport_key="soccer_spain_la_liga",
                toa_event_id=match.toa_event_id,
                match_id=match_id,
                bookmaker="pinnacle",
                market="h2h",
                line=None,
                outcome="Away",
                price=Decimal("3.500"),
                tier="T-3",
                planned_offset_min=180,
                actual_offset_min=180,
                source="toa",
                captured_at_utc=t_3,
            ),
        ]
        session.add_all(snaps_t3)
        session.commit()

        # Step 7 & 8: Kickoff passes, match finishes with official score 2-1
        match.status = "finished"
        result = Result(
            match_id=match_id,
            home_goals=2,
            away_goals=1,
            source="understat",
            settled_at_utc=kickoff_utc + dt.timedelta(hours=2, minutes=30),
        )
        session.add(result)
        session.commit()

    with sm() as session:
        user_bet = session.get(UserBet, bet_id)
        assert user_bet is not None
        clv_val, src = calculate_user_bet_clv(session, user_bet, config=cfg)
        assert clv_val is not None
        assert src == "fair_market_matrix"

    # Step 9: Run settlement and CLV job
    settle_report = job_settle_matches_and_clv(sm, config=cfg, dry_run=False)
    assert settle_report["settled_bets"] == 1
    assert settle_report["clv_calculated"] == 1

    # Step 10: Verify settled bet and performance metrics
    with sm() as session:
        settled_bet = session.get(UserBet, bet_id)
        assert settled_bet is not None
        assert settled_bet.status == "won"
        assert settled_bet.clv_fair is not None
        assert settled_bet.clv_source == "fair_market_matrix"

        # CLV against closing matrix: odds * p_closing - 1
        assert float(settled_bet.clv_fair) != 0.0

        # Portfolio metrics
        metrics = compute_performance_metrics([settled_bet])
        assert metrics.total_bets == 1
        assert metrics.settled_bets == 1
        assert metrics.won_bets == 1
        assert metrics.lost_bets == 0
        assert metrics.push_bets == 0
        assert metrics.hit_rate == 1.0
        assert metrics.total_pnl == Decimal("9.00")
        assert metrics.roi == 9.0  # +900% ROI on 1.0 unit stake at 10.00 odds
        assert metrics.avg_clv == float(settled_bet.clv_fair)

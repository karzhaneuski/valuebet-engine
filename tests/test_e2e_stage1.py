"""E2E Stage 1 integration test: full live lifecycle from pre-match to settlement (SPEC §9).

Lifecycle steps:
1. T-48: job_poll_events creates scheduled match (Barcelona — Real Madrid).
2. T-24: job_poll_odds_snapshots polls Pinnacle odds snapshots (totals + h2h).
3. T-24: build fair matrix from pre-kickoff market inputs.
4. User evaluates real Fortuna odds fixture (tests/fixtures/fortuna_barcelona_real_2026-10-25.txt).
5. User registers bet (UserBet before kickoff).
6. T-3: job_poll_odds_snapshots polls final closing snapshot (totals + h2h).
7. Verification: CLV is None before kickoff.
8. Kickoff passes, match finishes.
9. job_refresh_history syncs match score and records official Result.
10. job_settle_matches_and_clv:
   - bet settled as won.
   - pre-kickoff CLV calculated from latest pre-kickoff snapshot (source: fair_market_matrix).
11. Portfolio performance statistics verified.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from core.config import load_config
from core.orm import Match, UserBet
from core.scheduler.tasks import (
    job_poll_events,
    job_poll_odds_snapshots,
    job_refresh_history,
    job_settle_matches_and_clv,
)
from core.tracking.clv import calculate_user_bet_clv
from core.tracking.stats import compute_performance_metrics
from core.value.engine import evaluate_fortuna_prices
from core.value.fair_matrix import build_fair_matrix
from core.value.fortuna_input import parse_fortuna_text
from core.value.persistence import load_market_inputs_for_match
from tests.e2e_support import (
    DEFAULT_KICKOFF,
    DEFAULT_MATCH_EID,
    ClockPatcher,
    build_toa_handler,
    create_toa_client,
    create_understat_loader,
    init_e2e_db,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
REAL_FORTUNA_FILE = FIXTURES_DIR / "fortuna_barcelona_real_2026-10-25.txt"


def test_e2e_stage1_full_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 0. Setup isolated file-based SQLite database in tmp_path shared across participants
    db_file = tmp_path / "e2e_stage1.db"
    db_url = f"sqlite:///{db_file}"
    kickoff_utc = DEFAULT_KICKOFF
    match_eid = DEFAULT_MATCH_EID
    sm = init_e2e_db(db_url, kickoff_utc=kickoff_utc)
    cfg = load_config()

    # Current odds state served by mock transport
    current_odds_tier: str = "T-24"
    clock = ClockPatcher()
    toa_handler = build_toa_handler(
        kickoff_utc=kickoff_utc,
        match_eid=match_eid,
        get_odds_tier=lambda: current_odds_tier,
    )
    toa_client = create_toa_client(db_url, cfg, toa_handler, clock=clock.clock)

    # Step 1 (T-48h): Create scheduled match via job_poll_events
    t_48 = kickoff_utc - dt.timedelta(hours=48)
    clock.set_now(t_48)
    events_res = job_poll_events(
        sm,
        client=toa_client,
        config=cfg,
        now=t_48,
    )
    assert events_res["status"] == "success"
    assert events_res["events_seen"] == 1

    with sm() as session:
        match = session.scalar(select(Match).where(Match.toa_event_id == match_eid))
        assert match is not None
        assert match.status == "scheduled"
        match_id = match.id

    # Step 2 (T-24h): Poll snapshots (totals + h2h) via job_poll_odds_snapshots
    current_odds_tier = "T-24"
    t_24 = kickoff_utc - dt.timedelta(hours=24)
    clock.set_now(t_24)

    poll_t24_res = job_poll_odds_snapshots(
        sm,
        client=toa_client,
        config=cfg,
        now=t_24,
    )
    assert poll_t24_res["status"] == "success"
    assert poll_t24_res["snapshots_taken"] == 5

    # Step 3 (T-24h): Calculate fair prices from pre-kickoff market inputs
    with sm() as session:
        inputs = load_market_inputs_for_match(session, match_id)
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

    # Step 6 (T-3h): Poll final closing snapshot via job_poll_odds_snapshots
    current_odds_tier = "T-3"
    t_3 = kickoff_utc - dt.timedelta(hours=3)
    clock.set_now(t_3)
    poll_t3_res = job_poll_odds_snapshots(
        sm,
        client=toa_client,
        config=cfg,
        now=t_3,
    )
    assert poll_t3_res["status"] == "success"
    assert poll_t3_res["snapshots_taken"] == 5

    # Step 7: Verify CLV is not fixed before kickoff
    with sm() as session:
        user_bet = session.get(UserBet, bet_id)
        assert user_bet is not None
        clv_pre, _ = calculate_user_bet_clv(session, user_bet, config=cfg, now=t_3)
        assert clv_pre is None

    # Step 8: Match finishes -> sync official Result via job_refresh_history
    t_post = kickoff_utc + dt.timedelta(hours=3)
    understat_loader = create_understat_loader(db_url, cfg, kickoff_utc=kickoff_utc)
    hist_res = job_refresh_history(
        session_factory=sm,
        understat_loader=understat_loader,
        toa_client=toa_client,
        config=cfg,
        now=t_post,
    )
    assert hist_res["status"] == "success"

    # Verify exactly ONE Match row exists, status is finished, and MatchStat + Result belong to it
    with sm() as session:
        from core.orm import MatchStat, Result

        all_matches = list(session.scalars(select(Match)).all())
        assert len(all_matches) == 1
        assert all_matches[0].id == match_id
        assert all_matches[0].league == "la_liga"
        assert all_matches[0].status == "finished"

        m_stats = list(session.scalars(select(MatchStat)).all())
        assert len(m_stats) == 1
        assert m_stats[0].match_id == match_id

        m_results = list(session.scalars(select(Result)).all())
        assert len(m_results) == 1
        assert m_results[0].match_id == match_id

    # Step 9: Run settlement and CLV job via job_settle_matches_and_clv
    settle_report = job_settle_matches_and_clv(sm, config=cfg, dry_run=False, now=t_post)
    assert settle_report["settled_bets"] == 1
    assert settle_report["clv_calculated"] == 1

    # Step 10: Verify assertions:
    # - Snapshots for both tiers (T-24 and T-3) exist in DB
    # - MatchStat and official Result exist in DB
    # - CLV before kickoff (None) and after settlement (calculated)
    # - Portfolio performance statistics
    with sm() as session:
        from core.orm import MatchStat, OddsSnapshot, Result

        snaps_t24 = list(
            session.scalars(
                select(OddsSnapshot).where(
                    OddsSnapshot.match_id == match_id, OddsSnapshot.tier == "T-24"
                )
            ).all()
        )
        snaps_t3 = list(
            session.scalars(
                select(OddsSnapshot).where(
                    OddsSnapshot.match_id == match_id, OddsSnapshot.tier == "T-3"
                )
            ).all()
        )
        assert len(snaps_t24) == 5
        assert len(snaps_t3) == 5

        match_stat = session.scalar(select(MatchStat).where(MatchStat.match_id == match_id))
        assert match_stat is not None
        assert match_stat.home_goals == 2
        assert match_stat.away_goals == 1

        official_res = session.scalar(select(Result).where(Result.match_id == match_id))
        assert official_res is not None
        assert official_res.home_goals == 2
        assert official_res.away_goals == 1

        settled_bet = session.get(UserBet, bet_id)
        assert settled_bet is not None
        assert settled_bet.status == "won"
        assert settled_bet.clv_fair is not None
        assert settled_bet.clv_source == "fair_market_matrix"
        assert float(settled_bet.clv_fair) != 0.0

        metrics = compute_performance_metrics([settled_bet])
        assert metrics.total_bets == 1
        assert metrics.settled_bets == 1
        assert metrics.won_bets == 1
        assert metrics.lost_bets == 0
        assert metrics.push_bets == 0
        assert metrics.hit_rate == 1.0
        assert metrics.total_pnl == Decimal("9.00")
        assert metrics.total_stake == Decimal("1.00")
        assert metrics.roi == 9.0
        assert metrics.avg_clv == float(settled_bet.clv_fair)

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
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from core.config import load_config
from core.ingest.odds_api import EventsReport
from core.ingest.understat import UnderstatReport, UnderstatRow
from core.orm import Base, Match, MatchStat, OddsSnapshot, QuotaCounter, Team, TeamAlias, UserBet
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

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
REAL_FORTUNA_FILE = FIXTURES_DIR / "fortuna_barcelona_real_2026-10-25.txt"


class E2EFakeTOAClient:
    """Fake TOA client for live pipeline execution."""

    def __init__(self, sm: sessionmaker[Session], match_time: dt.datetime) -> None:
        self.sm = sm
        self.match_time = match_time
        self.calls: list[dict[str, Any]] = []

    def fetch_events(self, sport_key: str) -> list[dict[str, Any]]:
        return [
            {
                "id": "ev_barca_real_20261025",
                "sport_key": sport_key,
                "commence_time": self.match_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "home_team": "Barcelona",
                "away_team": "Real Madrid",
            }
        ]

    def apply_events(
        self,
        sport_key: str,
        events: list[dict[str, Any]],
        league_slug: str,
        season: int,
        now: dt.datetime,
    ) -> EventsReport:
        report = EventsReport()
        with self.sm() as session:
            barca = session.scalar(select(Team).where(Team.name == "Barcelona"))
            real = session.scalar(select(Team).where(Team.name == "Real Madrid"))
            assert barca is not None and real is not None

            m = session.scalar(select(Match).where(Match.toa_event_id == "ev_barca_real_20261025"))
            if m is None:
                m = Match(
                    league=league_slug,
                    season=f"{season}-{season+1}",
                    home_team_id=barca.id,
                    away_team_id=real.id,
                    home_team=barca,
                    away_team=real,
                    date_utc=self.match_time.date(),
                    commence_time_utc=self.match_time,
                    kickoff_source="toa",
                    status="scheduled",
                    toa_event_id="ev_barca_real_20261025",
                )
                session.add(m)
                report.events_seen += 1
            session.commit()
        return report

    def snapshot_sport_odds(
        self, sport_key: str, tier: str, markets: list[str] | None = None
    ) -> dict[str, Any]:
        self.calls.append({"sport_key": sport_key, "tier": tier, "markets": markets})
        with self.sm() as session:
            m = session.scalar(select(Match).where(Match.toa_event_id == "ev_barca_real_20261025"))
            assert m is not None

            capture_time = (
                self.match_time - dt.timedelta(hours=24)
                if tier == "T-24"
                else self.match_time - dt.timedelta(hours=3)
            )

            # Pinnacle totals 2.5
            session.add(
                OddsSnapshot(
                    toa_sport_key=sport_key,
                    toa_event_id=m.toa_event_id,
                    match_id=m.id,
                    bookmaker="pinnacle",
                    market="totals",
                    line=Decimal("2.5"),
                    outcome="Over",
                    price=Decimal("1.952" if tier == "T-24" else "1.930"),
                    tier=tier,
                    planned_offset_min=1440 if tier == "T-24" else 180,
                    actual_offset_min=1440 if tier == "T-24" else 180,
                    source="toa",
                    captured_at_utc=capture_time,
                )
            )
            session.add(
                OddsSnapshot(
                    toa_sport_key=sport_key,
                    toa_event_id=m.toa_event_id,
                    match_id=m.id,
                    bookmaker="pinnacle",
                    market="totals",
                    line=Decimal("2.5"),
                    outcome="Under",
                    price=Decimal("1.952" if tier == "T-24" else "1.970"),
                    tier=tier,
                    planned_offset_min=1440 if tier == "T-24" else 180,
                    actual_offset_min=1440 if tier == "T-24" else 180,
                    source="toa",
                    captured_at_utc=capture_time,
                )
            )
            # Pinnacle h2h
            for outcome, price_t24, price_t3 in [
                ("Home", "2.100", "2.050"),
                ("Draw", "3.500", "3.550"),
                ("Away", "3.400", "3.500"),
            ]:
                session.add(
                    OddsSnapshot(
                        toa_sport_key=sport_key,
                        toa_event_id=m.toa_event_id,
                        match_id=m.id,
                        bookmaker="pinnacle",
                        market="h2h",
                        line=None,
                        outcome=outcome,
                        price=Decimal(price_t24 if tier == "T-24" else price_t3),
                        tier=tier,
                        planned_offset_min=1440 if tier == "T-24" else 180,
                        actual_offset_min=1440 if tier == "T-24" else 180,
                        source="toa",
                        captured_at_utc=capture_time,
                    )
                )
            session.commit()
        return {"rows": 5, "credits": 2}


class E2EFakeUnderstatLoader:
    """Fake Understat loader for official score sync."""

    def __init__(self, sm: sessionmaker[Session]) -> None:
        self.sm = sm

    def schedule_rows(self, slug: str, year: int) -> list[UnderstatRow]:
        return [
            UnderstatRow(
                understat_id="99901",
                naive_start=dt.datetime(2026, 10, 25, 20, 0),
                home="Barcelona",
                away="Real Madrid",
                home_goals=2,
                away_goals=1,
                home_xg=2.15,
                away_xg=1.10,
                is_result=True,
                league_slug=slug,
                season=year,
            )
        ]

    def upsert_schedule(self, rows: list[UnderstatRow]) -> UnderstatReport:
        report = UnderstatReport()
        with self.sm() as session:
            m = session.scalar(select(Match).where(Match.toa_event_id == "ev_barca_real_20261025"))
            assert m is not None
            session.add(
                MatchStat(
                    match_id=m.id,
                    source="understat",
                    home_goals=2,
                    away_goals=1,
                    home_xg=2.15,
                    away_xg=1.10,
                )
            )
            session.commit()
            report.stats_written = 1
        return report


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
        # Seed teams & quotas
        session.add(
            QuotaCounter(
                provider="toa",
                period=dt.datetime.now(dt.UTC).strftime("%Y-%m"),
                credits_used=0,
                updated_at_utc=dt.datetime.now(dt.UTC),
            )
        )
        barca = Team(name="Barcelona", league="la_liga")
        real = Team(name="Real Madrid", league="la_liga")
        session.add_all([barca, real])
        session.flush()

        session.add_all([
            TeamAlias(team_id=barca.id, source="toa", alias="Barcelona"),
            TeamAlias(team_id=real.id, source="toa", alias="Real Madrid"),
        ])
        session.commit()

    kickoff_utc = dt.datetime(2026, 10, 25, 20, 0, tzinfo=dt.UTC)
    fake_toa = E2EFakeTOAClient(sm, kickoff_utc)

    # Step 1 (T-48h): Create scheduled match via job_poll_events
    t_48 = kickoff_utc - dt.timedelta(hours=48)
    events_res = job_poll_events(
        sm,
        client=fake_toa,  # type: ignore[arg-type]
        config=cfg,
        now=t_48,
    )
    assert events_res["status"] == "success"
    assert events_res["events_seen"] == 1

    with sm() as session:
        match = session.scalar(select(Match).where(Match.toa_event_id == "ev_barca_real_20261025"))
        assert match is not None
        assert match.status == "scheduled"
        match_id = match.id

    # Step 2 (T-24h): Poll snapshots (totals + h2h) via job_poll_odds_snapshots
    t_24 = kickoff_utc - dt.timedelta(hours=24)
    poll_t24_res = job_poll_odds_snapshots(
        sm,
        client=fake_toa,  # type: ignore[arg-type]
        config=cfg,
        now=t_24,
    )
    assert poll_t24_res["status"] == "success"
    assert poll_t24_res["snapshots_taken"] == 5

    # Step 3 (T-24h): Calculate fair prices from pre-kickoff market inputs
    with sm() as session:
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
    t_3 = kickoff_utc - dt.timedelta(hours=3)
    poll_t3_res = job_poll_odds_snapshots(
        sm,
        client=fake_toa,  # type: ignore[arg-type]
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
    fake_understat = E2EFakeUnderstatLoader(sm)
    hist_res = job_refresh_history(
        session_factory=sm,
        understat_loader=fake_understat,  # type: ignore[arg-type]
        config=cfg,
        now=t_post,
    )
    assert hist_res["status"] == "success"

    # Step 9: Run settlement and CLV job via job_settle_matches_and_clv
    settle_report = job_settle_matches_and_clv(sm, config=cfg, dry_run=False, now=t_post)
    assert settle_report["settled_bets"] == 1
    assert settle_report["clv_calculated"] == 1

    # Step 10: Verify settled bet and performance metrics
    with sm() as session:
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
        assert metrics.roi == 9.0
        assert metrics.avg_clv == float(settled_bet.clv_fair)

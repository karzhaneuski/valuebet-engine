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

import httpx
import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import core.ingest.odds_api
from core.config import load_config
from core.ingest.understat import UnderstatLoader
from core.orm import Base, Match, QuotaCounter, Team, TeamAlias, UserBet
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


class FakeUnderstatReader:
    """Fake Understat reader returning pandas schedule frame."""

    def __init__(self, kickoff_time: dt.datetime, understat_league: str | None = None) -> None:
        self.kickoff_time = kickoff_time
        self.understat_league = understat_league

    def read_schedule(self, **_: object) -> pd.DataFrame:
        cols = [
            "game_id",
            "home_team",
            "away_team",
            "date",
            "is_result",
            "home_goals",
            "away_goals",
            "home_xg",
            "away_xg",
        ]
        if self.understat_league == "ESP-La Liga":
            return pd.DataFrame([
                {
                    "game_id": 99901,
                    "home_team": "Barcelona",
                    "away_team": "Real Madrid",
                    "date": pd.Timestamp(self.kickoff_time.replace(tzinfo=None)),
                    "is_result": True,
                    "home_goals": 2,
                    "away_goals": 1,
                    "home_xg": 2.15,
                    "away_xg": 1.10,
                }
            ])
        return pd.DataFrame(columns=cols)


def test_e2e_stage1_full_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 0. Setup isolated file-based SQLite database in tmp_path shared across participants
    db_file = tmp_path / "e2e_stage1.db"
    db_url = f"sqlite:///{db_file}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)
    cfg = load_config()

    kickoff_utc = dt.datetime(2026, 10, 25, 20, 0, tzinfo=dt.UTC)
    now_base = kickoff_utc - dt.timedelta(hours=24)
    match_eid = "ev_barca_real_20261025"
    commence_str = kickoff_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    with sm() as session:
        # Seed teams & quotas
        session.add(
            QuotaCounter(
                provider="toa",
                period=kickoff_utc.strftime("%Y-%m"),
                credits_used=0,
                updated_at_utc=now_base,
            )
        )
        barca = Team(name="Barcelona", league="la_liga")
        real = Team(name="Real Madrid", league="la_liga")
        session.add_all([barca, real])
        session.flush()

        session.add_all([
            TeamAlias(team_id=barca.id, source="toa", alias="Barcelona"),
            TeamAlias(team_id=real.id, source="toa", alias="Real Madrid"),
            TeamAlias(team_id=barca.id, source="understat", alias="Barcelona"),
            TeamAlias(team_id=real.id, source="understat", alias="Real Madrid"),
        ])
        session.commit()

    # Current odds state served by mock transport
    current_odds_tier: str = "T-24"

    def toa_handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path
        if url_path == "/v4/sports/soccer_spain_la_liga/events":
            events_data = [
                {
                    "id": match_eid,
                    "sport_key": "soccer_spain_la_liga",
                    "commence_time": commence_str,
                    "home_team": "Barcelona",
                    "away_team": "Real Madrid",
                }
            ]
            return httpx.Response(
                200,
                json=events_data,
                headers={"x-requests-remaining": "495", "x-requests-last": "0"},
            )
        elif url_path.endswith("/events"):
            return httpx.Response(
                200,
                json=[],
                headers={"x-requests-remaining": "495", "x-requests-last": "0"},
            )
        elif url_path == "/v4/sports/soccer_spain_la_liga/odds":
            price_over = 1.952 if current_odds_tier == "T-24" else 1.930
            price_under = 1.952 if current_odds_tier == "T-24" else 1.970
            price_home = 2.100 if current_odds_tier == "T-24" else 2.050
            price_draw = 3.500 if current_odds_tier == "T-24" else 3.550
            price_away = 3.400 if current_odds_tier == "T-24" else 3.500

            odds_data = [
                {
                    "id": match_eid,
                    "sport_key": "soccer_spain_la_liga",
                    "commence_time": commence_str,
                    "home_team": "Barcelona",
                    "away_team": "Real Madrid",
                    "bookmakers": [
                        {
                            "key": "pinnacle",
                            "title": "Pinnacle",
                            "markets": [
                                {
                                    "key": "totals",
                                    "outcomes": [
                                        {"name": "Over", "price": price_over, "point": 2.5},
                                        {"name": "Under", "price": price_under, "point": 2.5},
                                    ],
                                },
                                {
                                    "key": "h2h",
                                    "outcomes": [
                                        {"name": "Barcelona", "price": price_home},
                                        {"name": "Draw", "price": price_draw},
                                        {"name": "Real Madrid", "price": price_away},
                                    ],
                                },
                            ],
                        }
                    ],
                }
            ]
            return httpx.Response(
                200,
                json=odds_data,
                headers={"x-requests-remaining": "490", "x-requests-last": "2"},
            )
        elif url_path == "/v4/sports/soccer_uefa_champs_league/scores":
            return httpx.Response(
                200,
                json=[],
                headers={"x-requests-remaining": "488", "x-requests-last": "2"},
            )
        else:
            pytest.fail(f"Unexpected TOA request URL: {request.url}")

    mock_transport = httpx.MockTransport(toa_handler)
    http_client = httpx.Client(transport=mock_transport)
    toa_client = core.ingest.odds_api.OddsApiClient(db_url, cfg, "k", client=http_client)

    # Step 1 (T-48h): Create scheduled match via job_poll_events
    t_48 = kickoff_utc - dt.timedelta(hours=48)
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
    current_time = t_24

    class MockDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return current_time

    monkeypatch.setattr(core.ingest.odds_api.dt, "datetime", MockDatetime)
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
    current_time = t_3
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
    understat_loader = UnderstatLoader(
        db_url,
        cfg,
        reader_factory=lambda lg, se: FakeUnderstatReader(kickoff_utc, understat_league=lg),
    )
    hist_res = job_refresh_history(
        session_factory=sm,
        understat_loader=understat_loader,
        toa_client=toa_client,
        config=cfg,
        now=t_post,
    )
    assert hist_res["status"] == "success"

    # Verify exactly ONE Match row exists, status is "finished", and MatchStat + Result belong to it
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

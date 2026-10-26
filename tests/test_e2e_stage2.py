"""E2E Stage 2 integration test: REST API + MCP tools + Streamlit Dashboard consistency.

Verifies that the entire user workflow works end-to-end through:
1. REST API endpoints (FastAPI TestClient)
2. FastMCP server tools (in-process calls)
3. Streamlit Dashboard views (AppTest)
And all three interfaces display consistent analytical numbers and disclaimers.
Zero manual inserts of OddsSnapshot or Result (all through scheduled pipelines).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from streamlit.testing.v1 import AppTest

from api.db import get_db
from api.main import app as rest_app
from core.config import load_config
from core.ingest.odds_api import EventsReport
from core.ingest.understat import UnderstatReport, UnderstatRow
from core.orm import Base, Match, MatchStat, OddsSnapshot, QuotaCounter, Team, TeamAlias
from core.scheduler.tasks import (
    job_poll_events,
    job_poll_odds_snapshots,
    job_refresh_history,
    job_settle_matches_and_clv,
)
from dashboard.api_client import ApiClient
from mcp_server.server import (
    evaluate_fortuna_odds,
    get_match_fair_prices,
    get_upcoming_matches,
    get_user_performance,
    record_user_bet,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
REAL_FORTUNA_FILE = FIXTURES_DIR / "fortuna_barcelona_real_2026-10-25.txt"
DASHBOARD_APP_PATH = str(Path(__file__).parent.parent / "dashboard" / "app.py")
AUTH_HEADER = {"Authorization": "Bearer test-e2e-token"}


class Stage2FakeTOAClient:
    """Mock TOA client for Stage 2 E2E pipeline."""

    def __init__(self, sm: sessionmaker[Session], match_time: dt.datetime) -> None:
        self.sm = sm
        self.match_time = match_time
        self.calls: list[dict[str, Any]] = []

    def fetch_events(self, sport_key: str) -> list[dict[str, Any]]:
        return [
            {
                "id": "ev_stage2_elclasico",
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

            m = session.scalar(select(Match).where(Match.toa_event_id == "ev_stage2_elclasico"))
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
                    toa_event_id="ev_stage2_elclasico",
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
            m = session.scalar(select(Match).where(Match.toa_event_id == "ev_stage2_elclasico"))
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
                    price=Decimal("1.952"),
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
                    price=Decimal("1.952"),
                    tier=tier,
                    planned_offset_min=1440 if tier == "T-24" else 180,
                    actual_offset_min=1440 if tier == "T-24" else 180,
                    source="toa",
                    captured_at_utc=capture_time,
                )
            )
            # Pinnacle h2h
            for outcome, price in [("Home", "2.100"), ("Draw", "3.500"), ("Away", "3.400")]:
                session.add(
                    OddsSnapshot(
                        toa_sport_key=sport_key,
                        toa_event_id=m.toa_event_id,
                        match_id=m.id,
                        bookmaker="pinnacle",
                        market="h2h",
                        line=None,
                        outcome=outcome,
                        price=Decimal(price),
                        tier=tier,
                        planned_offset_min=1440 if tier == "T-24" else 180,
                        actual_offset_min=1440 if tier == "T-24" else 180,
                        source="toa",
                        captured_at_utc=capture_time,
                    )
                )
            session.commit()
        return {"rows": 5, "credits": 2}


class Stage2FakeUnderstatLoader:
    """Mock Understat loader for Stage 2 score sync."""

    def __init__(self, sm: sessionmaker[Session]) -> None:
        self.sm = sm

    def schedule_rows(self, slug: str, year: int) -> list[UnderstatRow]:
        return [
            UnderstatRow(
                understat_id="99902",
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
            m = session.scalar(select(Match).where(Match.toa_event_id == "ev_stage2_elclasico"))
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


def test_e2e_stage2_multichannel_consistency(monkeypatch) -> None:
    # 0. Setup isolated in-memory database
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)
    cfg = load_config()

    monkeypatch.setenv("API_SECRET_TOKEN", "test-e2e-token")

    @contextmanager
    def _override_scope(url: str) -> Iterator[Session]:
        session = sm()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr("mcp_server.server.session_scope", _override_scope)

    def _override_db() -> Iterator[Session]:
        session = sm()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    rest_app.dependency_overrides[get_db] = _override_db

    # Seed teams & quotas
    with sm() as session:
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

    kickoff_utc = dt.datetime.now(dt.UTC) + dt.timedelta(hours=20)
    fake_toa = Stage2FakeTOAClient(sm, kickoff_utc)

    # Step 1: Create scheduled match via job_poll_events
    t_48 = kickoff_utc - dt.timedelta(hours=48)
    job_poll_events(
        sm,
        client=fake_toa,  # type: ignore[arg-type]
        config=cfg,
        now=t_48,
    )

    with sm() as session:
        match = session.scalar(select(Match).where(Match.toa_event_id == "ev_stage2_elclasico"))
        assert match is not None
        match_id = match.id

    # Step 2: Poll pre-kickoff Pinnacle snapshots via job_poll_odds_snapshots
    t_24 = kickoff_utc - dt.timedelta(hours=24)
    job_poll_odds_snapshots(
        sm,
        client=fake_toa,  # type: ignore[arg-type]
        config=cfg,
        now=t_24,
    )

    t_3 = kickoff_utc - dt.timedelta(hours=3)
    job_poll_odds_snapshots(
        sm,
        client=fake_toa,  # type: ignore[arg-type]
        config=cfg,
        now=t_3,
    )

    raw_fortuna = REAL_FORTUNA_FILE.read_text(encoding="utf-8")

    # 1. Test REST API Channel
    with TestClient(rest_app) as client:
        # Check health
        h_resp = client.get("/health")
        assert h_resp.status_code == 200
        h_data = h_resp.json()
        assert h_data["status"] == "ok"
        assert h_data["database_connected"] is True
        assert h_data["database_url_redacted"] is None

        # Check matches
        m_resp = client.get("/matches", headers=AUTH_HEADER)
        assert m_resp.status_code == 200
        matches_list = m_resp.json()["matches"]
        assert len(matches_list) == 1
        assert matches_list[0]["home_team"] == "Barcelona"

        # Check fair prices
        fp_resp = client.get(f"/matches/{match_id}/fair-prices", headers=AUTH_HEADER)
        assert fp_resp.status_code == 200
        fp_data = fp_resp.json()
        assert "exact_score" in fp_data
        assert len(fp_data["exact_score"]) > 10
        assert "inny" in fp_data

        # Evaluate Fortuna odds via REST API
        ev_resp = client.post(
            "/fortuna/evaluate",
            headers=AUTH_HEADER,
            json={"raw_text": raw_fortuna, "match_id": match_id},
        )
        assert ev_resp.status_code == 200
        ev_data = ev_resp.json()
        assert "Barcelona" in ev_data["match_name"]
        assert "Real Madrid" in ev_data["match_name"]
        assert len(ev_data["evaluations"]) > 10

        # Register UserBet 1 via REST API: exact_score 2:1 at odds 10.00
        bet1_resp = client.post(
            "/tracking/bets",
            headers=AUTH_HEADER,
            json={
                "match_id": match_id,
                "market": "exact_score",
                "outcome": "2:1",
                "odds": 10.00,
                "stake": 10.0,
                "bookmaker": "Fortuna",
            },
        )
        assert bet1_resp.status_code == 201
        assert bet1_resp.json()["status"] == "open"

    # 2. Test MCP Channel (in-process fastmcp tools)
    mcp_matches = get_upcoming_matches(days=3)
    assert "Barcelona — Real Madrid" in mcp_matches

    mcp_fair = get_match_fair_prices(match_id)
    assert "Справедливые котировки: Barcelona — Real Madrid" in mcp_fair

    mcp_eval = evaluate_fortuna_odds(raw_text=raw_fortuna, match_id=match_id)
    assert "Маржа букмекера по секциям" in mcp_eval
    assert "Без рекомендаций" in mcp_eval

    # Register UserBet 2 via MCP: totals over 2.5 at odds 1.95
    mcp_bet2 = record_user_bet(
        match_id=match_id,
        market="totals",
        outcome="over 2.5",
        line=2.5,
        odds=1.95,
        stake=10.0,
        bookmaker="Fortuna",
    )
    assert "Ставка успешно записана:" in mcp_bet2
    assert "over 2.5" in mcp_bet2

    # 3. Match finishes and official Result syncs via job_refresh_history
    t_post = kickoff_utc + dt.timedelta(hours=3)
    fake_understat = Stage2FakeUnderstatLoader(sm)
    job_refresh_history(
        session_factory=sm,
        understat_loader=fake_understat,  # type: ignore[arg-type]
        config=cfg,
        now=t_post,
    )

    settle_rep = job_settle_matches_and_clv(sm, config=cfg, dry_run=False, now=t_post)
    assert settle_rep["settled_bets"] == 2
    assert settle_rep["clv_calculated"] == 2

    # 4. Cross-Channel Performance Consistency Check
    with TestClient(rest_app) as client:
        perf_resp = client.get("/tracking/performance?period=all", headers=AUTH_HEADER)
        assert perf_resp.status_code == 200
        perf = perf_resp.json()

        assert perf["total_bets"] == 2
        assert perf["settled_bets"] == 2
        assert perf["pending_bets"] == 0
        assert perf["turnover"] == 20.0
        # Bet 1 (exact score 2:1): won -> pnl = 10 * 10 - 10 = +90.0
        # Bet 2 (totals over 2.5, 3 goals): won -> pnl = 10 * 1.95 - 10 = +9.5
        # Total profit = 99.5
        assert perf["total_profit"] == 99.5
        # ROI = 99.5 / 20 = 497.5%
        assert perf["roi"] == 497.5
        assert perf["hit_rate"] == 100.0
        assert "Ставки на спорт сопряжены с риском потери средств" in perf["disclaimer"]

    # Verify MCP performance report reflects identical metrics
    mcp_perf = get_user_performance(period="all")
    assert "Ставок: 2 (рассчитано: 2, в игре: 0)" in mcp_perf
    assert "Оборот: 20.00 ед. | P/L: +99.50 ед." in mcp_perf
    assert "ROI: +497.50%" in mcp_perf
    assert "Hit Rate: 100.0%" in mcp_perf
    assert "⚠️" in mcp_perf  # Sample size warning for N < 100

    # 5. Verify Streamlit Dashboard Renders Identical Metrics via ApiClient
    with patch.object(ApiClient, "get_health", return_value=h_data), \
         patch.object(ApiClient, "get_performance", return_value=perf):
        at = AppTest.from_file(DASHBOARD_APP_PATH, default_timeout=15.0)
        at.run()
        at.sidebar.radio[0].set_value("Статистика и CLV").run()
        assert not at.exception

        roi_metrics = [m for m in at.metric if m.label == "ROI"]
        assert len(roi_metrics) == 1
        assert roi_metrics[0].value == "+497.5%"

        pnl_metrics = [m for m in at.metric if "P/L" in m.label]
        assert len(pnl_metrics) == 1
        assert "+99.50 ед." in pnl_metrics[0].value

        warning_texts = [w.value for w in at.warning]
        assert any("статистически недостаточна" in w for w in warning_texts)

    rest_app.dependency_overrides.clear()

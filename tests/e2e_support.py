"""Shared helpers and fixtures for E2E integration tests."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

import httpx
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import core.ingest.odds_api
from core.config import AppConfig
from core.ingest.odds_api import OddsApiClient
from core.ingest.understat import UnderstatLoader
from core.orm import Base, QuotaCounter, Team, TeamAlias

DEFAULT_KICKOFF = dt.datetime(2026, 10, 25, 20, 0, tzinfo=dt.UTC)
DEFAULT_MATCH_EID = "ev_barca_real_20261025"


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


def build_toa_handler(
    kickoff_utc: dt.datetime,
    match_eid: str,
    get_odds_tier: Callable[[], str],
) -> Callable[[httpx.Request], httpx.Response]:
    """Builds a mock TOA handler serving /events, /odds, /scores."""
    commence_str = kickoff_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

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
            current_tier = get_odds_tier()
            price_over = 1.952 if current_tier == "T-24" else 1.930
            price_under = 1.952 if current_tier == "T-24" else 1.970
            price_home = 2.100 if current_tier == "T-24" else 2.050
            price_draw = 3.500 if current_tier == "T-24" else 3.550
            price_away = 3.400 if current_tier == "T-24" else 3.500

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
            raise AssertionError(f"Unexpected TOA request URL: {request.url}")

    return toa_handler


def init_e2e_db(
    db_url: str,
    kickoff_utc: dt.datetime = DEFAULT_KICKOFF,
) -> sessionmaker[Session]:
    """Initialize DB schema and seed teams/aliases/quota."""
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)

    now_base = kickoff_utc - dt.timedelta(hours=24)
    with sm() as session:
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

    return sm


def create_toa_client(
    db_url: str,
    cfg: AppConfig,
    handler: Callable[[httpx.Request], httpx.Response],
) -> OddsApiClient:
    """Instantiate OddsApiClient with mock transport."""
    mock_transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=mock_transport)
    return OddsApiClient(db_url, cfg, "k", client=http_client)


def create_understat_loader(
    db_url: str,
    cfg: AppConfig,
    kickoff_utc: dt.datetime = DEFAULT_KICKOFF,
) -> UnderstatLoader:
    """Instantiate UnderstatLoader with league-aware FakeUnderstatReader."""
    return UnderstatLoader(
        db_url,
        cfg,
        reader_factory=lambda lg, se: FakeUnderstatReader(kickoff_utc, understat_league=lg),
    )


class ClockPatcher:
    """Patches datetime / date across modules to return fixed current time."""

    def __init__(self, monkeypatch: Any) -> None:
        self.monkeypatch = monkeypatch
        self.current_time = dt.datetime(2026, 10, 24, 20, 0, tzinfo=dt.UTC)
        outer = self

        class MockDatetime(dt.datetime):
            @classmethod
            def now(cls, tz: Any = None) -> dt.datetime:
                return outer.current_time

        class MockDateModule:
            def __getattr__(self, name: str) -> Any:
                if name == "today":
                    return lambda: outer.current_time.date()
                return getattr(dt.date, name)

        class MockDtModule:
            date = MockDateModule()
            datetime = MockDatetime

            def __getattr__(self, name: str) -> Any:
                return getattr(dt, name)

        mock_module = MockDtModule()
        self.monkeypatch.setattr(core.ingest.odds_api.dt, "datetime", MockDatetime)
        self.monkeypatch.setattr("api.routes.matches.dt", mock_module)
        self.monkeypatch.setattr("mcp_server.server.dt", mock_module)

    def set_now(self, now: dt.datetime) -> None:
        self.current_time = now

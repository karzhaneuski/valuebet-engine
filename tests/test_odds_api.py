"""TOA client: events/postponements, snapshots from fixtures, scores."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from core.config import load_config
from core.db import get_engine, session_scope
from core.ingest.odds_api import OddsApiClient
from core.orm import Base, Match, OddsSnapshot, Result, Team, TeamAlias

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://api.the-odds-api.com/v4"


def make_client(tmp_path):
    url = f"sqlite:///{tmp_path}/toa.db"
    Base.metadata.create_all(get_engine(url))
    config = load_config()
    return url, config, OddsApiClient(url, config, "test-key")


def seed_teams(url: str) -> None:
    with session_scope(url) as s:
        seeded = {
            "Ipswich Town": "premier_league",
            "Nottingham Forest": "premier_league",
            "Aston Villa": "premier_league",
            "Manchester City": "premier_league",
            "Arsenal": "premier_league",
            "Everton": "premier_league",
            "Bayern Munich": None,
        }
        teams = {}
        for name, league in seeded.items():
            t = Team(name=name, league=league)
            s.add(t)
            teams[name] = t
        s.flush()
        for name, t in teams.items():
            s.add(TeamAlias(team_id=t.id, source="toa", alias=name))


def test_apply_events_creates_updates_and_flags_shift(tmp_path) -> None:
    url, _config, client = make_client(tmp_path)
    seed_teams(url)
    events = json.loads((FIXTURES / "toa_events.json").read_text())[
        "soccer_epl"]

    r1 = client.apply_events("soccer_epl", events, "premier_league", 2026)
    assert r1.matches_created == 3
    with session_scope(url) as s:
        m = s.scalars(select(Match).where(
            Match.toa_event_id == str(events[0]["id"]))).one()
        assert m.kickoff_source == "toa"
        assert m.commence_time_utc is not None

    # kickoff shifted by > 15 min -> postponed_suspected
    shifted = [dict(e) for e in events]
    shifted[0]["commence_time"] = "2026-11-27T21:00:00Z"
    r2 = client.apply_events("soccer_epl", shifted, "premier_league", 2026)
    assert r2.postponed_suspected == 1
    with session_scope(url) as s:
        m = s.scalars(select(Match).where(
            Match.toa_event_id == str(events[0]["id"]))).one()
        assert m.status == "postponed_suspected"


def test_apply_events_disappeared_event_flagged(tmp_path) -> None:
    url, _config, client = make_client(tmp_path)
    seed_teams(url)
    events = json.loads((FIXTURES / "toa_events.json").read_text())[
        "soccer_epl"]
    client.apply_events("soccer_epl", events, "premier_league", 2026)
    # now the event list shrinks: the vanished near-horizon match is
    # suspected postponed
    with session_scope(url) as s:
        m = s.scalars(select(Match).where(
            Match.toa_event_id == str(events[0]["id"]))).one()
        m.commence_time_utc = dt.datetime.now(dt.UTC) + dt.timedelta(hours=2)
        m.status = "scheduled"
    r = client.apply_events("soccer_epl", events[1:], "premier_league", 2026)
    with session_scope(url) as s:
        m = s.scalars(select(Match).where(
            Match.toa_event_id == str(events[0]["id"]))).one()
        assert m.status == "postponed_suspected"
        assert r.postponed_suspected >= 1


@respx.mock
def test_snapshot_sport_totals_from_recorded_fixture(tmp_path) -> None:
    url, _config, client = make_client(tmp_path)
    seed_teams(url)
    events = json.loads((FIXTURES / "toa_events.json").read_text())[
        "soccer_epl"]
    client.apply_events("soccer_epl", events, "premier_league", 2026)
    # synthetic-but-shaped sport-level totals body (structure as recorded
    # in phase 0; values rounded)
    body = [{
        "id": events[0]["id"], "commence_time": events[0]["commence_time"],
        "home_team": events[0]["home_team"],
        "away_team": events[0]["away_team"],
        "bookmakers": [
            {"key": "pinnacle", "title": "Pinnacle", "markets": [
                {"key": "totals", "outcomes": [
                    {"name": "Over", "price": 1.95, "point": 2.5},
                    {"name": "Under", "price": 2.05, "point": 2.5},
                    {"name": "Over", "price": 1.30, "point": 3.0},
                ]}]},
        ]}]
    respx.get(f"{BASE}/sports/soccer_epl/odds").mock(
        return_value=httpx.Response(200, json=body,
                                    headers={"x-requests-remaining": "479"}))
    result = client.snapshot_sport_totals("soccer_epl", "T-24")
    assert result["credits"] == 1 and result["rows"] == 2  # 3.0 dropped
    with session_scope(url) as s:
        snaps = s.scalars(select(OddsSnapshot)).all()
        assert len(snaps) == 2
        # fixture kickoff is in the past -> S4 marks the tier off_window
        assert all(sn.tier == "off_window" for sn in snaps)
        assert all(sn.bookmaker == "pinnacle" for sn in snaps)  # S5: TOA key
        # B1 reconcile: the provider header (479 remaining) wins
        assert client.quota.monthly_used() == 500 - 479


@respx.mock
def test_snapshot_event_markets_btts_fixture(tmp_path) -> None:
    url, _config, client = make_client(tmp_path)
    seed_teams(url)
    event_body = json.loads(
        (FIXTURES / "toa_event_odds_btts.json").read_text())
    eid = event_body["id"]
    with session_scope(url) as s:
        home = s.scalars(select(Team).where(
            Team.name == "Ipswich Town")).one()
        away = s.scalars(select(Team).where(
            Team.name == "Nottingham Forest")).one()
        s.add(Match(league="premier_league", season=2026,
                    date_utc=dt.date(2026, 10, 23), kickoff_source="toa",
                    home_team_id=home.id, away_team_id=away.id,
                    toa_event_id=eid,
                    commence_time_utc=dt.datetime.now(dt.UTC)))
    respx.get(f"{BASE}/sports/soccer_epl/events/{eid}/odds").mock(
        return_value=httpx.Response(200, json=event_body,
                                    headers={"x-requests-remaining": "478"}))
    result = client.snapshot_event_markets(
        "soccer_epl", eid, ["btts"], "T-3")
    assert result["credits"] == 1
    with session_scope(url) as s:
        snaps = s.scalars(select(OddsSnapshot).where(
            OddsSnapshot.market == "btts")).all()
        assert len(snaps) == len(event_body["bookmakers"]) * 2
        outcomes = {sn.outcome for sn in snaps}
        assert outcomes == {"Yes", "No"}


@respx.mock
def test_scores_writes_results(tmp_path) -> None:
    url, _config, client = make_client(tmp_path)
    seed_teams(url)
    with session_scope(url) as s:
        bayern = s.scalars(select(Team).where(
            Team.name == "Bayern Munich")).one()
        arsenal = s.scalars(select(Team).where(Team.name == "Arsenal")).one()
        s.add(Match(league="ucl", season=2026, stage="league_phase",
                    date_utc=dt.date(2026, 10, 21), kickoff_source="toa",
                    home_team_id=bayern.id, away_team_id=arsenal.id,
                    toa_event_id="ucl-1", status="scheduled",
                    commence_time_utc=dt.datetime(2026, 10, 21, 19, 0,
                                                  tzinfo=dt.UTC)))
    body = [{"id": "ucl-1", "completed": True,
             "home_team": "Bayern Munich", "away_team": "Arsenal",
             "scores": [{"name": "Bayern Munich", "score": "2"},
                        {"name": "Arsenal", "score": "1"}]}]
    respx.get(f"{BASE}/sports/soccer_uefa_champs_league/scores").mock(
        return_value=httpx.Response(200, json=body,
                                    headers={"x-requests-remaining": "477"}))
    result = client.apply_scores(client.fetch_scores(days_from=3))
    assert result["results_written"] == 1
    with session_scope(url) as s:
        m = s.scalars(select(Match).where(Match.toa_event_id == "ucl-1")).one()
        assert m.status == "finished"
        r = s.scalars(select(Result)).one()
        assert (r.home_goals, r.away_goals) == (2, 1)


def test_quota_guard_blocks_snapshot_at_stop(tmp_path) -> None:
    url, config, client = make_client(tmp_path)
    from core.orm import QuotaCounter

    with session_scope(url) as s:
        s.add(QuotaCounter(
            provider="toa", period=dt.datetime.now(dt.UTC).strftime("%Y-%m"),
            credits_used=config.odds_policy.hard_stop_credits,
            updated_at_utc=dt.datetime.now(dt.UTC)))
    with pytest.raises(Exception, match="hard stop"):
        client.snapshot_sport_totals("soccer_epl", "T-24")


@respx.mock
def test_scores_creates_missing_ucl_match(tmp_path) -> None:
    """UCL matchdays drop out of /events before /scores shows them: the
    scores payload itself must create the match for settlement."""
    url, _config, client = make_client(tmp_path)
    body = [{"id": "ucl-42", "completed": True,
             "home_team": "Real Madrid", "away_team": "AC Milan",
             "commence_time": "2026-10-21T19:00:00Z",
             "scores": [{"name": "Real Madrid", "score": "3"},
                        {"name": "AC Milan", "score": "1"}]}]
    respx.get(f"{BASE}/sports/soccer_uefa_champs_league/scores").mock(
        return_value=httpx.Response(200, json=body,
                                    headers={"x-requests-remaining": "470"}))
    result = client.apply_scores(client.fetch_scores(days_from=3))
    assert result["results_written"] == 1
    with session_scope(url) as s:
        m = s.scalars(select(Match).where(
            Match.toa_event_id == "ucl-42")).one()
        assert m.league == "ucl" and m.status == "finished"
        assert m.home_team.name == "Real Madrid"
        assert m.season == 2026

"""S9 (odds-snapshot without --event-id) and S11 (secret redaction
patterns for every key format)."""

from __future__ import annotations

import datetime as dt

from core.db import get_engine, session_scope
from core.http import redact_text, register_secret_values
from core.ingest.__main__ import current_season, pick_latest_event_id
from core.orm import Base, Match, Team


def make_db(tmp_path):
    url = f"sqlite:///{tmp_path}/cli.db"
    Base.metadata.create_all(get_engine(url))
    return url


def test_s9_pick_latest_event_id(tmp_path) -> None:
    url = make_db(tmp_path)
    with session_scope(url) as s:
        t1, t2, t3 = Team(name="A"), Team(name="B"), Team(name="C")
        s.add_all([t1, t2, t3])
        s.flush()
        for day, eid, home, away in ((10, "old", t1, t2),
                                     (24, "new", t1, t3)):
            s.add(Match(league="premier_league", season=2026,
                        date_utc=dt.date(2026, 10, day),
                        commence_time_utc=dt.datetime(2026, 10, day, 15, 0,
                                                      tzinfo=dt.UTC),
                        kickoff_source="toa", status="scheduled",
                        home_team_id=home.id, away_team_id=away.id,
                        toa_event_id=eid))
    assert pick_latest_event_id(url, "premier_league") == "new"
    assert pick_latest_event_id(url, "ligue_1") is None


def test_s9_parser_accepts_snapshot_without_event_id() -> None:

    from core.ingest.__main__ import build_parser

    parser = build_parser()
    args = parser.parse_args(["odds-snapshot", "--league", "la_liga",
                              "--tier", "T-24"])
    assert args.league == "la_liga" and args.event_id is None
    assert args.markets == ["totals"]


def test_s11_every_key_format_redacted() -> None:
    fake = "A1b2C3d4E5f6G7h8I9j0K1l2M3"  # 26 chars, fake
    samples = [
        f"ODDS_API_KEY={fake}",
        f'API_FOOTBALL_KEY="{fake}"',
        f"TAVILY_API_KEY: {fake}",
        f'"BRAVE_SEARCH_API_KEY": "{fake}"',
        f"GEMINI_API_KEY=AIza{fake}",
        f"API_TOKEN={fake}",
        f"api_key={fake}",
        f"apiKey:{fake}",
        f"token = {fake}",
        f"AIza{fake}",           # Gemini key format with dots support
        f"tvly-{fake}",
        f"BSA{fake}",
    ]
    for text in samples:
        out = redact_text(text)
        assert fake not in out, f"leaked in: {text}"


def test_s11_registered_runtime_values_redacted_everywhere() -> None:
    secret = "runtime-secret-value-123456"
    register_secret_values([secret])
    assert redact_text(f"GET /x?apiKey={secret}") == "GET /x?apiKey=***"
    assert redact_text(f"value {secret} hidden") == "value *** hidden"


def test_s9_current_season() -> None:
    assert current_season(dt.datetime(2026, 10, 24, tzinfo=dt.UTC)) == 2026
    assert current_season(dt.datetime(2026, 6, 1, tzinfo=dt.UTC)) == 2025
    assert current_season(dt.datetime(2026, 7, 1, tzinfo=dt.UTC)) == 2026

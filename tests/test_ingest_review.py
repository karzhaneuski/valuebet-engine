"""Review B3/B4/B5/B6/S3/S6/S7/S10 behaviour tests (mocked, no network)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select

from core.config import load_config
from core.db import get_engine, session_scope
from core.ingest.checks import future_kickoff_data_violations
from core.ingest.football_data import FootballDataLoader
from core.ingest.odds_api import OddsApiClient
from core.ingest.understat import UnderstatLoader, UnderstatRow
from core.orm import Base, ClosingOdds, Match, MatchLineup, MatchStat, OddsSnapshot, Team, TeamAlias

UTC = dt.UTC
NOW = dt.datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def make_db(tmp_path):
    url = f"sqlite:///{tmp_path}/rev.db"
    Base.metadata.create_all(get_engine(url))
    return url


def seed_pair(url, home="Home FC", away="Away FC", league="premier_league",
              season=2026, status="scheduled",
              kickoff=None, understat_id=1, kickoff_source="toa"):
    with session_scope(url) as s:
        def team(name):
            t = s.scalars(select(Team).where(Team.name == name)).first()
            if t is None:
                t = Team(name=name, league=league)
                s.add(t)
                s.flush()
            return t

        h, a = team(home), team(away)
        for t, name in ((h, home), (a, away)):
            for source in ("understat", "toa"):
                exists = s.scalars(select(TeamAlias).where(
                    TeamAlias.source == source,
                    TeamAlias.alias == name)).first()
                if exists is None:
                    s.add(TeamAlias(team_id=t.id, source=source, alias=name))
        s.add(Match(league=league, season=season,
                    date_utc=(kickoff or NOW).date(),
                    commence_time_utc=kickoff or NOW,
                    kickoff_source=kickoff_source, status=status,
                    home_team_id=h.id, away_team_id=a.id,
                    understat_id=understat_id))
        s.flush()


# --- B3: vanished event rule only for FUTURE kickoffs ------------------------

def test_b3_past_kickoff_vanished_event_unchanged(tmp_path) -> None:
    url = make_db(tmp_path)
    started_3h_ago = NOW - dt.timedelta(hours=3)
    seed_pair(url, kickoff=started_3h_ago, status="scheduled",
              kickoff_source="toa", understat_id=11)
    client = OddsApiClient(url, load_config(), "k")
    with session_scope(url) as s:
        m = s.scalars(select(Match)).one()
        m.toa_event_id = "gone-1"
    report = client.apply_events("soccer_epl", [], "premier_league", 2026, now=NOW)
    with session_scope(url) as s:
        m = s.scalars(select(Match)).one()
        assert m.status == "scheduled"  # KO+3h is phase-7 territory
    assert report.postponed_suspected == 0


def test_b3_future_kickoff_vanished_event_flagged(tmp_path) -> None:
    url = make_db(tmp_path)
    seed_pair(url, kickoff=NOW + dt.timedelta(hours=6), status="scheduled",
              kickoff_source="toa", understat_id=12)
    client = OddsApiClient(url, load_config(), "k")
    with session_scope(url) as s:
        s.scalars(select(Match)).one().toa_event_id = "gone-2"
    client.apply_events("soccer_epl", [], "premier_league", 2026, now=NOW)
    with session_scope(url) as s:
        assert s.scalars(select(Match)).one().status == "postponed_suspected"


# --- B4: upsert updates without duplicates -------------------------------------

def _us_row(home="Home FC", away="Away FC", played=True,
            kickoff=dt.datetime(2026, 10, 1, 14, 0)):
    return UnderstatRow(
        league_slug="premier_league", season=2026, understat_id="77",
        home=home, away=away, naive_start=kickoff, is_result=played,
        home_goals=2 if played else None, away_goals=1 if played else None,
        home_xg=1.8 if played else None, away_xg=0.9 if played else None)


def test_b4_understat_upsert_updates_status_and_stats(tmp_path) -> None:
    url = make_db(tmp_path)
    config = load_config()
    loader = UnderstatLoader(url, config)
    ko = dt.datetime(2026, 10, 1, 14, 0, tzinfo=UTC)
    seed_pair(url, kickoff=ko, status="scheduled",
              kickoff_source="understat_placeholder", understat_id=77)

    r1 = loader.upsert_schedule([_us_row(kickoff=ko)])
    assert r1.matches_existing == 1 and r1.stats_written == 1
    with session_scope(url) as s:
        m = s.scalars(select(Match)).one()
        assert m.status == "finished"  # scheduled -> finished (B4)

    # second load with a corrected score updates, no duplicates
    row2 = _us_row(kickoff=ko)
    row2.home_goals = 3
    r2 = loader.upsert_schedule([row2])
    assert r2.stats_updated == 1
    with session_scope(url) as s:
        assert len(s.scalars(select(MatchStat)).all()) == 1
        assert s.scalars(select(MatchStat)).one().home_goals == 3
        assert len(s.scalars(select(Match)).all()) == 1


def test_b4_toa_kickoff_never_overwritten(tmp_path) -> None:
    url = make_db(tmp_path)
    config = load_config()
    loader = UnderstatLoader(url, config)
    ko_toa = dt.datetime(2026, 10, 1, 16, 0, tzinfo=UTC)
    seed_pair(url, kickoff=ko_toa, status="scheduled",
              kickoff_source="toa", understat_id=78)
    row = _us_row(kickoff=dt.datetime(2026, 10, 1, 15, 0))
    row.understat_id = "78"
    loader.upsert_schedule([row])
    with session_scope(url) as s:
        m = s.scalars(select(Match)).one()
        assert m.commence_time_utc == ko_toa  # authoritative source kept
        assert m.kickoff_source == "toa"


def test_b4_fd_closing_prices_update(tmp_path) -> None:
    url = make_db(tmp_path)
    config = load_config()
    loader = FootballDataLoader(url, config, cache_dir=tmp_path / "fd")
    seed_pair(url, kickoff=dt.datetime(2026, 10, 1, 15, 0, tzinfo=UTC),
              status="finished", kickoff_source="toa", understat_id=79)
    from core.ingest.football_data import FdRow

    def row(over: str):
        return FdRow(league_slug="premier_league", season=2026,
                     date=dt.date(2026, 10, 1), time=dt.time(15, 0),
                     home="Home FC", away="Away FC", fthg=2, ftag=1,
                     avgc_over=Decimal(over), avgc_under=Decimal("1.9"),
                     maxc_over=None, maxc_under=None,
                     pc_over=None, pc_under=None)

    r1 = loader.upsert([row("1.95")])
    assert r1.closing_written == 1  # only AvgC present in the row
    r2 = loader.upsert([row("1.85")])
    assert r2.closing_updated >= 1
    with session_scope(url) as s:
        prices = s.scalars(select(ClosingOdds).where(
            ClosingOdds.bookmaker == "AvgC")).all()
        assert len(prices) == 1 and prices[0].over_price == Decimal("1.85")


# --- B5: inverted pair + quarantine + invariant -------------------------------

def test_b5_inverted_pair_reported_not_bound(tmp_path) -> None:
    """fd 'Rennes v Paris SG' 23.08: the direct pair matches the March
    return leg (match 10559) — the same-date inverted fixture must win and
    nothing may be written to the far-away match."""
    url = make_db(tmp_path)
    config = load_config()
    loader = FootballDataLoader(url, config, cache_dir=tmp_path / "fd")
    # the August fixture as Understat sees it (PSG home)
    seed_pair(url, home="Paris Saint Germain", away="Rennes",
              season=2026, status="finished",
              kickoff=dt.datetime(2026, 8, 23, 20, 0, tzinfo=UTC),
              understat_id=10363)
    # the March return leg (Rennes home) — the dangerous direct-pair hit
    seed_pair(url, home="Rennes", away="Paris Saint Germain",
              season=2026, status="scheduled",
              kickoff=dt.datetime(2027, 3, 6, 20, 0, tzinfo=UTC),
              understat_id=10559)
    from core.ingest.football_data import FdRow

    inverted_row = FdRow(
        league_slug="premier_league", season=2026, date=dt.date(2026, 8, 23),
        time=dt.time(20, 0), home="Rennes", away="Paris Saint Germain",
        fthg=1, ftag=2, avgc_over=Decimal("1.9"), avgc_under=Decimal("1.9"),
        maxc_over=None, maxc_under=None, pc_over=None, pc_under=None)
    report = loader.upsert([inverted_row])
    assert report.inverted_pairs and not report.quarantined
    with session_scope(url) as s:
        march = s.scalars(select(Match).where(
            Match.understat_id == 10559)).one()
        assert not s.scalars(select(MatchStat).where(
            MatchStat.match_id == march.id)).first()
        assert not s.scalars(select(ClosingOdds).where(
            ClosingOdds.match_id == march.id)).first()
    assert future_kickoff_data_violations(url, now=NOW) == []


def test_b5_date_mismatch_quarantined_without_matching_score(tmp_path) -> None:
    url = make_db(tmp_path)
    config = load_config()
    loader = FootballDataLoader(url, config, cache_dir=tmp_path / "fd")
    seed_pair(url, kickoff=dt.datetime(2026, 9, 20, 15, 0, tzinfo=UTC),
              status="scheduled", understat_id=80)
    from core.ingest.football_data import FdRow

    off_date = FdRow(
        league_slug="premier_league", season=2026, date=dt.date(2026, 8, 23),
        time=dt.time(15, 0), home="Home FC", away="Away FC", fthg=2, ftag=1,
        avgc_over=None, avgc_under=None, maxc_over=None, maxc_under=None,
        pc_over=None, pc_under=None)
    report = loader.upsert([off_date])
    # >1 day off AND no matching understat score -> quarantine, no write
    assert report.quarantined and report.stats_written == 0


def test_b5_date_mismatch_written_when_finished_and_score_agrees(tmp_path) -> None:
    url = make_db(tmp_path)
    config = load_config()
    loader = FootballDataLoader(url, config, cache_dir=tmp_path / "fd")
    seed_pair(url, kickoff=dt.datetime(2026, 9, 20, 15, 0, tzinfo=UTC),
              status="finished", understat_id=81)
    with session_scope(url) as s:
        m = s.scalars(select(Match)).one()
        s.add(MatchStat(match_id=m.id, source="understat", home_goals=2,
                        away_goals=1))
    from core.ingest.football_data import FdRow

    off_date = FdRow(
        league_slug="premier_league", season=2026, date=dt.date(2026, 8, 23),
        time=dt.time(15, 0), home="Home FC", away="Away FC", fthg=2, ftag=1,
        avgc_over=None, avgc_under=None, maxc_over=None, maxc_under=None,
        pc_over=None, pc_under=None)
    report = loader.upsert([off_date])
    assert not report.quarantined and report.stats_written == 1


# --- B6/S10: toa alias, relink, un-suspect, placeholder ------------------------

def _event(eid, home, away, when):
    return {"id": eid, "commence_time": when.isoformat().replace("+00:00", "Z"),
            "home_team": home, "away_team": away}


def test_b6_pair_match_links_alias_and_snapshots(tmp_path) -> None:
    url = make_db(tmp_path)
    ko = dt.datetime(2026, 10, 26, 15, 0, tzinfo=UTC)
    seed_pair(url, kickoff=ko, status="scheduled",
              kickoff_source="understat_placeholder", understat_id=90)
    # an earlier snapshot stored against the event id without a match link
    with session_scope(url) as s:
        s.add(OddsSnapshot(toa_sport_key="soccer_epl", toa_event_id="e-90",
                           match_id=None, bookmaker="pinnacle",
                           market="totals", line=Decimal("2.5"),
                           outcome="Over", price=Decimal("1.95"),
                           tier="T-48", planned_offset_min=2400,
                           actual_offset_min=2500, source="toa",
                           captured_at_utc=NOW))
    client = OddsApiClient(url, load_config(), "k")
    report = client.apply_events(
        "soccer_epl", [_event("e-90", "Home FC", "Away FC", ko)],
        "premier_league", 2026, now=NOW)
    assert report.matches_created == 0  # matched by pair, not created
    with session_scope(url) as s:
        m = s.scalars(select(Match)).one()
        assert m.toa_event_id == "e-90"
        snap = s.scalars(select(OddsSnapshot)).one()
        assert snap.match_id == m.id  # B6 re-link
        alias = s.scalars(select(TeamAlias).where(
            TeamAlias.source == "toa", TeamAlias.alias == "Home FC")).first()
        assert alias is not None  # B6 alias remembered


def test_s10_placeholder_correction_is_not_postponement(tmp_path) -> None:
    url = make_db(tmp_path)
    placeholder_ko = dt.datetime(2026, 10, 26, 15, 0, tzinfo=UTC)
    seed_pair(url, kickoff=placeholder_ko, status="scheduled",
              kickoff_source="understat_placeholder", understat_id=91)
    real_ko = dt.datetime(2026, 10, 26, 20, 0, tzinfo=UTC)  # 5 h later
    client = OddsApiClient(url, load_config(), "k")
    report = client.apply_events(
        "soccer_epl", [_event("e-91", "Home FC", "Away FC", real_ko)],
        "premier_league", 2026, now=NOW)
    with session_scope(url) as s:
        m = s.scalars(select(Match)).one()
        assert m.status == "scheduled"  # placeholder correction, not a shift
        assert m.commence_time_utc == real_ko
    assert report.postponed_suspected == 0


def test_s10_toa_to_toa_shift_is_postponement(tmp_path) -> None:
    url = make_db(tmp_path)
    ko = dt.datetime(2026, 10, 26, 15, 0, tzinfo=UTC)
    seed_pair(url, kickoff=ko, status="scheduled",
              kickoff_source="toa", understat_id=92)
    with session_scope(url) as s:
        s.scalars(select(Match)).one().toa_event_id = "e-92"
    shifted = ko + dt.timedelta(hours=2)
    client = OddsApiClient(url, load_config(), "k")
    report = client.apply_events(
        "soccer_epl", [_event("e-92", "Home FC", "Away FC", shifted)],
        "premier_league", 2026, now=NOW)
    with session_scope(url) as s:
        assert s.scalars(select(Match)).one().status == "postponed_suspected"
    assert report.postponed_suspected == 1

    # S12: a repeat poll with the SAME (shifted) time must NOT clear the
    # flag — the postponement is remembered (original kickoff + when seen)
    stable = client.apply_events(
        "soccer_epl", [_event("e-92", "Home FC", "Away FC", shifted)],
        "premier_league", 2026, now=NOW)
    with session_scope(url) as s:
        m = s.scalars(select(Match)).one()
        assert m.status == "postponed_suspected"  # survives the loop
        assert m.original_kickoff_utc is not None
        assert m.rescheduled_at_utc is not None
    assert stable.un_suspected == 0


# --- S3: UCL pair search before create -----------------------------------------

def test_s3_ucl_pair_search_prevents_duplicate(tmp_path) -> None:
    url = make_db(tmp_path)
    ko = dt.datetime(2026, 11, 3, 20, 0, tzinfo=UTC)
    seed_pair(url, league="ucl", kickoff=ko, status="scheduled",
              understat_id=93, kickoff_source="toa")
    with session_scope(url) as s:
        s.scalars(select(Match)).one().toa_event_id = "ucl-old"
    client = OddsApiClient(url, load_config(), "k")
    report = client.apply_events(
        "soccer_uefa_champs_league",
        [_event("ucl-new", "Home FC", "Away FC", ko + dt.timedelta(hours=1))],
        "ucl", 2026, now=NOW)
    with session_scope(url) as s:
        matches = s.scalars(select(Match)).all()
        assert len(matches) == 1  # pair+date found, no duplicate
        assert matches[0].toa_event_id == "ucl-new"  # re-linked
    assert report.matches_created == 0


# --- S6: lineups all-or-nothing ---------------------------------------------------

def test_s6_lineup_unmatched_team_writes_nothing(tmp_path) -> None:
    url = make_db(tmp_path)
    config = load_config()
    loader = UnderstatLoader(url, config)
    ko = dt.datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    seed_pair(url, kickoff=ko, status="finished", understat_id=94)
    with session_scope(url) as s:
        match = s.scalars(select(Match)).one()
        match_id = match.id
    rows = [{"team": "Home FC", "player": "A", "understat_player_id": 1,
             "started": True, "minutes": 90},
            {"team": "Totally Unknown", "player": "B",
             "understat_player_id": 2, "started": False, "minutes": 10}]
    written = loader.upsert_lineups(type("M", (), {"id": match_id})(), rows)
    assert written == 0
    with session_scope(url) as s:
        assert s.scalars(select(MatchLineup)).first() is None


# --- S7: fd London midnight conversions -------------------------------------------

def test_s7_fd_midnight_gmt_and_bst() -> None:
    from zoneinfo import ZoneInfo

    london = ZoneInfo("Europe/London")
    # 2 Jan (GMT): 00:15 London == 00:15 UTC, same date
    gmt = dt.datetime(2026, 1, 2, 0, 15, tzinfo=london).astimezone(dt.UTC)
    assert (gmt.hour, gmt.day) == (0, 2)
    # 2 Jul (BST=UTC+1): 00:15 London == 23:15 UTC on the PREVIOUS day
    bst = dt.datetime(2026, 7, 2, 0, 15, tzinfo=london).astimezone(dt.UTC)
    assert (bst.hour, bst.day) == (23, 1)


# --- B2 (final round): TOA never creates top-5 teams --------------------------

def test_b2_toa_brighton_binds_without_creating(tmp_path) -> None:
    """TOA 'Brighton and Hove Albion' against a roster whose team is
    aliased 'Brighton' (understat/fd): bidirectional containment binds to
    the EXISTING team; zero teams/matches created."""
    url = make_db(tmp_path)
    ko = dt.datetime(2026, 10, 28, 19, 0, tzinfo=dt.UTC)
    with session_scope(url) as s:
        brighton = Team(name="Brighton", league="premier_league")
        liverpool = Team(name="Liverpool", league="premier_league")
        s.add_all([brighton, liverpool])
        s.flush()
        for t, name in ((brighton, "Brighton"), (liverpool, "Liverpool")):
            s.add(TeamAlias(team_id=t.id, source="understat", alias=name))
            s.add(TeamAlias(team_id=t.id, source="football_data",
                            alias=name))
        s.add(Match(league="premier_league", season=2026,
                    date_utc=ko.date(), commence_time_utc=ko,
                    kickoff_source="understat_placeholder",
                    status="scheduled", home_team_id=liverpool.id,
                    away_team_id=brighton.id, understat_id=500))
    client = OddsApiClient(url, load_config(), "k")
    report = client.apply_events(
        "soccer_epl",
        [_event("e-b2", "Liverpool", "Brighton and Hove Albion", ko)],
        "premier_league", 2026, now=NOW)
    with session_scope(url) as s:
        assert len(s.scalars(select(Team)).all()) == 2  # nothing created
        assert len(s.scalars(select(Match)).all()) == 1
        m = s.scalars(select(Match)).one()
        assert m.toa_event_id == "e-b2"
        assert m.away_team.name == "Brighton"
        toa_alias = s.scalars(select(TeamAlias).where(
            TeamAlias.source == "toa",
            TeamAlias.alias == "Brighton and Hove Albion")).one()
        assert toa_alias.team_id == m.away_team_id
    assert report.matches_created == 0
    assert report.unmatched_teams == []
    assert report.teams_created == []


def test_b2_toa_unmatched_in_top5_reported_not_created(tmp_path) -> None:
    url = make_db(tmp_path)
    ko = dt.datetime(2026, 10, 28, 19, 0, tzinfo=dt.UTC)
    seed_pair(url, kickoff=ko, status="scheduled", understat_id=501)
    client = OddsApiClient(url, load_config(), "k")
    report = client.apply_events(
        "soccer_epl",
        [_event("e-x", "Home FC", "Mysterious Unknown XI", ko)],
        "premier_league", 2026, now=NOW)
    with session_scope(url) as s:
        assert len(s.scalars(select(Team)).all()) == 2  # nothing created
    assert report.unmatched_teams == ["Mysterious Unknown XI"]


# --- S6: lineups backfill takes only unloaded matches ---------------------------

def test_s6_backfill_query_skips_loaded_matches(tmp_path) -> None:
    """The pending query (NOT EXISTS lineups) is applied BEFORE any
    download: a loaded match must not appear in the candidate list."""

    url = make_db(tmp_path)
    ko = dt.datetime(2026, 10, 20, 15, 0, tzinfo=dt.UTC)
    seed_pair(url, kickoff=ko, status="finished", understat_id=601)
    with session_scope(url) as s:
        m = s.scalars(select(Match)).one()
        s.add(MatchLineup(match_id=m.id, team_id=m.home_team_id,
                          player="Someone", started=True, minutes=90))
    with session_scope(url) as s:
        has_lineups = select(MatchLineup.match_id).where(
            MatchLineup.match_id == Match.id).exists()
        pending = s.scalars(select(Match).where(
            Match.status == "finished",
            Match.understat_id.isnot(None),
            ~has_lineups)).all()
        assert pending == []  # the loaded match is filtered out early

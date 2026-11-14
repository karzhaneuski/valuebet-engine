"""Unit and behavioral tests for TOA polling schedule, deduplication, and pacing degradation."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from core.config import AppConfig, load_config
from core.ingest.odds_api import OddsApiClient
from core.orm import ApiQuotaUsage, Base, Match, OddsPollAttempt, OddsSnapshot, QuotaCounter, Team
from core.scheduler.tasks import job_poll_odds_snapshots


class FakeTOAClient:
    """Mock TOA client recording calls and writing synthetic snapshots."""

    def __init__(self, sm: sessionmaker[Session], sport_key: str = "soccer_spain_la_liga") -> None:
        self.sm = sm
        self.sport_key = sport_key
        self.calls: list[dict[str, Any]] = []

    def snapshot_sport_odds(
        self, sport_key: str, tier: str, markets: list[str] | None = None
    ) -> dict[str, Any]:
        mkts = markets or ["totals", "h2h"]
        self.calls.append({"sport_key": sport_key, "tier": tier, "markets": mkts})

        with self.sm() as session:
            # Find scheduled matches without a snapshot for this tier
            matches = list(
                session.scalars(
                    select(Match)
                    .outerjoin(
                        OddsSnapshot,
                        (OddsSnapshot.match_id == Match.id) & (OddsSnapshot.tier == tier),
                    )
                    .where(
                        Match.league == "la_liga",
                        OddsSnapshot.id.is_(None),
                    )
                ).all()
            )
            if matches:
                matches.sort(key=lambda m: m.commence_time_utc or dt.datetime.min)
                first_ko = matches[0].commence_time_utc
                slot_matches = [m for m in matches if m.commence_time_utc == first_ko]
                for m in slot_matches:
                    snap = OddsSnapshot(
                        toa_sport_key=sport_key,
                        toa_event_id=m.toa_event_id or f"ev_{m.id}",
                        match_id=m.id,
                        bookmaker="pinnacle",
                        market="totals",
                        line=Decimal("2.5"),
                        outcome="Over",
                        price=Decimal("1.950"),
                        tier=tier,
                        planned_offset_min=1440 if tier == "T-24" else 180,
                        actual_offset_min=1440 if tier == "T-24" else 180,
                        source="toa",
                        captured_at_utc=dt.datetime.now(dt.UTC),
                    )
                    session.add(snap)
            session.commit()
        return {"rows": 1, "credits": len(mkts)}


def _setup_test_db(period: str | None = None) -> tuple[sessionmaker[Session], AppConfig]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)

    cfg = load_config()
    # Ensure quota counter starts at 0
    with sm() as s:
        s.add(
            QuotaCounter(
                provider="toa",
                period=period or dt.datetime.now(dt.UTC).strftime("%Y-%m"),
                credits_used=0,
                updated_at_utc=dt.datetime.now(dt.UTC),
            )
        )
        s.commit()
    return sm, cfg


def test_dedup_slots_and_repeated_runs() -> None:
    sm, cfg = _setup_test_db()

    with sm() as s:
        teams = [Team(name=f"Team {i}", league="la_liga") for i in range(8)]
        s.add_all(teams)
        s.flush()

        # Match slots: 13:30, 16:30, 20:00, 20:45 on 2026-10-25
        match_times = [
            dt.datetime(2026, 10, 25, 13, 30, tzinfo=dt.UTC),
            dt.datetime(2026, 10, 25, 16, 30, tzinfo=dt.UTC),
            dt.datetime(2026, 10, 25, 20, 0, tzinfo=dt.UTC),
            dt.datetime(2026, 10, 25, 20, 45, tzinfo=dt.UTC),
        ]
        for i, ko in enumerate(match_times):
            s.add(
                Match(
                    league="la_liga",
                    season="2026-2027",
                    home_team_id=teams[i * 2].id,
                    away_team_id=teams[i * 2 + 1].id,
                    date_utc=ko.date(),
                    commence_time_utc=ko,
                    kickoff_source="toa",
                    status="scheduled",
                    toa_event_id=f"ev_{i}",
                )
            )
        s.commit()

    fake_client = FakeTOAClient(sm)

    # 1. T-24 poll for 13:30 slot (at 2026-10-24 13:30)
    now_t24_first = dt.datetime(2026, 10, 24, 13, 30, tzinfo=dt.UTC)
    res = job_poll_odds_snapshots(
        sm,
        client=fake_client,  # type: ignore[arg-type]
        config=cfg,
        now=now_t24_first,
    )
    assert res["status"] == "success"
    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["tier"] == "T-24"

    # 2. Repeated run 15 minutes later in the same window -> 0 new calls (deduplicated by DB)
    res_repeat = job_poll_odds_snapshots(
        sm,
        client=fake_client,  # type: ignore[arg-type]
        config=cfg,
        now=now_t24_first + dt.timedelta(minutes=15),
    )
    assert res_repeat["status"] == "success"
    assert len(fake_client.calls) == 1  # No additional call

    # 3. Poll remaining 3 slots at their respective T-24 times
    for ko in match_times[1:]:
        t24_time = ko - dt.timedelta(hours=24)
        job_poll_odds_snapshots(
            sm,
            client=fake_client,  # type: ignore[arg-type]
            config=cfg,
            now=t24_time,
        )

    # Total 4 slots * 1 call = 4 calls for T-24
    assert len(fake_client.calls) == 4

    # 4. T-3 polling for all 4 slots
    for ko in match_times:
        t3_time = ko - dt.timedelta(hours=3)
        job_poll_odds_snapshots(
            sm,
            client=fake_client,  # type: ignore[arg-type]
            config=cfg,
            now=t3_time,
        )

    # Total 4 (T-24) + 4 (T-3) = 8 calls
    assert len(fake_client.calls) == 8


def test_hard_stop_refuses_calls() -> None:
    sm, cfg = _setup_test_db()

    with sm() as s:
        row = s.scalars(select(QuotaCounter)).one()
        row.credits_used = cfg.odds_policy.hard_stop_credits
        s.commit()

    fake_client = FakeTOAClient(sm)
    res = job_poll_odds_snapshots(
        sm,
        client=fake_client,  # type: ignore[arg-type]
        config=cfg,
    )
    assert res["status"] == "skipped"
    assert res["reason"] == "hard_stop"
    assert len(fake_client.calls) == 0


def test_no_api_key_skips_task(monkeypatch) -> None:
    sm, cfg = _setup_test_db()
    monkeypatch.setattr(
        "core.scheduler.tasks.Secrets",
        lambda: type("MockSecrets", (), {"odds_api_key": ""})(),
    )

    # No client passed, no key in env -> skipped
    res = job_poll_odds_snapshots(sm, client=None, config=cfg)
    assert res["status"] == "skipped"
    assert res["reason"] == "no_api_key"


def test_t3_day_degradation_collapses_to_one_daily_call() -> None:
    sm, cfg = _setup_test_db(period="2026-10")

    # Set credits used to 442 (triggers t3_slot_to_day degradation >= 440)
    with sm() as s:
        row = s.scalars(select(QuotaCounter)).one()
        row.credits_used = 442
        s.commit()

        teams = [Team(name=f"Deg Team {i}", league="la_liga") for i in range(6)]
        s.add_all(teams)
        s.flush()

        match_times = [
            dt.datetime(2026, 10, 25, 13, 30, tzinfo=dt.UTC),
            dt.datetime(2026, 10, 25, 16, 30, tzinfo=dt.UTC),
            dt.datetime(2026, 10, 25, 20, 0, tzinfo=dt.UTC),
        ]
        for i, ko in enumerate(match_times):
            s.add(
                Match(
                    league="la_liga",
                    season="2026-2027",
                    home_team_id=teams[i * 2].id,
                    away_team_id=teams[i * 2 + 1].id,
                    date_utc=ko.date(),
                    commence_time_utc=ko,
                    kickoff_source="toa",
                    status="scheduled",
                    toa_event_id=f"ev_deg_{i}",
                )
            )
        s.commit()

    fake_client = FakeTOAClient(sm)

    # First T-3 slot runs at 10:30 (for 13:30 match)
    now_first = dt.datetime(2026, 10, 25, 10, 30, tzinfo=dt.UTC)
    res1 = job_poll_odds_snapshots(
        sm,
        client=fake_client,  # type: ignore[arg-type]
        config=cfg,
        now=now_first,
    )
    assert res1["status"] == "success"
    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["tier"] == "T-3"

    # Second T-3 slot at 13:30 (for 16:30 match) -> skip because T-3 call already occurred today
    now_second = dt.datetime(2026, 10, 25, 13, 30, tzinfo=dt.UTC)
    res2 = job_poll_odds_snapshots(
        sm,
        client=fake_client,  # type: ignore[arg-type]
        config=cfg,
        now=now_second,
    )
    assert res2["status"] == "success"
    assert len(fake_client.calls) == 1  # Still 1 call!

    # Third T-3 slot at 17:00 (for 20:00 match) -> should skip as well
    now_third = dt.datetime(2026, 10, 25, 17, 0, tzinfo=dt.UTC)
    res3 = job_poll_odds_snapshots(
        sm,
        client=fake_client,  # type: ignore[arg-type]
        config=cfg,
        now=now_third,
    )
    assert res3["status"] == "success"
    assert len(fake_client.calls) == 1  # Exactly 1 call per day!


@pytest.fixture
def real_poll_db(tmp_path: Path):
    url = f"sqlite:///{tmp_path / 'scheduler.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    try:
        yield url, sessionmaker(bind=engine), load_config()
    finally:
        engine.dispose()


@pytest.fixture
def toa_events() -> dict[str, list[dict[str, Any]]]:
    return json.loads((Path(__file__).parent / "fixtures" / "toa_events.json").read_text())


def _add_match(
    sm: sessionmaker[Session],
    event: dict[str, Any],
    ko: dt.datetime,
    *,
    league: str = "premier_league",
    linked: bool = True,
) -> int:
    with sm.begin() as session:
        home = Team(name=event["home_team"], league=league)
        away = Team(name=event["away_team"], league=league)
        session.add_all([home, away])
        session.flush()
        match = Match(
            league=league, season="2026-2027", home_team_id=home.id, away_team_id=away.id,
            date_utc=ko.date(), commence_time_utc=ko, status="scheduled",
            kickoff_source="toa" if linked else "understat_placeholder",
            toa_event_id=event["id"] if linked else None,
        )
        session.add(match)
        session.flush()
        return match.id


def _empty_odds(events: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(
        200, json=[dict(event, bookmakers=[]) for event in events],
        headers={"x-requests-last": "2", "x-requests-remaining": "490"},
    )


def test_real_client_skips_no_event_id_and_does_not_repeat_empty_slot(real_poll_db, toa_events):
    url, sm, cfg = real_poll_db
    linked, unlinked = toa_events["soccer_epl"][1:3]
    ko = dt.datetime.fromisoformat(linked["commence_time"])
    now = ko - dt.timedelta(hours=3)
    _add_match(sm, unlinked, ko, linked=False)
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _empty_odds([linked])

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = OddsApiClient(url, cfg, "k", client=http)
        no_match = job_poll_odds_snapshots(sm, client=client, config=cfg, now=now)
        assert no_match["status"] == "success"
        assert not requests
        with sm() as session:
            assert session.scalar(select(func.count(OddsPollAttempt.id))) == 0

        match_id = _add_match(sm, linked, ko)
        first = job_poll_odds_snapshots(sm, client=client, config=cfg, now=now)
        second = job_poll_odds_snapshots(
            sm, client=client, config=cfg, now=now + dt.timedelta(minutes=15),
        )
    assert first["status"] == second["status"] == "success"
    assert len(requests) == 1
    assert requests[0].url.path == "/v4/sports/soccer_epl/odds"
    with sm() as session:
        attempt = session.scalars(select(OddsPollAttempt)).one()
        assert (attempt.match_id, attempt.status, attempt.tier) == (match_id, "empty", "T-3")
        assert (attempt.slot, attempt.ts) == (ko, now)
        assert session.scalar(select(func.count(OddsSnapshot.id))) == 0


@pytest.mark.parametrize("recover", [False, True])
def test_real_client_retries_failed_slot_on_one_later_tick(
    real_poll_db, toa_events, monkeypatch, recover,
):
    url, sm, cfg = real_poll_db
    events = toa_events["soccer_epl"][:2]
    now = dt.datetime(2026, 10, 24, 10, tzinfo=dt.UTC)
    for event in events:
        _add_match(sm, event, now + dt.timedelta(hours=3))
    monkeypatch.setattr("core.scheduler.tasks.time.sleep", lambda _: None)

    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/v4/sports/soccer_epl/odds"
        if recover and calls == 3:
            return _empty_odds(events)
        return httpx.Response(500, headers={"x-requests-last": "0"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = OddsApiClient(url, cfg, "k", client=http)
        job_poll_odds_snapshots(sm, client=client, config=cfg, now=now)
        assert calls == 2
        with sm() as session:
            assert session.scalars(select(OddsPollAttempt.status)).all() == ["error", "error"]
        job_poll_odds_snapshots(
            sm, client=client, config=cfg, now=now + dt.timedelta(minutes=15),
        )
        assert calls == (3 if recover else 4)
        job_poll_odds_snapshots(
            sm, client=client, config=cfg, now=now + dt.timedelta(minutes=30),
        )
        assert calls == (3 if recover else 4)
    with sm() as session:
        statuses = session.scalars(select(OddsPollAttempt.status).order_by(OddsPollAttempt.id))
        assert statuses.all() == ["error", "error"] + ["empty" if recover else "error"] * 2


def test_real_client_empty_t3_slot_closes_the_degraded_sport_day(real_poll_db, toa_events):
    url, sm, cfg = real_poll_db
    first, second = toa_events["soccer_epl"][1:3]
    first_ko = dt.datetime.fromisoformat(first["commence_time"])
    second_ko = dt.datetime.fromisoformat(second["commence_time"])
    _add_match(sm, first, first_ko)
    _add_match(sm, second, second_ko)
    with sm.begin() as session:
        session.add(QuotaCounter(
            provider="toa", period=first_ko.strftime("%Y-%m"), credits_used=442,
            updated_at_utc=first_ko,
        ))
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.params["markets"] == "totals"
        return httpx.Response(200, json=[dict(first, bookmakers=[])],
                              headers={"x-requests-last": "1"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = OddsApiClient(url, cfg, "k", client=http)
        for ko in (first_ko, second_ko):
            state = job_poll_odds_snapshots(
                sm, client=client, config=cfg, now=ko - dt.timedelta(hours=3),
            )
            assert state["degradations_applied"]["t3_slot_to_day"]
    assert calls == 1


def test_real_client_four_matches_twelve_ticks_use_at_most_twelve_credits(real_poll_db, toa_events):
    url, sm, cfg = real_poll_db
    now = dt.datetime(2026, 10, 24, 10, tzinfo=dt.UTC)
    epl = toa_events["soccer_epl"]
    ucl = toa_events["soccer_uefa_champs_league"][:1]
    for i, event in enumerate(epl + ucl):
        league = "premier_league" if event in epl else "ucl"
        _add_match(sm, event, now + dt.timedelta(hours=24, minutes=15 * i), league=league)
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _empty_odds(epl if "/soccer_epl/" in request.url.path else ucl)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = OddsApiClient(url, cfg, "k", client=http)
        for i in range(12):
            result = job_poll_odds_snapshots(
                sm, client=client, config=cfg, now=now + dt.timedelta(minutes=15 * i),
            )
            assert result["status"] == "success"

    assert len(requests) == 4
    with sm() as session:
        attempts = session.scalars(select(OddsPollAttempt.status)).all()
        assert attempts == ["empty"] * 4
        credits = session.scalar(select(func.sum(ApiQuotaUsage.credits)))
        assert credits == 8

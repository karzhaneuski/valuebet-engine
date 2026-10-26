"""Unit and integration tests for ValueBet Engine REST API."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api.db import get_db
from api.main import app
from core.orm import Base, ClosingOdds, Match, PrematchOdds, Team, UserBet

TEST_TOKEN = "secret-token-12345"
AUTH_HEADER = {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    """Create isolated in-memory SQLite database with schema and seed data."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)
    session = sm()

    # Seed teams
    arsenal = Team(id=1, name="Arsenal", league="epl")
    chelsea = Team(id=2, name="Chelsea", league="epl")
    city = Team(id=3, name="Manchester City", league="epl")
    utd = Team(id=4, name="Manchester United", league="epl")
    session.add_all([arsenal, chelsea, city, utd])
    session.flush()

    # Seed matches
    today = dt.date.today()
    now_utc = dt.datetime.now(dt.UTC)
    m1 = Match(
        id=101,
        league="epl",
        season="2026-2027",
        date_utc=today + dt.timedelta(days=1),
        home_team_id=1,
        away_team_id=2,
        home_team=arsenal,
        away_team=chelsea,
        status="scheduled",
        kickoff_source="toa",
        commence_time_utc=now_utc + dt.timedelta(days=1),
    )
    m2 = Match(
        id=102,
        league="epl",
        season="2026-2027",
        date_utc=today + dt.timedelta(days=2),
        home_team_id=3,
        away_team_id=4,
        home_team=city,
        away_team=utd,
        status="scheduled",
        kickoff_source="toa",
        commence_time_utc=now_utc + dt.timedelta(days=2),
    )
    session.add_all([m1, m2])
    session.flush()

    # Seed prematch and closing odds for m1 so fair matrix can be built
    c1 = ClosingOdds(
        match_id=101,
        bookmaker="PC",
        over_price=Decimal("1.95"),
        under_price=Decimal("1.95"),
        home_price=Decimal("2.10"),
        draw_price=Decimal("3.40"),
        away_price=Decimal("3.60"),
    )
    p1 = PrematchOdds(
        match_id=101,
        bookmaker="P",
        over_price=Decimal("1.95"),
        under_price=Decimal("1.95"),
        home_price=Decimal("2.10"),
        draw_price=Decimal("3.40"),
        away_price=Decimal("3.60"),
    )
    session.add_all([c1, p1])
    session.commit()
    session.close()

    yield sm


@pytest.fixture
def client(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """TestClient with database override and API token env."""
    monkeypatch.setenv("API_SECRET_TOKEN", TEST_TOKEN)

    def _override_db() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# --- Authentication Tests ----------------------------------------------------


def test_health_is_public(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["version"] == "0.3.0"
    assert data["database_connected"] is True
    assert data["database_url_redacted"] is None
    assert "pacing_stage" in data


def test_health_degraded_on_db_failure() -> None:
    def _failing_db() -> Iterator[Session]:
        from unittest.mock import MagicMock
        m_session = MagicMock()
        m_session.execute.side_effect = RuntimeError("DB down")
        yield m_session

    app.dependency_overrides[get_db] = _failing_db
    try:
        with TestClient(app) as tc:
            resp = tc.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "degraded"
            assert data["database_connected"] is False
    finally:
        app.dependency_overrides.clear()


def test_auth_missing_token(client: TestClient) -> None:
    resp = client.get("/matches")
    assert resp.status_code == 401
    assert "Bearer" in resp.json()["detail"]


def test_auth_wrong_token(client: TestClient) -> None:
    resp = client.get("/matches", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401
    assert "Неверный токен" in resp.json()["detail"]


def test_auth_valid_token(client: TestClient) -> None:
    resp = client.get("/matches", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert "matches" in data
    assert data["total"] >= 2


# --- Matches & Fair Prices Tests ---------------------------------------------


def test_list_matches_with_filter(client: TestClient) -> None:
    resp = client.get("/matches?league=epl&days=7", headers=AUTH_HEADER)
    assert resp.status_code == 200
    matches = resp.json()["matches"]
    assert len(matches) == 2
    # Match 101 has closing odds
    assert matches[0]["has_market_lines"] is True
    assert "1X2" in matches[0]["available_markets"]


def test_get_fair_prices_success(client: TestClient) -> None:
    resp = client.get("/matches/101/fair-prices", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()

    assert data["match_id"] == 101
    assert data["home_team"] == "Arsenal"
    assert data["away_team"] == "Chelsea"

    # Exact scores
    scores = {item["score"]: item for item in data["exact_score"]}
    assert "1:0" in scores
    assert scores["1:0"]["fair_odds"] > 1.0

    # Inny (>4 goals)
    assert "inny" in data["inny"]["score"]
    assert data["inny"]["prob"] > 0.0

    # Totals
    totals_map = {t["line"]: t for t in data["totals"]}
    assert 2.5 in totals_map
    assert totals_map[2.5]["over_fair_odds"] > 1.0
    assert totals_map[2.5]["push_prob"] == 0.0

    # Whole totals (e.g. 2.0 with push)
    assert 2.0 in totals_map
    assert totals_map[2.0]["push_prob"] > 0.0

    # BTTS & 1X2
    assert data["btts"]["yes_fair_odds"] > 1.0
    assert data["h2h"]["home_fair_odds"] > 1.0

    # Methodological caveats
    caveats = " ".join(data["methodological_caveats"])
    assert "ρ = 0" in caveats
    assert "κ_high = 0.20" in caveats


def test_get_fair_prices_not_found(client: TestClient) -> None:
    resp = client.get("/matches/999/fair-prices", headers=AUTH_HEADER)
    assert resp.status_code == 404


def test_get_fair_prices_no_market_data(client: TestClient) -> None:
    # Match 102 has no odds
    resp = client.get("/matches/102/fair-prices", headers=AUTH_HEADER)
    assert resp.status_code == 404
    assert "пока не загружены" in resp.json()["detail"]


# --- Disambiguation Tests ----------------------------------------------------


def test_search_matches_single(client: TestClient) -> None:
    resp = client.get("/matches?search=Arsenal - Chelsea", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    assert resp.json()["matches"][0]["id"] == 101


def test_search_matches_ambiguous(client: TestClient) -> None:
    # Searching "Manchester" matches City and United
    resp = client.get("/matches?search=Manchester", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


# --- Fortuna Evaluation Tests ------------------------------------------------


def test_fortuna_eval_success(client: TestClient) -> None:
    raw_fortuna = """
    1:0 8.50
    2:0 11.00
    2:1 9.20
    1:1 6.80
    inny 15.00
    +2.5 1.90
    -2.5 1.90
    1 2.05
    X 3.35
    2 3.55
    """
    payload = {
        "match_id": 101,
        "raw_text": raw_fortuna,
    }
    resp = client.post("/fortuna-eval", json=payload, headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()

    assert data["match_id"] == 101
    assert "Arsenal - Chelsea" in data["match_name"]
    assert len(data["evaluations"]) >= 5
    assert "totals_2.5" in data["section_margins"]
    assert data["section_margins"]["totals_2.5"] > 0.0

    # Verify no recommendations and disclaimer present
    for ev in data["evaluations"]:
        assert "рекомендация" not in ev["status_note"].lower()
    assert "не даёт гарантий доходности" in data["disclaimer"]


def test_fortuna_eval_ambiguous_query_error(client: TestClient) -> None:
    # Query with non-matching teams
    payload = {
        "query": "Nonexistent FC vs Unknown Team",
        "raw_text": "1:0 8.50",
    }
    resp = client.post("/fortuna-eval", json=payload, headers=AUTH_HEADER)
    assert resp.status_code == 404


# --- User Bet Tracking Tests -------------------------------------------------


def test_record_user_bet_and_list(client: TestClient) -> None:
    payload = {
        "match_id": 101,
        "market": "exact_score",
        "outcome": "2:1",
        "odds": 9.50,
        "stake": 10.0,
        "bookmaker": "Fortuna",
    }
    create_resp = client.post("/user-bets", json=payload, headers=AUTH_HEADER)
    assert create_resp.status_code == 201
    bet = create_resp.json()
    assert bet["status"] == "open"
    assert bet["outcome"] == "2:1"
    assert bet["odds"] == 9.50
    assert bet["stake"] == 10.0

    # List bets
    list_resp = client.get("/user-bets", headers=AUTH_HEADER)
    assert list_resp.status_code == 200
    items = list_resp.json()["bets"]
    assert len(items) == 1
    assert items[0]["id"] == bet["id"]


def test_user_bets_returns_clv_pct(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    now_utc = dt.datetime.now(dt.UTC)
    with session_factory() as s:
        b = UserBet(
            match_id=101,
            market="exact_score",
            selection_key="2:1",
            odds=Decimal("8.00"),
            stake_units=Decimal("1.00"),
            status="won",
            pnl_units=Decimal("7.00"),
            clv_fair=Decimal("0.0525"),
            clv_source="fair_market_matrix",
            placed_at_utc=now_utc - dt.timedelta(hours=2),
            settled_at_utc=now_utc,
            created_at_utc=now_utc,
        )
        s.add(b)
        s.commit()

    resp = client.get("/user-bets", headers=AUTH_HEADER)
    assert resp.status_code == 200
    bets = resp.json()["bets"]
    found = next(x for x in bets if x.get("clv_source") == "fair_market_matrix")
    assert found["clv"] == 0.0525
    assert found["clv_pct"] == 5.25


# --- Performance Stats Tests -------------------------------------------------


def test_get_performance(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    # Add a settled bet
    now_utc = dt.datetime.now(dt.UTC)
    settled_bet = UserBet(
        match_id=101,
        market="exact_score",
        selection_key="2:1",
        odds=Decimal("8.00"),
        stake_units=Decimal("1.00"),
        bookmaker="fortuna",
        status="won",
        payout_units=Decimal("8.00"),
        pnl_units=Decimal("7.00"),
        clv_fair=Decimal("0.05"),
        placed_at_utc=now_utc,
        created_at_utc=now_utc,
        settled_at_utc=now_utc,
    )
    session = session_factory()
    session.add(settled_bet)
    session.commit()
    session.close()

    resp = client.get("/performance", headers=AUTH_HEADER)
    assert resp.status_code == 200
    perf = resp.json()
    assert perf["settled_bets"] == 1
    assert perf["total_profit"] == 7.0
    assert perf["roi"] == 700.0
    # Mandatory sample size warning for N < 100
    assert perf["sample_size_warning"] is not None
    assert "статистически недостаточна" in perf["sample_size_warning"]
    assert "не даёт гарантий" in perf["disclaimer"]

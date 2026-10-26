"""Unit tests for FastMCP server tools (SPEC v3 §9)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from core.orm import Base, ClosingOdds, Match, PrematchOdds, Team
from mcp_server.server import (
    evaluate_fortuna_odds,
    get_match_fair_prices,
    get_news_brief,
    get_upcoming_matches,
    get_user_performance,
    record_user_bet,
)


@pytest.fixture
def mcp_test_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    """Create in-memory SQLite database and monkeypatch load_config to use it."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)
    session = sm()

    # Monkeypatch session_scope in mcp_server.server to use our engine
    from contextlib import contextmanager

    @contextmanager
    def _override_scope(url: str) -> Iterator[Session]:
        sess = sm()
        try:
            yield sess
            sess.commit()
        except Exception:
            sess.rollback()
            raise
        finally:
            sess.close()

    monkeypatch.setattr("mcp_server.server.session_scope", _override_scope)

    # Seed teams
    arsenal = Team(id=1, name="Arsenal", league="epl")
    chelsea = Team(id=2, name="Chelsea", league="epl")
    city = Team(id=3, name="Manchester City", league="epl")
    session.add_all([arsenal, chelsea, city])
    session.flush()

    # Seed matches
    today = dt.date.today()
    now_utc = dt.datetime.now(dt.UTC)
    m1 = Match(
        id=201,
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
        id=202,
        league="epl",
        season="2026-2027",
        date_utc=today + dt.timedelta(days=2),
        home_team_id=3,
        away_team_id=1,
        home_team=city,
        away_team=arsenal,
        status="scheduled",
        kickoff_source="toa",
        commence_time_utc=now_utc + dt.timedelta(days=2),
    )
    session.add_all([m1, m2])
    session.flush()

    # Seed closing and prematch odds for m1
    c1 = ClosingOdds(
        match_id=201,
        bookmaker="PC",
        over_price=Decimal("1.95"),
        under_price=Decimal("1.95"),
        home_price=Decimal("2.10"),
        draw_price=Decimal("3.40"),
        away_price=Decimal("3.60"),
    )
    p1 = PrematchOdds(
        match_id=201,
        bookmaker="P",
        over_price=Decimal("1.95"),
        under_price=Decimal("1.95"),
        home_price=Decimal("2.10"),
        draw_price=Decimal("3.40"),
        away_price=Decimal("3.60"),
    )
    session.add_all([c1, p1])
    session.commit()

    yield session
    session.close()


def test_get_upcoming_matches(mcp_test_db: Session) -> None:
    res = get_upcoming_matches(days=3)
    assert "Ближайшие матчи" in res
    assert "Arsenal — Chelsea" in res
    assert "ID 201" in res


def test_get_match_fair_prices_success(mcp_test_db: Session) -> None:
    res = get_match_fair_prices(201)
    assert "Справедливые котировки: Arsenal — Chelsea" in res
    assert "1X2:" in res
    assert "Тотал 2.5:" in res
    assert "Обе забьют:" in res
    assert "inny" in res
    assert "ρ=0" in res


def test_get_match_fair_prices_not_found(mcp_test_db: Session) -> None:
    res = get_match_fair_prices(999)
    assert "не найден" in res


def test_evaluate_fortuna_odds_success(mcp_test_db: Session) -> None:
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
    res = evaluate_fortuna_odds(raw_text=raw_fortuna, match_id=201)
    assert "Анализ котировок Fortuna: Arsenal — Chelsea" in res
    assert "Маржа букмекера по секциям" in res
    assert "Без рекомендаций" in res


def test_evaluate_fortuna_odds_ambiguous_query(mcp_test_db: Session) -> None:
    # "Arsenal" plays in two matches (m1 and m2)
    res = evaluate_fortuna_odds(raw_text="1:0 8.50", team_query="Arsenal")
    assert "Неоднозначный запрос" in res
    assert "Укажите точный match_id" in res


def test_record_user_bet(mcp_test_db: Session) -> None:
    res = record_user_bet(
        match_id=201,
        market="exact_score",
        outcome="1:1",
        odds=6.80,
        stake=10.0,
        bookmaker="Fortuna",
    )
    assert "Ставка успешно записана:" in res
    assert "Arsenal — Chelsea" in res
    assert "ID 201" in res
    assert "status: open" in res.lower() or "open" in res


def test_record_user_bet_rejects_past_kickoff(mcp_test_db: Session) -> None:
    # Set match commence_time in the past
    match = mcp_test_db.get(Match, 201)
    assert match is not None
    orig_time = match.commence_time_utc
    match.commence_time_utc = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    mcp_test_db.commit()

    try:
        res = record_user_bet(
            match_id=201,
            market="totals",
            outcome="over 2.5",
            odds=1.90,
            stake=5.0,
        )
        assert "Ошибка: матч уже начался" in res
    finally:
        match.commence_time_utc = orig_time
        mcp_test_db.commit()


def test_record_user_bet_rejects_invalid_market(mcp_test_db: Session) -> None:
    res = record_user_bet(
        match_id=201,
        market="corners",
        outcome="over 10.5",
        odds=2.00,
        stake=1.0,
    )
    assert "Неподдерживаемый рынок 'corners'" in res


def test_get_user_performance(mcp_test_db: Session) -> None:
    res = get_user_performance(period="all")
    assert "Статистика" in res
    assert "P/L:" in res
    assert "ROI:" in res
    assert "Дисклеймер:" in res


def test_get_news_brief(mcp_test_db: Session) -> None:
    res = get_news_brief(home_team="Arsenal", away_team="Chelsea")
    assert "Сводка" in res
    assert "Arsenal" in res
    assert "Chelsea" in res
    assert "0.20" in res

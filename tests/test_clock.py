"""Tests for clock abstraction, clock dependencies, and direct clock read guards."""

from __future__ import annotations

import ast
import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api.db import get_clock, get_db
from api.main import app as rest_app
from core.clock import system_clock, today_date
from core.config import load_config
from core.ingest.odds_api import OddsApiClient
from core.orm import Base, Match, OddsSnapshot, Team
from core.scheduler.tasks import job_poll_events
from mcp_server.server import (
    get_upcoming_matches,
)
from mcp_server.server import (
    set_clock as mcp_set_clock,
)

AUTH_HEADER = {"Authorization": "Bearer test-token"}


def test_system_clock_and_today() -> None:
    now = system_clock()
    assert now.tzinfo == dt.UTC
    assert today_date() == now.date()

    fixed_time = dt.datetime(2026, 5, 10, 14, 30, tzinfo=dt.UTC)

    def fixed_clock() -> dt.datetime:
        return fixed_time

    assert today_date(fixed_clock) == dt.date(2026, 5, 10)


def test_odds_api_client_fixed_clock_stamps_snapshots(tmp_path: Path) -> None:
    db_file = tmp_path / "test_clock_odds.db"
    db_url = f"sqlite:///{db_file}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    fixed_time = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.UTC)
    cfg = load_config()

    def handler(request: httpx.Request) -> httpx.Response:
        odds_data = [
            {
                "id": "ev_1",
                "sport_key": "soccer_epl",
                "bookmakers": [
                    {
                        "key": "pinnacle",
                        "markets": [
                            {
                                "key": "totals",
                                "outcomes": [
                                    {"name": "Over", "price": 1.95, "point": 2.5},
                                    {"name": "Under", "price": 1.95, "point": 2.5},
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
        return httpx.Response(
            200,
            json=odds_data,
            headers={"x-requests-remaining": "100", "x-requests-last": "1"},
        )

    mock_transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=mock_transport)
    client = OddsApiClient(
        db_url,
        cfg,
        "test_key",
        client=http_client,
        clock=lambda: fixed_time,
    )

    res = client.snapshot_sport_odds("soccer_epl", "T-24", markets=["totals"])
    assert res["rows"] == 2

    sm = sessionmaker(bind=engine)
    with sm() as session:
        snaps = list(session.scalars(select(OddsSnapshot)).all())
        assert len(snaps) == 2
        for snap in snaps:
            assert snap.captured_at_utc == fixed_time


def test_api_route_clock_dependency_returns_data_for_overridden_date(monkeypatch) -> None:
    monkeypatch.setenv("API_SECRET_TOKEN", "test-token")
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)

    target_date = dt.date(2026, 11, 20)
    fixed_now = dt.datetime(2026, 11, 20, 10, 0, tzinfo=dt.UTC)

    with sm() as session:
        t1 = Team(id=1, name="HomeTeam", league="epl")
        t2 = Team(id=2, name="AwayTeam", league="epl")
        session.add_all([t1, t2])
        m = Match(
            id=101,
            league="epl",
            season=2026,
            date_utc=target_date,
            commence_time_utc=fixed_now + dt.timedelta(hours=4),
            home_team_id=1,
            away_team_id=2,
            home_team=t1,
            away_team=t2,
            status="scheduled",
            kickoff_source="toa",
        )
        session.add(m)
        session.commit()

    def _override_db() -> Iterator[Session]:
        sess = sm()
        try:
            yield sess
            sess.commit()
        except Exception:
            sess.rollback()
            raise
        finally:
            sess.close()

    try:
        rest_app.dependency_overrides[get_db] = _override_db
        rest_app.dependency_overrides[get_clock] = lambda: (lambda: fixed_now)

        with TestClient(rest_app) as client:
            resp = client.get("/matches?status=scheduled&days=3", headers=AUTH_HEADER)
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 1
            assert data["matches"][0]["id"] == 101
            assert data["matches"][0]["date_utc"] == target_date.isoformat()
    finally:
        rest_app.dependency_overrides.clear()


def test_mcp_tool_uses_provided_clock(monkeypatch) -> None:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)

    target_date = dt.date(2026, 12, 1)
    fixed_now = dt.datetime(2026, 12, 1, 9, 0, tzinfo=dt.UTC)

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

    with sm() as session:
        t1 = Team(id=1, name="Alpha", league="epl")
        t2 = Team(id=2, name="Beta", league="epl")
        session.add_all([t1, t2])
        m = Match(
            id=301,
            league="epl",
            season=2026,
            date_utc=target_date,
            commence_time_utc=fixed_now + dt.timedelta(hours=5),
            home_team_id=1,
            away_team_id=2,
            home_team=t1,
            away_team=t2,
            status="scheduled",
            kickoff_source="toa",
        )
        session.add(m)
        session.commit()

    try:
        mcp_set_clock(lambda: fixed_now)
        res = get_upcoming_matches(days=2)
        assert "Alpha — Beta" in res
    finally:
        mcp_set_clock(None)


def test_job_odds_api_client_clock_injection(monkeypatch) -> None:
    """Calling a job without now constructs client with clock None (uses system_clock),

    and with now=X the client's clock returns X.
    """
    constructed_clocks: list[Any] = []
    real_init = OddsApiClient.__init__

    def spy_init(self: OddsApiClient, *args: Any, **kwargs: Any) -> None:
        constructed_clocks.append(kwargs.get("clock"))
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(OddsApiClient, "__init__", spy_init)
    monkeypatch.setattr(
        "core.scheduler.tasks.Secrets",
        lambda: type("MockSecrets", (), {"odds_api_key": "test_key"})(),
    )
    monkeypatch.setattr(OddsApiClient, "fetch_events", lambda self, sport_key: [])

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)

    # 1. Calling job without `now` -> constructed with clock None (client._clock is system_clock)
    constructed_clocks.clear()
    job_poll_events(sm)
    assert len(constructed_clocks) == 1
    assert constructed_clocks[0] is None

    # 2. Calling job with `now=X` -> client's clock returns X
    constructed_clocks.clear()
    custom_now = dt.datetime(2026, 10, 20, 15, 30, tzinfo=dt.UTC)
    job_poll_events(sm, now=custom_now)
    assert len(constructed_clocks) == 1
    assert constructed_clocks[0] is not None
    assert constructed_clocks[0]() == custom_now


def test_guard_no_direct_clock_reads() -> None:
    """Scan core/ingest/odds_api.py, api/routes/*.py and mcp_server/server.py with ast

    and fail if they call datetime.now, datetime.utcnow or date.today directly.
    """
    root = Path(__file__).resolve().parent.parent

    files_to_check: list[Path] = [
        root / "core" / "ingest" / "odds_api.py",
        root / "mcp_server" / "server.py",
    ]
    files_to_check.extend((root / "api" / "routes").glob("*.py"))

    forbidden_calls = {"now", "utcnow", "today"}
    violations: list[str] = []

    for file_path in files_to_check:
        if not file_path.exists() or file_path.name == "__init__.py":
            continue
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                # e.g. dt.datetime.now(), datetime.now(), dt.date.today(), date.today()
                if isinstance(func, ast.Attribute) and func.attr in forbidden_calls:
                    val = func.value
                    val_name = ""
                    if isinstance(val, ast.Name):
                        val_name = val.id
                    elif isinstance(val, ast.Attribute):
                        val_name = val.attr
                    if val_name in {"datetime", "date"}:
                        rel = file_path.relative_to(root)
                        msg = f"{rel}:{node.lineno}: direct call {val_name}.{func.attr}()"
                        violations.append(msg)

    assert not violations, "Forbidden direct clock reads detected:\n" + "\n".join(violations)

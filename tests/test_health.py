"""Health-check endpoints for REST API, Scheduler, and FastMCP server."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api.db import get_db
from api.main import app as api_app
from core.orm import Base
from core.scheduler.main import app as scheduler_app
from mcp_server.server import mcp


def test_api_health_ok() -> None:
    """When database is reachable and tables exist, /health returns status='ok'."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)

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

    api_app.dependency_overrides[get_db] = _override_db
    try:
        client = TestClient(api_app)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["database_connected"] is True
        assert data["version"] == "0.3.0"
    finally:
        api_app.dependency_overrides.clear()


def test_api_health_degraded_when_tables_missing() -> None:
    """When database is reachable but key tables are missing, /health returns status='degraded'."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Note: Base.metadata.create_all is deliberately NOT called here.
    sm = sessionmaker(bind=engine, expire_on_commit=False)

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

    api_app.dependency_overrides[get_db] = _override_db
    try:
        client = TestClient(api_app)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["database_connected"] is True
    finally:
        api_app.dependency_overrides.clear()


def test_api_health_degraded_when_db_disconnected() -> None:
    """When database SELECT 1 query fails, /health returns degraded and not connected."""
    mock_session = MagicMock(spec=Session)
    mock_session.execute.side_effect = RuntimeError("Database unreachable")

    def _override_db() -> Iterator[Session]:
        yield mock_session

    api_app.dependency_overrides[get_db] = _override_db
    try:
        client = TestClient(api_app)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["database_connected"] is False
    finally:
        api_app.dependency_overrides.clear()


def test_scheduler_health() -> None:
    client = TestClient(scheduler_app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["service"] == "scheduler"


def test_mcp_health() -> None:
    assert mcp.name == "valuebet-engine"
    assert len(mcp._tool_manager.list_tools()) >= 5

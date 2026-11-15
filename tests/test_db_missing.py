"""Tests for DatabaseMissingError and SQLite database existence checks."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from api.main import app as api_app
from core.db import DatabaseMissingError, get_engine, reset_engine, session_scope
from core.orm import Base, Team
from scripts.build_fixture_db import main as build_fixture_db_main


def test_missing_database_raises_and_does_not_create(tmp_path: Path) -> None:
    """Missing SQLite file -> DatabaseMissingError and the file still does not exist."""
    db_file = tmp_path / "nonexistent.db"
    url = f"sqlite:///{db_file}"
    assert not db_file.exists()

    with pytest.raises(DatabaseMissingError) as exc_info:
        get_engine(url)

    err_msg = str(exc_info.value)
    assert str(db_file.resolve()) in err_msg
    assert "alembic upgrade head" in err_msg or "DATABASE_URL" in err_msg
    assert not db_file.exists()


def test_zero_byte_database_raises_and_stays_zero(tmp_path: Path) -> None:
    """Zero-byte SQLite file -> DatabaseMissingError and size stays 0."""
    db_file = tmp_path / "empty.db"
    db_file.touch()
    assert db_file.exists()
    assert db_file.stat().st_size == 0
    url = f"sqlite:///{db_file}"

    with pytest.raises(DatabaseMissingError) as exc_info:
        get_engine(url)

    err_msg = str(exc_info.value)
    assert str(db_file.resolve()) in err_msg
    assert db_file.exists()
    assert db_file.stat().st_size == 0


def test_opt_in_creation_works_with_create_flag(tmp_path: Path) -> None:
    """Opt-in creation via create=True creates the file and allows schema creation."""
    db_file = tmp_path / "opt_in_flag.db"
    url = f"sqlite:///{db_file}"
    reset_engine()

    engine = get_engine(url, create=True)
    Base.metadata.create_all(engine)
    assert db_file.exists()
    assert db_file.stat().st_size > 0

    # Once schema exists and file is non-empty, standard get_engine should open normally
    reset_engine()
    engine2 = get_engine(url)
    tables = inspect(engine2).get_table_names()
    assert "teams" in tables


def test_env_var_does_not_allow_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting VALUEBET_ALLOW_DB_CREATE=1 does NOT allow creation (bypass removed)."""
    db_file = tmp_path / "opt_in_env.db"
    url = f"sqlite:///{db_file}"
    reset_engine()
    monkeypatch.setenv("VALUEBET_ALLOW_DB_CREATE", "1")

    with pytest.raises(DatabaseMissingError):
        get_engine(url)

    assert not db_file.exists()


def test_in_memory_database_always_allowed() -> None:
    """sqlite:///:memory: works without opt-in and without DatabaseMissingError."""
    reset_engine()
    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    tables = inspect(engine).get_table_names()
    assert "teams" in tables


def test_existing_valid_database_opens_normally(tmp_path: Path) -> None:
    """An existing valid database opens normally without create opt-in."""
    db_file = tmp_path / "valid.db"
    url = f"sqlite:///{db_file}"
    reset_engine()

    # Pre-create valid schema
    engine_init = get_engine(url, create=True)
    Base.metadata.create_all(engine_init)
    with session_scope(url, create=True) as session:
        session.add(Team(name="Arsenal", league="epl"))
    reset_engine()

    # Now open with normal get_engine / session_scope without create=True
    get_engine(url)
    with session_scope(url) as session:
        team = session.scalar(select(Team).where(Team.name == "Arsenal"))
        assert team is not None
        assert team.name == "Arsenal"


def test_api_health_unhealthy_when_database_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API /health reports unhealthy status when DB file is missing without crashing."""
    db_file = tmp_path / "missing_health.db"
    url = f"sqlite:///{db_file}"
    reset_engine()
    monkeypatch.setenv("DATABASE_URL", url)

    # Ensure no dependency overrides
    api_app.dependency_overrides.clear()
    client = TestClient(api_app)
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "unhealthy"
    assert data["database_connected"] is False
    assert not db_file.exists()


def test_api_route_returns_503_when_database_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When DB file is missing, /matches returns 503, creates no file, and leaks no paths."""
    db_file = tmp_path / "missing_matches.db"
    url = f"sqlite:///{db_file}"
    reset_engine()
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("API_SECRET_TOKEN", "test-token")

    api_app.dependency_overrides.clear()
    client = TestClient(api_app, raise_server_exceptions=False)
    resp = client.get(
        "/matches",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 503
    data = resp.json()
    assert data["detail"] == "database unavailable"
    assert "reason" in data
    # Response contains no traceback and no filesystem path
    raw_text = resp.text
    assert "Traceback" not in raw_text
    assert str(tmp_path) not in raw_text
    assert str(db_file) not in raw_text
    assert not db_file.exists()


def test_build_fixture_db_creates_file_and_checks_on_missing_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build_fixture_db creates non-empty DB; checks on missing DB raises DatabaseMissingError."""
    # 1. Opening a missing path in python -m core.ingest checks style raises DatabaseMissingError
    missing_file = tmp_path / "missing_check.db"
    missing_url = f"sqlite:///{missing_file}"
    reset_engine()
    with pytest.raises(DatabaseMissingError), session_scope(missing_url):
        pass
    assert not missing_file.exists()

    # 2. build_fixture_db.main() creates a fresh non-empty db
    fixture_file = tmp_path / "fixture.db"
    fixture_url = f"sqlite:///{fixture_file}"
    reset_engine()
    monkeypatch.setenv("DATABASE_URL", fixture_url)
    monkeypatch.chdir(tmp_path)

    assert not fixture_file.exists()
    build_fixture_db_main()
    assert fixture_file.exists()
    assert fixture_file.stat().st_size > 0
    assert (tmp_path / "reports/ingest/events_report.json").exists()

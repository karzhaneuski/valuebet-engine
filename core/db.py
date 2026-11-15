"""Database engine and session management (SQLite via SQLAlchemy 2.0).

The URL comes from config.yaml (database.url) and defaults to a local SQLite
file; switching to Postgres later must not require code changes outside this
module and config.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


class DatabaseMissingError(RuntimeError):
    """Raised when a SQLite database file is missing or empty (0 bytes)."""
    pass


def _is_sqlite_url(url: str) -> bool:
    try:
        parsed = make_url(url)
        return parsed.get_backend_name() == "sqlite"
    except Exception:
        return url.startswith("sqlite:")


def _resolve_sqlite_path(url: str) -> Path | None:
    """Return resolved Path if sqlite url points to a file, or None if in-memory / empty."""
    try:
        parsed = make_url(url)
        if parsed.get_backend_name() != "sqlite":
            return None
        db = parsed.database
        if not db or db == ":memory:":
            return None
        return Path(db).resolve()
    except Exception:
        if not url.startswith("sqlite:///"):
            return None
        raw = url[len("sqlite:///"):]
        if not raw or raw == ":memory:":
            return None
        return Path(raw).resolve()


def _ensure_database_accessible(url: str, create: bool = False) -> None:
    """Check that SQLite database file exists and is non-empty unless create opt-in is allowed.

    Creation opt-in is permitted if `create=True`.
    """
    if create:
        return

    path = _resolve_sqlite_path(url)
    if path is None:
        return

    if not path.exists():
        raise DatabaseMissingError(
            f"SQLite database file not found at '{path}'. "
            "Run 'alembic upgrade head' to create and migrate it, or fix DATABASE_URL."
        )

    try:
        if path.stat().st_size == 0:
            raise DatabaseMissingError(
                f"SQLite database file at '{path}' has size 0 bytes. "
                "Run 'alembic upgrade head' to initialize it, or fix DATABASE_URL."
            )
    except FileNotFoundError as err:
        raise DatabaseMissingError(
            f"SQLite database file not found at '{path}'. "
            "Run 'alembic upgrade head' to create and migrate it, or fix DATABASE_URL."
        ) from err


@event.listens_for(Engine, "connect")
def _sqlite_fk_on(dbapi_connection: Any, connection_record: Any) -> None:
    """SQLite ships with foreign keys OFF; enforce them on every connect
    (application, tests and alembic all go through create_engine)."""
    import sqlite3

    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_engine(url: str, create: bool = False) -> Engine:
    """Create (once) and return the engine for ``url``.

    Parameters:
        url: Database URL string.
        create: If True, allow connecting to / creating a new or empty SQLite file.
    """
    global _engine, _session_factory
    _ensure_database_accessible(url, create=create)
    if _engine is None or _engine.url.render_as_string(hide_password=False) != url:
        _engine = create_engine(url, future=True)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False,
                                        future=True)
    return _engine


def get_session_factory(url: str, create: bool = False) -> sessionmaker[Session]:
    get_engine(url, create=create)
    assert _session_factory is not None
    return _session_factory


@contextmanager
def session_scope(url: str, create: bool = False) -> Iterator[Session]:
    """Transaction-scoped session: commit on success, rollback on error.

    Parameters:
        url: Database URL string.
        create: If True, allow connecting to / creating a new or empty SQLite file.
    """
    factory = get_session_factory(url, create=create)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Drop the cached engine (used by tests to switch databases)."""
    global _engine, _session_factory
    _engine = None
    _session_factory = None

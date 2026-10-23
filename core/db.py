"""Database engine and session management (SQLite via SQLAlchemy 2.0).

The URL comes from config.yaml (database.url) and defaults to a local SQLite
file; switching to Postgres later must not require code changes outside this
module and config.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine(url: str) -> Engine:
    """Create (once) and return the engine for ``url``."""
    global _engine, _session_factory
    if _engine is None or _engine.url.render_as_string(hide_password=False) != url:
        _engine = create_engine(url, future=True)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False,
                                        future=True)
    return _engine


def get_session_factory(url: str) -> sessionmaker[Session]:
    get_engine(url)
    assert _session_factory is not None
    return _session_factory


@contextmanager
def session_scope(url: str) -> Iterator[Session]:
    """Transaction-scoped session: commit on success, rollback on error."""
    factory = get_session_factory(url)
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

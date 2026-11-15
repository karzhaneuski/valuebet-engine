"""Database session dependency for FastAPI."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from core.clock import Clock, system_clock
from core.config import load_config
from core.db import get_session_factory


def get_clock() -> Clock:
    """Provide the system clock dependency for FastAPI routes."""
    return system_clock


def get_db() -> Iterator[Session]:
    """Yield a transactional database session for each request, or raise DatabaseMissingError."""
    cfg = load_config()
    factory = get_session_factory(cfg.database.url)

    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

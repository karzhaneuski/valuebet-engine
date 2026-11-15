"""Database session dependency for FastAPI."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from core.config import load_config
from core.db import DatabaseMissingError, get_session_factory


def get_db() -> Iterator[Session | None]:
    """Yield a transactional database session for each request, or None if DB is missing."""
    cfg = load_config()
    try:
        factory = get_session_factory(cfg.database.url)
    except DatabaseMissingError:
        yield None
        return

    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

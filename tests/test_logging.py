"""structlog configuration sanity."""

from __future__ import annotations

from core.logging import configure, get_logger


def test_configure_and_log() -> None:
    configure(dev_mode=True)
    logger = get_logger("test")
    assert callable(logger.info)
    # must not raise
    logger.info("log.line.test", extra_field=1)

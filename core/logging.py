"""structlog configuration shared by all services.

JSON logs in production (the scheduler/api run headless), pretty console
logs when a TTY is attached. All services call :func:`configure` at startup.
"""

from __future__ import annotations

import logging
import sys

import structlog

from core.http import RedactingFilter, redact_processor


def configure(dev_mode: bool = False) -> None:
    """Set up structlog once per process; idempotent."""
    import os

    os.environ.setdefault("SOCCERDATA_LOGLEVEL", "WARNING")
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO)
    # httpx logs full request URLs at INFO; quiet third-party library chatter
    for noisy in ("httpx", "httpcore", "soccerdata", "TLSLibrary", "TLSRequests", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        import tls_requests.models.libraries as lib
        lib.logger.setLevel(logging.WARNING)
    except (ImportError, AttributeError):
        pass
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactingFilter())
    renderer = (
        structlog.dev.ConsoleRenderer()
        if dev_mode
        else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            redact_processor,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a configured logger; configures defaults on first use."""
    return structlog.get_logger(name)

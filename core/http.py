"""HTTP helpers: URL redaction, error wrapping, log redaction (review B2).

Rules enforced here:
- request URLs are NEVER logged (they carry apiKey query params);
- httpx errors are re-raised as HttpError with the endpoint path only,
  ``from None`` so the original exception (containing the URL) never
  reaches logs or reports;
- a structlog processor and a logging.Filter redact secret VALUES and
  ``apiKey=...``-style patterns from any message or exception text that
  still slips through.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, MutableMapping
from typing import Any

import httpx

# query parameter names that must never appear in logs
SENSITIVE_PARAMS = ("apiKey", "api_key", "key", "token", "apikey")

_SECRET_VALUE_PATTERNS = [
    re.compile(r"(apiKey|api_key|apikey|key|token)=[A-Za-z0-9_\-]{8,}",
               re.IGNORECASE),
    re.compile(r"AIza[A-Za-z0-9_\-]{20,}"),
    re.compile(r"tvly-[A-Za-z0-9\-_]{10,}"),
    re.compile(r"BSA[A-Za-z0-9_\-]{15,}"),
]

_redact_values: list[str] = []


def register_secret_values(values: list[str]) -> None:
    """Register runtime secret values (from .env) to redact from logs."""
    for v in values:
        if len(v) >= 8 and v not in _redact_values:
            _redact_values.append(v)


_PARAM_PATTERN = re.compile(
    r"(apiKey|api_key|apikey|key|token)=[A-Za-z0-9_\-]{8,}", re.IGNORECASE)


def redact_text(text: str) -> str:
    for value in _redact_values:
        text = text.replace(value, "***")
    text = _PARAM_PATTERN.sub(r"\1=***", text)
    for pattern in _SECRET_VALUE_PATTERNS[1:]:
        text = pattern.sub("***", text)
    return text


def redact_url(url: str) -> str:
    """Replace sensitive query parameter values with ***."""
    base, _, query = url.partition("?")
    if not query:
        return base
    kept = []
    for part in query.split("&"):
        name = part.partition("=")[0].lower()
        if name in {p.lower() for p in SENSITIVE_PARAMS}:
            kept.append(f"{part.partition('=')[0]}=***")
        else:
            kept.append(part)
    return f"{base}?{'&'.join(kept)}"


class HttpError(RuntimeError):
    """HTTP failure without the original URL/exception chain."""

    def __init__(self, status: int | None, endpoint: str,
                 message: str = "") -> None:
        self.status = status
        self.endpoint = endpoint
        super().__init__(
            f"http error: status={status} endpoint={endpoint} {message}"
            .strip())


def endpoint_of(url: str) -> str:
    """Path without scheme/host/query, for safe error reporting."""
    from urllib.parse import urlsplit

    return urlsplit(url).path


def safe_request(client: httpx.Client, method: str, url: str,
                 **kwargs: Any) -> httpx.Response:
    """httpx request that never leaks the URL through the exception chain."""
    try:
        resp = client.request(method, url, **kwargs)
        resp.raise_for_status()
        return resp
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        headers = exc.response.headers
        raise HttpError(status, endpoint_of(url),
                        message=f"hdrs_last={headers.get('x-requests-last')} "
                                f"remaining={headers.get('x-requests-remaining')}"
                        ) from None
    except httpx.HTTPError as exc:
        raise HttpError(None, endpoint_of(url),
                        message=type(exc).__name__) from None


# --- log redaction -----------------------------------------------------------

class RedactingFilter(logging.Filter):
    """logging.Filter for the root handler: redacts records in place."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(str(record.msg))
        if record.args:
            record.args = tuple(redact_text(str(a)) for a in record.args)
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            if exc.args:
                exc.args = tuple(redact_text(str(a)) for a in exc.args)
        return True


def redact_processor(logger: object, method: str,
                     event_dict: MutableMapping[str, object]) -> Mapping[str, object]:
    """structlog processor: redact every string value in the event."""
    for key, value in list(event_dict.items()):
        if isinstance(value, str):
            event_dict[key] = redact_text(value)
        elif isinstance(value, BaseException):
            event_dict[key] = redact_text(str(value))
    return event_dict


def retry_call(fn, attempts: int = 3, backoff_s: float = 2.0):
    """Run fn() with exponential backoff between attempts (S2). Free
    endpoints and football-data only; paid calls retry through the quota
    reserve, one reservation per attempt."""
    import time

    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if attempt < attempts:
                time.sleep(backoff_s * attempt)
    assert last is not None
    raise last

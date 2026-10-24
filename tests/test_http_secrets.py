"""B2: secrets never leak into logs, exceptions or reports."""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

from core.http import (
    HttpError,
    RedactingFilter,
    redact_text,
    redact_url,
    register_secret_values,
    safe_request,
)

FAKE_KEY = "sk-fake-key-1234567890abcdef"
BASE = "https://api.example.test"


@pytest.fixture(autouse=True)
def _register_fake_key():
    register_secret_values([FAKE_KEY])
    yield


def test_redact_url_query_params() -> None:
    url = f"{BASE}/v4/sports/x/odds?apiKey={FAKE_KEY}&regions=eu"
    assert FAKE_KEY not in redact_url(url)
    assert "apiKey=***" in redact_url(url)
    assert "regions=eu" in redact_url(url)


@respx.mock
def test_401_error_has_no_key(caplog, capsys) -> None:
    respx.get(f"{BASE}/odds").mock(return_value=httpx.Response(
        401, json={"message": "bad key"}, headers={"x-requests-last": "0"}))
    with pytest.raises(HttpError) as exc_info:
        safe_request(httpx.Client(), "GET",
                     f"{BASE}/odds?apiKey={FAKE_KEY}&regions=eu")
    assert FAKE_KEY not in str(exc_info.value)
    assert exc_info.value.status == 401
    assert FAKE_KEY not in caplog.text
    assert FAKE_KEY not in capsys.readouterr().out
    assert FAKE_KEY not in capsys.readouterr().err


@respx.mock
def test_500_error_has_no_key() -> None:
    respx.get(f"{BASE}/odds").mock(return_value=httpx.Response(500))
    with pytest.raises(HttpError) as exc_info:
        safe_request(httpx.Client(), "GET", f"{BASE}/odds?apiKey={FAKE_KEY}")
    assert FAKE_KEY not in str(exc_info.value)
    assert exc_info.value.endpoint == "/odds"


@respx.mock
def test_connect_error_has_no_key() -> None:
    respx.get(f"{BASE}/odds").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(HttpError) as exc_info:
        safe_request(httpx.Client(), "GET", f"{BASE}/odds?apiKey={FAKE_KEY}")
    assert FAKE_KEY not in str(exc_info.value)
    assert exc_info.value.status is None


def test_redacting_filter_cleans_record() -> None:
    record = logging.LogRecord("t", logging.INFO, "p", 1,
                               f"url {BASE}?apiKey={FAKE_KEY}", None, None)
    RedactingFilter().filter(record)
    assert FAKE_KEY not in str(record.msg)


def test_redact_text_patterns() -> None:
    out = redact_text("apiKey=abcdefgh123 and tvly-abcdefgh12")
    assert "apiKey=***" in out
    assert "abcdefgh123" not in out
    assert "tvly-abcdefgh12" not in out

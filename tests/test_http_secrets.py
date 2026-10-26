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
    tvly = "tvly-" + "abcdefgh12"
    aiza = "AIzaSyD" + "fakegeminikey1234567890."
    bsa = "BSA" + "testsecrettoken12345678"
    out = redact_text(f"apiKey=abcdefgh123 and {tvly} and {aiza} and {bsa}")
    assert "apiKey=***" in out
    assert "abcdefgh123" not in out
    assert tvly not in out
    assert aiza not in out
    assert bsa not in out


def test_register_runtime_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    custom_secret = "custom_very_secret_token_value_999"
    monkeypatch.setenv("API_SECRET_TOKEN", custom_secret)
    from core.http import register_runtime_secrets
    register_runtime_secrets()
    log_line = f"Processing request with token: {custom_secret}"
    redacted = redact_text(log_line)
    assert custom_secret not in redacted
    assert "***" in redacted


@pytest.mark.parametrize(
    "raw_assignment,raw_val",
    [
        ("token = " + "test-" + "1234567890abcdef1234", "test-" + "1234567890abcdef1234"),
        ("token = " + "Sec." + "1234567890abcdef1234", "Sec." + "1234567890abcdef1234"),
        ("token = " + "Example-" + "1234567890abcdef1234", "Example-" + "1234567890abcdef1234"),
        ("ACCESS_TOKEN = " + "1234567890abcdef1234", "1234567890abcdef1234"),
        ("X_API_KEY = " + "1234567890abcdef1234", "1234567890abcdef1234"),
        ('"access_token": "' + "1234567890abcdef1234" + '"', "1234567890abcdef1234"),
        ("token = " + "Bearer_" + "1234567890abcdef1234", "Bearer_" + "1234567890abcdef1234"),
    ],
)
def test_fake_secret_table_caught_by_pattern_and_redacted(
    raw_assignment: str, raw_val: str
) -> None:
    from core.http import ASSIGNMENT_PATTERN, redact_text

    # 1. CI pattern catches it
    assert ASSIGNMENT_PATTERN.search(raw_assignment) is not None

    # 2. redact_text hides the secret value
    redacted = redact_text(raw_assignment)
    assert raw_val not in redacted
    assert "***" in redacted


def test_env_example_format_caught_by_ci_pattern() -> None:
    from core.http import ASSIGNMENT_PATTERN

    line = "ODDS_API_KEY=" + "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4" + "  # see .env.example"
    assert ASSIGNMENT_PATTERN.search(line) is not None


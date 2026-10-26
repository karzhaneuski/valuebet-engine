"""Tests for Streamlit analytical dashboard and REST API client.

SPEC v3 §9:
- Zero direct SQLite / SQLAlchemy access from dashboard modules.
- Complete navigation and rendering coverage across all 5 pages.
- Transparent audit of metrics, bootstrap CIs, and variance warnings.
"""

from __future__ import annotations

import pathlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from streamlit.testing.v1 import AppTest

from dashboard.api_client import ApiClient

# ============================================================================
# 1. Architectural Guardrail: Zero direct database access
# ============================================================================


def test_dashboard_zero_direct_sqlite_access() -> None:
    """Verify dashboard code never imports SQLite, SQLAlchemy, or internal ORM modules."""
    dashboard_dir = pathlib.Path(__file__).parent.parent / "dashboard"
    py_files = list(dashboard_dir.rglob("*.py"))
    assert len(py_files) >= 6, (
        f"Expected at least 6 python files in dashboard/, found {len(py_files)}"
    )

    forbidden_patterns = [
        "sqlite3",
        "sqlalchemy",
        "core.orm",
        "core.db",
        "create_engine",
        "sessionmaker",
        "get_engine",
        "get_db",
    ]

    violations: list[str] = []
    for file_path in py_files:
        content = file_path.read_text(encoding="utf-8")
        for line_no, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            # Ignore comments
            if stripped.startswith("#"):
                continue
            for pattern in forbidden_patterns:
                if pattern in stripped:
                    violations.append(
                        f"{file_path.name}:{line_no} -> '{pattern}' found in: {stripped}"
                    )

    msg = "Forbidden direct database access detected in dashboard:\n" + "\n".join(violations)
    assert not violations, msg


# ============================================================================
# 2. ApiClient Unit Tests
# ============================================================================


def test_api_client_headers() -> None:
    client_no_token = ApiClient(base_url="http://test:8000", token="")
    assert client_no_token.headers == {"Content-Type": "application/json"}

    client_with_token = ApiClient(base_url="http://test:8000", token="secret123")
    assert client_with_token.headers == {
        "Content-Type": "application/json",
        "Authorization": "Bearer secret123",
    }


def test_api_client_methods_mocked() -> None:
    client = ApiClient(base_url="http://test:8000", token="test-token")

    with patch("httpx.Client.get") as mock_get, patch("httpx.Client.post") as mock_post:
        # Mock get_health
        mock_get_resp = MagicMock()
        mock_get_resp.json.return_value = {"status": "ok", "version": "0.3.0"}
        mock_get.return_value = mock_get_resp

        health = client.get_health()
        assert health["status"] == "ok"
        mock_get.assert_called_with("http://test:8000/health")

        # Mock get_matches
        mock_get_resp.json.return_value = {"matches": [{"id": 1, "home_team": "Arsenal"}]}
        matches = client.get_matches(days=5, league="epl", search="Ars")
        assert len(matches["matches"]) == 1
        assert "http://test:8000/matches" in mock_get.call_args[0][0]

        # Mock get_fair_prices
        mock_get_resp.json.return_value = {"match_id": 1, "markets": {}}
        prices = client.get_fair_prices(match_id=1)
        assert prices["match_id"] == 1

        # Mock evaluate_fortuna
        mock_post_resp = MagicMock()
        mock_post_resp.json.return_value = {"match_id": 1, "evaluations": []}
        mock_post.return_value = mock_post_resp

        fortuna_eval = client.evaluate_fortuna("1:0 8.50", match_id=1)
        assert fortuna_eval["match_id"] == 1

        # Mock record_user_bet
        mock_post_resp.json.return_value = {"id": 12, "status": "open"}
        bet = client.record_user_bet(
            match_id=1, market="totals", outcome="over 2.5", odds=1.95, stake=1.0
        )
        assert bet["id"] == 12

        # Mock get_user_bets
        mock_get_resp.json.return_value = {"bets": [{"id": 12}]}
        user_bets = client.get_user_bets(status="open")
        assert len(user_bets["bets"]) == 1

        # Mock get_performance
        mock_get_resp.json.return_value = {"total_bets": 5, "roi": 4.2}
        perf = client.get_performance(period="month")
        assert perf["roi"] == 4.2


# ============================================================================
# 3. Streamlit AppTest Suite (All 5 Pages)
# ============================================================================


MOCK_HEALTH_DATA: dict[str, Any] = {
    "status": "ok",
    "version": "0.3.0",
    "database_connected": True,
    "last_odds_snapshot_utc": "2026-10-25T14:00:00Z",
    "toa_credits_remaining": 420,
    "search_quota": {
        "period": "2026-10",
        "tavily_credits_used": 15,
        "brave_credits_used": 25,
    },
}

MOCK_MATCHES_DATA: dict[str, Any] = {
    "matches": [
        {
            "id": 101,
            "league": "epl",
            "season": "2026-2027",
            "date_utc": "2026-10-26T18:00:00Z",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "status": "scheduled",
            "has_fair_matrix": True,
        }
    ],
    "total": 1,
}

MOCK_FAIR_PRICES_DATA: dict[str, Any] = {
    "match_id": 101,
    "match_name": "Arsenal — Chelsea",
    "league": "epl",
    "date_utc": "2026-10-26T18:00:00Z",
    "matrix_status": "sharp_closing",
    "standby_model_used": False,
    "methodological_caveats": [
        "Матрица построена на базе девигованных закрывающих котировок острых букмекеров.",
        "Резервная модель не использовалась.",
    ],
    "markets": {
        "1x2": {
            "p_fair": {"1": 0.50, "X": 0.28, "2": 0.22},
            "fair_odds": {"1": 2.00, "X": 3.57, "2": 4.55},
        },
        "btts": {
            "p_fair": {"yes": 0.55, "no": 0.45},
            "fair_odds": {"yes": 1.82, "no": 2.22},
        },
        "totals": {
            "2.5": {
                "p_fair": {"over": 0.52, "under": 0.48},
                "fair_odds": {"over": 1.92, "under": 2.08},
                "is_integer": False,
            },
            "2.0": {
                "p_fair": {"over": 0.60, "under": 0.20, "push": 0.20},
                "fair_odds": {"over": 1.33, "under": 4.00},
                "is_integer": True,
            },
        },
        "exact_score": {
            "p_fair": {"1:0": 0.11, "2:0": 0.09, "1:1": 0.12, "inny": 0.15},
            "fair_odds": {"1:0": 9.09, "2:0": 11.11, "1:1": 8.33, "inny": 6.67},
        },
    },
}

MOCK_FORTUNA_EVAL_DATA: dict[str, Any] = {
    "match_id": 101,
    "match_name": "Arsenal — Chelsea",
    "raw_lines_parsed": 4,
    "section_margins": {
        "exact_score": 0.312,
        "1x2": 0.055,
        "totals": 0.061,
    },
    "evaluations": [
        {
            "market": "exact_score",
            "outcome": "1:0",
            "fortuna_odds": 10.50,
            "fair_prob": 0.11,
            "fair_odds": 9.09,
            "edge": 0.155,
            "ev": 0.155,
            "is_value": True,
            "note": "Цена выше справедливой (+15.5%)",
        },
        {
            "market": "1x2",
            "outcome": "1",
            "fortuna_odds": 1.90,
            "fair_prob": 0.50,
            "fair_odds": 2.00,
            "edge": -0.05,
            "ev": -0.05,
            "is_value": False,
            "note": "Ожидаемая доходность отрицательная",
        },
    ],
}

MOCK_USER_BETS_DATA: dict[str, Any] = {
    "bets": [
        {
            "id": 1,
            "match_id": 101,
            "match_name": "Arsenal — Chelsea",
            "market": "exact_score",
            "outcome": "1:0",
            "odds": 10.50,
            "bookmaker": "Fortuna",
            "closing_fair_odds": 9.09,
            "clv": 15.51,
            "stake": 1.0,
            "status": "won",
            "pnl": 9.50,
            "placed_at_utc": "2026-10-25T12:00:00Z",
            "settled_at_utc": "2026-10-26T20:00:00Z",
        },
        {
            "id": 2,
            "match_id": 101,
            "match_name": "Arsenal — Chelsea",
            "market": "1x2",
            "outcome": "1",
            "odds": 1.90,
            "bookmaker": "Fortuna",
            "closing_fair_odds": 2.00,
            "clv": -5.00,
            "stake": 1.0,
            "status": "lost",
            "pnl": -1.00,
            "placed_at_utc": "2026-10-25T13:00:00Z",
            "settled_at_utc": "2026-10-26T20:00:00Z",
        },
    ],
    "total": 2,
}

MOCK_PERFORMANCE_DATA: dict[str, Any] = {
    "period": "all",
    "total_bets": 2,
    "settled_bets": 2,
    "pending_bets": 0,
    "turnover": 2.0,
    "total_profit": 8.50,
    "roi": 425.0,
    "avg_clv": 5.25,
    "hit_rate": 50.0,
    "bootstrap_ci_roi_95": {"lower": -50.0, "upper": 900.0},
    "bootstrap_ci_clv_95": {"lower": -5.0, "upper": 15.5},
    "sample_size_warning": (
        "⚠️ Внимание: выборка < 100 ставок (рассчитано 2 ставок). "
        "Оценка ROI и CLV имеет высокую погрешность из-за случайных колебаний."
    ),
}


@pytest.fixture
def mock_api_client():
    """Mock ApiClient methods so AppTest runs without external network calls."""
    with patch.object(ApiClient, "get_health", return_value=MOCK_HEALTH_DATA), \
         patch.object(ApiClient, "get_matches", return_value=MOCK_MATCHES_DATA), \
         patch.object(ApiClient, "get_fair_prices", return_value=MOCK_FAIR_PRICES_DATA), \
         patch.object(ApiClient, "evaluate_fortuna", return_value=MOCK_FORTUNA_EVAL_DATA), \
         patch.object(ApiClient, "get_user_bets", return_value=MOCK_USER_BETS_DATA), \
         patch.object(ApiClient, "get_performance", return_value=MOCK_PERFORMANCE_DATA):
        yield


APP_PATH = str(pathlib.Path(__file__).parent.parent / "dashboard" / "app.py")


def test_app_default_page_matches(mock_api_client: None) -> None:
    """Test initial page (Matches & Fair Prices) renders properly."""
    at = AppTest.from_file(APP_PATH, default_timeout=15.0)
    at.run()
    assert not at.exception
    # Check headers and markdown elements
    header_texts = [h.value for h in at.header]
    assert any("Матчи и справедливые цены" in h for h in header_texts)


def test_app_page_fortuna(mock_api_client: None) -> None:
    """Test navigating to Fortuna Evaluation page."""
    at = AppTest.from_file(APP_PATH, default_timeout=15.0)
    at.run()
    # Switch radio to Fortuna page
    at.sidebar.radio[0].set_value("Оценка цен Fortuna").run()
    assert not at.exception
    header_texts = [h.value for h in at.header]
    assert any("Оценка цен Fortuna" in h for h in header_texts)


def test_app_page_bets(mock_api_client: None) -> None:
    """Test navigating to User Bets page."""
    at = AppTest.from_file(APP_PATH, default_timeout=15.0)
    at.run()
    at.sidebar.radio[0].set_value("Мои ставки").run()
    assert not at.exception
    header_texts = [h.value for h in at.header]
    assert any("Мои ставки" in h for h in header_texts)


def test_app_page_stats(mock_api_client: None) -> None:
    """Test navigating to Stats & CLV Audit page, verifying warning and metrics."""
    at = AppTest.from_file(APP_PATH, default_timeout=15.0)
    at.run()
    at.sidebar.radio[0].set_value("Статистика и CLV").run()
    assert not at.exception
    header_texts = [h.value for h in at.header]
    assert any("Статистика портфеля и аудит CLV" in h for h in header_texts)
    # Check for small sample size warning
    warning_texts = [w.value for w in at.warning]
    assert any("< 100 ставок" in w for w in warning_texts)


def test_app_page_stats_negative_roi_formatting(mock_api_client: None) -> None:
    """Unit contract: when API returns roi: -20.0, the dashboard displays exactly -20.0%."""
    negative_perf = dict(MOCK_PERFORMANCE_DATA)
    negative_perf["roi"] = -20.0
    with patch.object(ApiClient, "get_performance", return_value=negative_perf):
        at = AppTest.from_file(APP_PATH, default_timeout=15.0)
        at.run()
        at.sidebar.radio[0].set_value("Статистика и CLV").run()
        assert not at.exception

        roi_metrics = [m for m in at.metric if m.label == "ROI"]
        assert len(roi_metrics) == 1
        assert roi_metrics[0].value == "-20.0%"


def test_app_page_status(mock_api_client: None) -> None:
    """Test navigating to System & Data Status page."""
    at = AppTest.from_file(APP_PATH, default_timeout=15.0)
    at.run()
    at.sidebar.radio[0].set_value("Состояние данных").run()
    assert not at.exception
    header_texts = [h.value for h in at.header]
    assert any("Состояние данных и квоты" in h for h in header_texts)


def test_app_api_error_graceful_handling() -> None:
    """Test that API communication errors are rendered gracefully via st.error."""
    with patch.object(ApiClient, "get_matches", side_effect=RuntimeError("API Connection refused")):
        at = AppTest.from_file(APP_PATH, default_timeout=15.0)
        at.run()
        # Uncaught exceptions must be 0
        assert not at.exception
        # st.error must contain error message
        assert len(at.error) > 0
        assert any("API Connection refused" in err.value for err in at.error)


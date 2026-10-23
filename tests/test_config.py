"""Config shape and defaults from docs/decisions.md."""

from __future__ import annotations

import pytest

from core.config import AppConfig, load_config, load_secrets


@pytest.fixture()
def config() -> AppConfig:
    return load_config()


def test_all_leagues_present(config: AppConfig) -> None:
    slugs = [lg.slug for lg in config.leagues]
    assert slugs == ["premier_league", "la_liga", "serie_a", "bundesliga",
                     "ligue_1", "ucl"]


def test_ligue_one_sport_key(config: AppConfig) -> None:
    # phase-0 finding: soccer_france_ligue_one, NOT soccer_france_ligue_1
    ligue = next(lg for lg in config.leagues if lg.slug == "ligue_1")
    assert ligue.toa_sport_key == "soccer_france_ligue_one"


def test_ucl_experimental(config: AppConfig) -> None:
    ucl = next(lg for lg in config.leagues if lg.slug == "ucl")
    assert ucl.ucl and ucl.experimental
    assert ucl.fd_code is None and ucl.understat_league is None
    assert ucl.totals_edge_threshold == 0.06


def test_odds_policy_defaults(config: AppConfig) -> None:
    p = config.odds_policy
    assert p.t3_granularity == "slot"           # decisions, final round S1
    assert p.t1_bet_market is False
    assert p.hard_stop_credits == 450
    assert p.tier_windows.t48 == (40.0, 56.0)
    assert p.tier_windows.t24 == (18.0, 30.0)
    assert p.tier_windows.t3 == (1.0, 4.0)
    assert p.degradation_ladder[-1] == "t3_slot_to_day"  # T-3 never disabled


def test_prefilter(config: AppConfig) -> None:
    assert config.prefilter.edge_fraction == 0.5
    assert config.prefilter.weekly_cap == 15


def test_books_fortuna_only_bettable(config: AppConfig) -> None:
    assert config.books.bookmaker_whitelist == ["fortuna"]
    assert "Pinnacle" in config.books.market_reference_books
    assert "1xBet" in config.books.excluded_books


def test_totals_lines_half_only(config: AppConfig) -> None:
    assert config.totals_lines.half_lines_only
    assert 0.5 in config.totals_lines.requested


def test_llm_models(config: AppConfig) -> None:
    assert config.llm.extraction_model == "gemini-3.8-flash"
    assert config.llm.reasoning_model == "gemini-3.8-flash"
    assert config.llm.reasoning_thinking_budget == 4096
    assert config.llm.fallback_model == "gemini-3.7-flash"


def test_secrets_never_required() -> None:
    # secrets come from .env; empty defaults must not break loading
    s = load_secrets()
    assert isinstance(s.api_token, str)

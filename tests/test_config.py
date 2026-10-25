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
    assert ucl.totals_edge_threshold is None


def test_odds_policy_defaults(config: AppConfig) -> None:
    p = config.odds_policy
    assert p.t3_granularity == "slot"           # decisions, final round S1
    assert p.t1_bet_market is False
    assert p.monthly_quota == 500
    assert p.hard_stop_credits == 450
    assert p.pacing.enabled and p.pacing.safety_margin_credits == 25
    assert p.t4_recheck is True
    assert p.postponement.shift_min == 15
    assert p.postponement.no_result_after_h == 3
    assert p.postponement.void_after_h == 48
    assert p.tier_windows.t48 == (40.0, 56.0)
    assert p.tier_windows.t24 == (18.0, 30.0)
    assert p.tier_windows.t3 == (1.0, 4.0)
    assert p.active_tiers == ["t24", "t3"]      # T-48 disabled by default (Phase 4b B1)
    assert p.tier_markets["t24"] == ["totals", "h2h"]
    assert p.tier_markets["t3"] == ["totals", "h2h"]
    assert p.pinnacle_correct_score_max_weekly == 10
    assert p.degradation_ladder == [
        "cap_correct_score_5_week",
        "disable_t3_h2h",
        "t3_slot_to_day",
    ]
    assert p.degradation_ladder[-1] == "t3_slot_to_day"  # T-3 never disabled


def test_prefilter(config: AppConfig) -> None:
    assert config.prefilter.edge_fraction == 0.5
    assert config.prefilter.weekly_cap == 15


def test_books_fortuna_only_bettable(config: AppConfig) -> None:
    assert config.books.bookmaker_whitelist == ["fortuna"]
    assert "pinnacle" in config.books.market_reference_books  # TOA key
    assert "onexbet" in config.books.excluded_books           # TOA key


def test_scheduler_polls_free_events_only(config: AppConfig) -> None:
    sched = type(config.scheduler).model_fields
    assert "events_poll_cron" in sched and "refresh_odds_cron" not in sched


def test_database_url_env_override(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./from-env.db")
    cfg = load_config()
    assert cfg.database.url == "sqlite:///./from-env.db"


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

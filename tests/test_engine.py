"""Tests for Value Engine: edge calculation, thresholds, risk flags, and table formatting."""

from __future__ import annotations

import datetime as dt

import pytest

from core.value.engine import (
    calculate_kelly_quarter_capped,
    check_model_anomaly,
    evaluate_fortuna_prices,
)
from core.value.fair_matrix import (
    FairMatrix,
    MarketInputs,
    MarketOdds1X2,
    MarketTotalsLine,
    build_fair_matrix,
)
from core.value.fortuna_input import (
    FortunaPrice,
    parse_fortuna_text,
)


@pytest.fixture()
def sample_fair_matrix() -> FairMatrix:
    # Match with ~2.7 total goals, home share ~55%
    inputs = MarketInputs(
        source="toa_snapshot",
        snapshot_time=dt.datetime(2026, 10, 24, 10, 0),
        totals_by_book={"pinnacle": [MarketTotalsLine(line=2.5, over=1.85, under=2.05)]},
        h2h_by_book={"pinnacle": MarketOdds1X2(home=1.95, draw=3.60, away=4.20)},
        match_name="Team A - Team B",
    )
    return build_fair_matrix(inputs)


def test_calculate_kelly_quarter_capped() -> None:
    # 1. Negative edge -> 0.0
    assert calculate_kelly_quarter_capped(0.50, 1.90) == 0.0
    assert calculate_kelly_quarter_capped(0.0, 2.0) == 0.0
    assert calculate_kelly_quarter_capped(0.5, 0.9) == 0.0

    # 2. Positive edge: p=0.6, odds=2.0 -> edge = 0.20
    # Full Kelly: edge / (odds - 1) = 0.20 / 1.0 = 0.20
    # Quarter Kelly: 0.20 / 4 = 0.05
    # Capped at 0.02 -> 0.02
    assert calculate_kelly_quarter_capped(0.60, 2.00, cap=0.02) == pytest.approx(0.02)

    # Small edge: p=0.52, odds=2.0 -> edge = 0.04
    # Full Kelly: 0.04 / 1.0 = 0.04 -> Quarter: 0.01
    assert calculate_kelly_quarter_capped(0.52, 2.00, cap=0.02) == pytest.approx(0.01)


def test_check_model_anomaly() -> None:
    # 1. No model share -> no anomaly
    ano, msg = check_model_anomaly(0.55, None, threshold=0.15)
    assert not ano and msg == ""

    # 2. Within threshold: diff 0.10 <= 0.15
    ano, msg = check_model_anomaly(0.55, 0.45, threshold=0.15)
    assert not ano and msg == ""

    # 3. Beyond threshold: diff 0.20 > 0.15 -> anomaly flagged
    ano, msg = check_model_anomaly(0.60, 0.40, threshold=0.15)
    assert ano
    assert "Аномалия модели" in msg
    assert "Проверь составы/данные" in msg


def test_exact_score_thresholds_and_rejections(sample_fair_matrix: FairMatrix) -> None:
    # Get fair probability of 2:1
    p_21 = sample_fair_matrix.exact_score(2, 1)
    fair_odds_21 = 1.0 / p_21

    # Case 1: Value recommendation (edge >= 12%, p >= 4%, odds <= 21)
    good_price = fair_odds_21 * 1.15  # 15% edge
    item_good = FortunaPrice("exact_score", (2, 1), good_price, "2:1", 1)

    # Case 2: Rejected - Below edge threshold (edge = 5% < 12%)
    small_price = fair_odds_21 * 1.05
    item_small = FortunaPrice("exact_score", (2, 1), small_price, "2:1", 2)

    # Case 3: Rejected - Long odds (> 21.0)
    item_long = FortunaPrice("exact_score", (4, 3), 35.0, "4:3", 3)

    # Case 4: Rejected - Small p_fair (< 4%) with odds <= 21
    p_40 = sample_fair_matrix.exact_score(4, 0)
    assert p_40 < 0.04
    item_low_p = FortunaPrice("exact_score", (4, 0), 18.0, "4:0", 4)

    res = evaluate_fortuna_prices(
        sample_fair_matrix,
        [item_good, item_small, item_long, item_low_p],
    )

    assert res.recommendations_count == 1

    # Check outcomes
    out_good = res.outcomes[0]
    assert out_good.recommended is True
    assert out_good.stake_flat == 1.0
    assert out_good.kelly_quarter_capped > 0.0

    out_small = res.outcomes[1]
    assert out_small.recommended is False
    assert "Ниже порога edge" in out_small.rejection_reason

    out_long = res.outcomes[2]
    assert out_long.recommended is False
    assert "Длинный коэффициент" in out_long.rejection_reason

    out_low_p = res.outcomes[3]
    assert out_low_p.recommended is False
    assert "Мала p_fair" in out_low_p.rejection_reason


def test_multiscore_and_goals_range_thresholds(sample_fair_matrix: FairMatrix) -> None:
    scores = [(1, 0), (2, 0), (2, 1)]
    p_multi = sample_fair_matrix.multiscore(scores)
    fair_odds_multi = 1.0 / p_multi

    # Multiscore with 10% edge (>= 8%)
    item_multi_good = FortunaPrice(
        "multiscore", frozenset(scores), fair_odds_multi * 1.10, "multi", 1,
    )
    # Multiscore with 5% edge (< 8%)
    item_multi_bad = FortunaPrice(
        "multiscore", frozenset(scores), fair_odds_multi * 1.05, "multi", 2,
    )

    # Goals range 2-4 with 10% edge
    p_grange = sample_fair_matrix.goals_range(2, 4)
    item_range_good = FortunaPrice("goals_range", (2, 4), (1.0 / p_grange) * 1.10, "2-4", 3)

    res = evaluate_fortuna_prices(
        sample_fair_matrix,
        [item_multi_good, item_multi_bad, item_range_good],
    )
    assert res.outcomes[0].recommended is True
    assert res.outcomes[1].recommended is False
    assert "Ниже порога edge" in res.outcomes[1].rejection_reason
    assert res.outcomes[2].recommended is True


def test_reference_totals_and_h2h_never_recommended(sample_fair_matrix: FairMatrix) -> None:
    # Even with huge edge, totals and 1X2 are strictly reference-only
    item_tot = FortunaPrice("totals", ("over", 2.5), 5.00, "over 2.5", 1)
    item_h2h = FortunaPrice("h2h", "1", 5.00, "1", 2)

    res = evaluate_fortuna_prices(sample_fair_matrix, [item_tot, item_h2h])
    assert res.recommendations_count == 0
    assert res.outcomes[0].recommended is False
    assert "Справочный рынок" in res.outcomes[0].rejection_reason
    assert res.outcomes[1].recommended is False
    assert "Справочный рынок" in res.outcomes[1].rejection_reason


def test_risk_flags_multiplier_and_exclusion(sample_fair_matrix: FairMatrix) -> None:
    p_21 = sample_fair_matrix.exact_score(2, 1)
    # Edge is 14% (would be recommended with base 12%, but fails 12% * 1.5 = 18%)
    item_edge14 = FortunaPrice("exact_score", (2, 1), (1.0 / p_21) * 1.14, "2:1", 1)
    # Edge is 22% (passes even with 18% threshold)
    item_edge22 = FortunaPrice("exact_score", (2, 1), (1.0 / p_21) * 1.22, "2:1", 2)

    # 1. One key player absent -> edge threshold x 1.5
    res_one_abs = evaluate_fortuna_prices(
        sample_fair_matrix,
        [item_edge14, item_edge22],
        key_absences_home=["Erling Haaland"],
    )
    assert any("отсутствует ключевой игрок" in f for f in res_one_abs.risk_flags)
    assert res_one_abs.outcomes[0].recommended is False
    assert "Ниже порога edge" in res_one_abs.outcomes[0].rejection_reason
    assert res_one_abs.outcomes[1].recommended is True

    # 2. Two key players absent in one team -> total exclusion
    res_two_abs = evaluate_fortuna_prices(
        sample_fair_matrix,
        [item_edge14, item_edge22],
        key_absences_home=["Erling Haaland", "Kevin De Bruyne"],
    )
    assert res_two_abs.is_excluded is True
    assert res_two_abs.recommendations_count == 0
    assert "Исключено" in res_two_abs.outcomes[1].rejection_reason

    # 3. Conflicting sources -> threshold x 1.5
    res_conflict = evaluate_fortuna_prices(
        sample_fair_matrix,
        [item_edge14],
        sources_conflict=True,
    )
    assert any("Противоречие источников" in f for f in res_conflict.risk_flags)
    assert res_conflict.outcomes[0].recommended is False


def test_evaluation_summary_table_output(sample_fair_matrix: FairMatrix) -> None:
    text = """
    1:0 @ 7.00
    2:1 @ 9.50
    1:0, 2:0, 2:1 @ 3.40
    2-4 gole @ 1.80
    over 2.5 1.95
    1 @ 2.05
    """
    prices = parse_fortuna_text(text)
    res = evaluate_fortuna_prices(
        sample_fair_matrix,
        prices,
        match_name="Arsenal - Chelsea",
        model_home_share=0.35,  # large divergence with market (~55%) -> triggers anomaly warning
    )
    table = res.format_table()
    assert "Матч: Arsenal - Chelsea" in table
    assert "Маржа Fortuna:" in table
    assert "Аномалия модели" in table
    assert "Исход" in table and "Fortuna" in table and "p_fair" in table and "Edge" in table
    assert "Итого рекомендаций:" in table

"""Tests for Value Engine: edge calculation, thresholds, risk flags, and table formatting."""

from __future__ import annotations

import datetime as dt

import numpy as np
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


def test_inny_incomplete_vs_complete_grid(sample_fair_matrix: FairMatrix) -> None:
    # 1. Incomplete grid: 3 scores + inny
    p_inny = FortunaPrice("other", "other", 25.0, "inny", 4)
    res_incomp = evaluate_fortuna_prices(
        sample_fair_matrix,
        [
            FortunaPrice("exact_score", (1, 0), 7.0, "1:0", 1),
            FortunaPrice("exact_score", (2, 0), 10.0, "2:0", 2),
            FortunaPrice("exact_score", (2, 1), 9.0, "2:1", 3),
            p_inny,
        ],
    )
    inny_out = res_incomp.outcomes[-1]
    assert inny_out.recommended is False
    assert "Неполная сетка Fortuna (3 из 25): inny не оценивается" in inny_out.rejection_reason

    # 2. Complete grid: all 25 scores + inny
    all_25_prices = [
        FortunaPrice("exact_score", (h, a), 15.0, f"{h}:{a}", i + 1)
        for i, (h, a) in enumerate([(h, a) for h in range(5) for a in range(5)])
    ]
    all_25_prices.append(FortunaPrice("other", "other", 25.0, "inny", 26))
    res_comp = evaluate_fortuna_prices(sample_fair_matrix, all_25_prices)
    inny_out_comp = res_comp.outcomes[-1]
    assert inny_out_comp.recommended is False
    assert inny_out_comp.p_fair is not None
    assert inny_out_comp.p_fair == pytest.approx(1.0 - sample_fair_matrix.matrix[:5, :5].sum())
    assert "Справочно: inny (хвост 5+ смещен, z=-2.28)" in inny_out_comp.rejection_reason


def test_implausible_edge_guards(sample_fair_matrix: FairMatrix) -> None:
    p_21 = sample_fair_matrix.exact_score(2, 1)
    # Exact score with 45% edge (> 40% cap) -> rejected as implausible
    p_crazy_cs = (1.0 / p_21) * 1.45
    item_crazy_cs = FortunaPrice("exact_score", (2, 1), p_crazy_cs, "2:1", 1)

    # Multiscore with 30% edge (> 25% cap) -> rejected as implausible
    p_multi = sample_fair_matrix.multiscore([(1, 0), (2, 0)])
    p_crazy_ms = (1.0 / p_multi) * 1.30
    item_crazy_ms = FortunaPrice("multiscore", frozenset([(1, 0), (2, 0)]), p_crazy_ms, "multi", 2)

    # Goals range with 28% edge (> 25% cap) -> rejected as implausible
    p_range = sample_fair_matrix.goals_range(2, 4)
    p_crazy_rg = (1.0 / p_range) * 1.28
    item_crazy_rg = FortunaPrice("goals_range", (2, 4), p_crazy_rg, "2-4 gole", 3)

    res = evaluate_fortuna_prices(
        sample_fair_matrix,
        [item_crazy_cs, item_crazy_ms, item_crazy_rg],
    )
    for out in res.outcomes:
        assert out.recommended is False
        assert "Неправдоподобный edge" in out.rejection_reason


def test_integer_and_quarter_totals_lines(sample_fair_matrix: FairMatrix) -> None:
    """Integer lines must account for push; quarter lines must be cleanly rejected."""
    # Integer line 3.0
    item_u3 = FortunaPrice("totals", ("under", 3.0), 1.90, "mniej niż 3 1.90", 1)
    item_o3 = FortunaPrice("totals", ("over", 3.0), 1.95, "więcej niż 3 1.95", 2)

    # Quarter lines 2.25 and 2.75
    item_q1 = FortunaPrice("totals", ("under", 2.25), 1.85, "mniej niż 2.25", 3)
    item_q2 = FortunaPrice("totals", ("over", 2.75), 2.05, "więcej niż 2.75", 4)

    res = evaluate_fortuna_prices(
        sample_fair_matrix,
        [item_u3, item_o3, item_q1, item_q2],
    )
    assert len(res.outcomes) == 4

    out_u3, out_o3 = res.outcomes[0], res.outcomes[1]
    n = sample_fair_matrix.matrix.shape[0]
    totals_grid = np.add.outer(np.arange(n), np.arange(n))
    p_push_3 = float(sample_fair_matrix.matrix[totals_grid == 3].sum())

    # P(under 3) + P(push 3) + P(over 3) must sum to 1.0!
    assert out_u3.p_fair + out_o3.p_fair + p_push_3 == pytest.approx(1.0, abs=1e-9)
    assert out_u3.recommended is False
    assert out_u3.rejection_reason == "Справочный рынок"

    # Edge must be EV = P(win) * odds + P(push) * 1 - 1
    ev_u3 = out_u3.p_fair * item_u3.odds + p_push_3 * 1.0 - 1.0
    assert out_u3.edge == pytest.approx(ev_u3, abs=1e-9)
    assert out_u3.fair_odds == pytest.approx((1.0 - p_push_3) / out_u3.p_fair, abs=1e-9)

    # Quarter lines
    out_q1, out_q2 = res.outcomes[2], res.outcomes[3]
    assert out_q1.recommended is False
    assert out_q1.p_fair == 0.0
    assert "Четвертная линия тотала не поддерживается" in out_q1.rejection_reason
    assert out_q2.recommended is False
    assert out_q2.p_fair == 0.0
    assert "Четвертная линия тотала не поддерживается" in out_q2.rejection_reason


def test_integer_totals_expected_value_and_fair_odds() -> None:
    """User requirement: W=0.40, Q=0.25, odds=1.80 -> EV = -0.03.

    EV = P(win) * odds + P(push) * 1 - 1
    fair_odds = (1 - P(push)) / P(win)
    """
    p_win = 0.40
    p_push = 0.25
    odds = 1.80
    ev = p_win * odds + p_push * 1.0 - 1.0
    assert ev == pytest.approx(-0.03, abs=1e-9)
    fair_odds = (1.0 - p_push) / p_win
    assert fair_odds == pytest.approx(1.875, abs=1e-9)

    mat = np.zeros((11, 11), dtype=np.float64)
    # under 3: sum = 0.40
    mat[0, 0] = 0.10
    mat[1, 0] = 0.15
    mat[0, 2] = 0.15
    # push 3: sum = 0.25
    mat[1, 2] = 0.25
    # over 3: sum = 0.35
    mat[2, 2] = 0.35

    fm = FairMatrix(
        matrix=mat,
        lambda_home=1.5,
        lambda_away=1.5,
        lambda_total=3.0,
        home_share=0.5,
        line_used=2.5,
        source="synthetic",
        status="ok",
        books_used=["test"],
        snapshot_time=None,
        input_hash="test",
        p_over_devig=0.5,
        p_home_devig=0.4,
        p_draw_devig=0.3,
        p_away_devig=0.3,
        rho=0.0,
        kappa_high=0.0,
    )

    item = FortunaPrice("totals", ("under", 3.0), 1.80, "mniej niż 3 1.80", 1)
    res = evaluate_fortuna_prices(fm, [item])
    out = res.outcomes[0]
    assert out.p_fair == pytest.approx(0.40, abs=1e-9)
    assert out.edge == pytest.approx(-0.03, abs=1e-9)
    assert out.fair_odds == pytest.approx(1.875, abs=1e-9)


def test_team_totals_side_matching(sample_fair_matrix: FairMatrix) -> None:
    """Test side determination for team totals, handling abbreviations like 'Real M.'."""
    item_real = FortunaPrice("team_totals", ("Real M.", "over", 1.5), 1.90, "+ 1.5 1.90", 1)
    item_barca = FortunaPrice("team_totals", ("Barcelona", "over", 1.5), 1.80, "+ 1.5 1.80", 2)
    item_unkn = FortunaPrice("team_totals", ("Unknown FC", "over", 1.5), 2.00, "+ 1.5 2.00", 3)

    res = evaluate_fortuna_prices(
        sample_fair_matrix,
        [item_real, item_barca, item_unkn],
        match_name="Barcelona - Real Madrid",
        home_team="Barcelona",
        away_team="Real Madrid",
    )

    out_real = res.outcomes[0]
    out_barca = res.outcomes[1]
    out_unkn = res.outcomes[2]

    # Real M. matches away team (Real Madrid), Barcelona matches home team
    # Home team in sample_fair_matrix is stronger, so home over 1.5 > away over 1.5
    marg_home = sample_fair_matrix.matrix.sum(axis=1)
    marg_away = sample_fair_matrix.matrix.sum(axis=0)
    exp_barca = float(sum(marg_home[g] for g in range(len(marg_home)) if g > 1.5))
    exp_real = float(sum(marg_away[g] for g in range(len(marg_away)) if g > 1.5))

    assert out_barca.p_fair == pytest.approx(exp_barca, abs=1e-9)
    assert out_real.p_fair == pytest.approx(exp_real, abs=1e-9)
    # Real M. is NOT Barcelona
    assert out_real.p_fair != pytest.approx(exp_barca, abs=1e-3)

    # Unknown team is rejected as unmatchable
    assert out_unkn.p_fair == 0.0
    assert "Не удалось однозначно сопоставить команду" in out_unkn.rejection_reason  # noqa: RUF001


def test_multiwynik_11_groups_exact_partition(sample_fair_matrix: FairMatrix) -> None:
    """Sum of fair probabilities across all 11 Multiwynik groups must equal 1.0 within 1e-9."""
    groups: list[FortunaPrice] = [
        # 4 home groups
        FortunaPrice("multiscore", frozenset([(1, 0), (2, 0), (3, 0)]), 6.60, "1:0 2:0 3:0", 1),
        FortunaPrice("multiscore", frozenset([(2, 1), (3, 1), (4, 1)]), 4.30, "2:1 3:1 4:1", 2),
        FortunaPrice(
            "multiscore", frozenset([(3, 2), (4, 2), (4, 3), (5, 1)]), 5.80, "3:2 4:2 4:3 5:1", 3
        ),
        FortunaPrice("multiscore", frozenset([(4, 0), (5, 0), (6, 0)]), 16.00, "4:0 5:0 6:0", 4),
        # Home other
        FortunaPrice("multiscore", "multi:home_other", 9.20, "1.drużyna wygra dowolnym innym", 5),
        # Remis (all draws)
        FortunaPrice("multiscore", "multi:remis", 4.60, "Remis", 6),
        # 4 away groups
        FortunaPrice("multiscore", frozenset([(0, 1), (0, 2), (0, 3)]), 18.00, "0:1 0:2 0:3", 7),
        FortunaPrice("multiscore", frozenset([(1, 2), (1, 3), (1, 4)]), 10.00, "1:2 1:3 1:4", 8),
        FortunaPrice(
            "multiscore", frozenset([(2, 3), (2, 4), (3, 4), (1, 5)]), 13.00, "2:3 2:4 3:4 1:5", 9
        ),
        FortunaPrice("multiscore", frozenset([(0, 4), (0, 5), (0, 6)]), 100.00, "0:4 0:5 0:6", 10),
        # Away other
        FortunaPrice("multiscore", "multi:away_other", 45.00, "2.drużyna wygra dowolnym innym", 11),
    ]

    res = evaluate_fortuna_prices(sample_fair_matrix, groups)
    assert len(res.outcomes) == 11
    total_p = sum(out.p_fair for out in res.outcomes)
    assert total_p == pytest.approx(1.0, abs=1e-9)


def test_evaluate_all_three_real_fixtures(sample_fair_matrix: FairMatrix) -> None:
    """Verify all 3 Fortuna fixtures evaluate without errors and compute realistic overrounds."""
    from pathlib import Path

    from core.value.fortuna_input import parse_fortuna_text

    fixtures = [
        "fortuna_barcelona_real_2026-10-25.txt",
        "fortuna_udinese_milan_2026-10-25.txt",
        "fortuna_nice_rennes_2026-10-25.txt",
    ]

    for fname in fixtures:
        p = Path(f"tests/fixtures/{fname}")
        assert p.exists()
        prices = parse_fortuna_text(p.read_text(encoding="utf-8"))
        assert len(prices) > 60
        res = evaluate_fortuna_prices(sample_fair_matrix, prices, match_name=p.stem)
        assert len(res.outcomes) == len(prices)
        assert res.fortuna_overround is not None
        assert 0.28 < res.fortuna_overround < 0.38

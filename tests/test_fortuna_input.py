"""Tests for Fortuna manual odds parser and overround calculation (Step 3)."""

from __future__ import annotations

import pytest

from core.value.fortuna_input import (
    FortunaParseError,
    calculate_fortuna_overround,
    parse_fortuna_line,
    parse_fortuna_text,
)


def test_parse_exact_score_formats() -> None:
    cases = [
        ("2:1 9.50", (2, 1), 9.50),
        ("2-1 @ 9,50", (2, 1), 9.50),
        ("2:1 - 9.5", (2, 1), 9.5),
        ("2:1 = 9.50", (2, 1), 9.50),
        ("0:0 12,0", (0, 0), 12.0),
        ("3-2 @ 34.0", (3, 2), 34.0),
        ("1:0 : 6.50", (1, 0), 6.50),
        ("4:1   25,00", (4, 1), 25.0),
    ]
    for raw, exp_sel, exp_odds in cases:
        p = parse_fortuna_line(raw)
        assert p.market == "exact_score"
        assert p.selection == exp_sel
        assert p.odds == pytest.approx(exp_odds)


def test_parse_other_score_variants() -> None:
    variants = [
        ("inny wynik 45.0", 45.0),
        ("inny @ 45,0", 45.0),
        ("pozostałe 50.0", 50.0),
        ("pozostale @ 50,0", 50.0),
        ("other 40.0", 40.0),
        ("any other @ 40,0", 40.0),
        ("любой другой 35.0", 35.0),
        ("другой счёт @ 35,0", 35.0),
    ]
    for raw, exp_odds in variants:
        p = parse_fortuna_line(raw)
        assert p.market == "other"
        assert p.selection == "other"
        assert p.odds == pytest.approx(exp_odds)


def test_parse_multiscore_formats() -> None:
    cases = [
        ("1:0, 2:0, 2:1 @ 3.40", frozenset([(1, 0), (2, 0), (2, 1)]), 3.40),
        ("1:0 / 2:0 / 2:1 3.40", frozenset([(1, 0), (2, 0), (2, 1)]), 3.40),
        ("0:1, 0:2, 1:2 @ 4.25", frozenset([(0, 1), (0, 2), (1, 2)]), 4.25),
        ("1:1, 2:2 @ 5,50", frozenset([(1, 1), (2, 2)]), 5.50),
    ]
    for raw, exp_sel, exp_odds in cases:
        p = parse_fortuna_line(raw)
        assert p.market == "multiscore"
        assert p.selection == exp_sel
        assert p.odds == pytest.approx(exp_odds)


def test_parse_goals_range_formats() -> None:
    cases = [
        ("2-4 gole @ 1.80", (2, 4), 1.80),
        ("2–4 bramki 1,80", (2, 4), 1.80),
        ("2-4 гола 1.80", (2, 4), 1.80),
        ("0-1 goli @ 3.20", (0, 1), 3.20),
        ("2-3 goals 2.05", (2, 3), 2.05),
    ]
    for raw, exp_sel, exp_odds in cases:
        p = parse_fortuna_line(raw)
        assert p.market == "goals_range"
        assert p.selection == exp_sel
        assert p.odds == pytest.approx(exp_odds)


def test_dashes_without_keywords_parse_as_exact_score() -> None:
    """Hyphen, en-dash, em-dash without range keywords must parse as exact score."""
    cases = [
        ("1–2 7.00", (1, 2), 7.00),
        ("2–1 9.50", (2, 1), 9.50),
        ("2–4 @ 1.85", (2, 4), 1.85),
        ("3—1 15.0", (3, 1), 15.0),
        ("0-0 10.0", (0, 0), 10.0),
    ]
    for raw, exp_sel, exp_odds in cases:
        p = parse_fortuna_line(raw)
        assert p.market == "exact_score"
        assert p.selection == exp_sel
        assert p.odds == pytest.approx(exp_odds)


def test_exact_score_outside_grid_raises_error() -> None:
    """Scores outside config grid (0..4) must raise FortunaParseError."""
    with pytest.raises(FortunaParseError) as exc1:
        parse_fortuna_line("5:0 12.0")
    assert "вне стандартной сетки Fortuna 0..4" in str(exc1.value)

    with pytest.raises(FortunaParseError) as exc2:
        parse_fortuna_line("0:5 15.0")
    assert "вне стандартной сетки Fortuna 0..4" in str(exc2.value)

    with pytest.raises(FortunaParseError) as exc3:
        parse_fortuna_line("5-2 30.0")
    assert "вне стандартной сетки Fortuna 0..4" in str(exc3.value)


def test_parse_reference_totals_and_h2h() -> None:
    # Totals
    t_cases = [
        ("over 2.5 1.95", ("over", 2.5), 1.95),
        ("powyżej 2.5 @ 1,95", ("over", 2.5), 1.95),
        ("+2.5 1.95", ("over", 2.5), 1.95),
        ("under 2.5 1.85", ("under", 2.5), 1.85),
        ("poniżej 2.5 @ 1,85", ("under", 2.5), 1.85),
        ("-2.5 1.85", ("under", 2.5), 1.85),
    ]
    for raw, exp_totals_sel, exp_totals_odds in t_cases:
        p = parse_fortuna_line(raw)
        assert p.market == "totals"
        assert p.selection == exp_totals_sel
        assert p.odds == pytest.approx(exp_totals_odds)

    # H2H 1X2
    h_cases = [
        ("1 @ 2.10", "1", 2.10),
        ("X @ 3.40", "X", 3.40),
        ("remis 3,40", "X", 3.40),
        ("2 @ 3.50", "2", 3.50),
    ]
    for raw_h, exp_h_sel, exp_h_odds in h_cases:
        p_h = parse_fortuna_line(raw_h)
        assert p_h.market == "h2h"
        assert p_h.selection == exp_h_sel
        assert p_h.odds == pytest.approx(exp_h_odds)


def test_parse_fortuna_multi_line_text() -> None:
    text = """
    # Fortuna odds batch
    1:0 @ 6.50
    2:0 @ 9.00
    2:1 @ 8.50
    0:0 @ 11.00
    1:1 @ 6.50
    2:2 @ 14.00
    0:1 @ 9.50
    0:2 @ 15.00
    1:2 @ 12.00
    inny wynik @ 40.00
    1:0, 2:0, 2:1 @ 3.40
    2-4 gole @ 1.80
    over 2.5 1.95
    1 @ 2.20
    """
    items = parse_fortuna_text(text)
    assert len(items) == 14
    markets = {p.market for p in items}
    assert markets == {"exact_score", "other", "multiscore", "goals_range", "totals", "h2h"}


def test_conflicting_odds_raises_parse_error() -> None:
    text = """
    1:0 @ 6.50
    2:1 @ 8.50
    1:0 @ 7.00
    """
    with pytest.raises(FortunaParseError) as exc_info:
        parse_fortuna_text(text)
    err = exc_info.value
    assert "Конфликт коэффициентов" in str(err)
    assert err.line_num == 4


def test_identical_duplicate_odds_deduplicated() -> None:
    text = """
    1:0 @ 6.50
    2:1 @ 8.50
    1:0 @ 6.50
    """
    items = parse_fortuna_text(text)
    assert len(items) == 2
    assert [p.selection for p in items] == [(1, 0), (2, 1)]


def test_invalid_syntax_raises_error_with_line_number() -> None:
    text = """
    1:0 @ 6.50
    some corrupted garbage here
    2:1 @ 8.50
    """
    with pytest.raises(FortunaParseError) as exc_info:
        parse_fortuna_text(text)
    assert exc_info.value.line_num == 3
    assert "some corrupted garbage here" in str(exc_info.value)


def test_invalid_odds_below_one_raises_error() -> None:
    with pytest.raises(FortunaParseError) as exc_info:
        parse_fortuna_line("1:0 @ 0.95", line_num=5)
    assert exc_info.value.line_num == 5
    assert "больше 1.0" in str(exc_info.value)


def test_calculate_fortuna_overround() -> None:
    # 1. Sparse items -> returns None
    sparse = parse_fortuna_text("1:0 @ 6.5; 2:1 @ 8.5; 0:0 @ 10.0")
    assert calculate_fortuna_overround(sparse) is None

    # 2. Incomplete grid (16 scores + other) with grid_max_goals=4 -> returns None
    grid16_text = """
    1:0 @ 7.00
    2:0 @ 10.00
    2:1 @ 9.00
    3:0 @ 20.00
    3:1 @ 18.00
    3:2 @ 28.00
    0:0 @ 11.00
    1:1 @ 7.00
    2:2 @ 14.00
    3:3 @ 45.00
    0:1 @ 9.50
    0:2 @ 16.00
    1:2 @ 12.00
    0:3 @ 35.00
    1:3 @ 28.00
    2:3 @ 32.00
    inny wynik @ 25.00
    """
    grid16_items = parse_fortuna_text(grid16_text)
    assert calculate_fortuna_overround(grid16_items, grid_max_goals=4) is None
    # But with grid_max_goals=3 (16 scores + other), it is complete
    margin16 = calculate_fortuna_overround(grid16_items, grid_max_goals=3)
    assert margin16 is not None
    assert 0.05 < margin16 < 0.35


def test_parse_real_fortuna_fixture_barcelona_real() -> None:
    """Test parsing complete real Fortuna copy-paste fixture."""
    from pathlib import Path

    fixture_path = Path("tests/fixtures/fortuna_barcelona_real_2026-10-25.txt")
    assert fixture_path.exists()
    content = fixture_path.read_text(encoding="utf-8")
    items = parse_fortuna_text(content)

    # 1. Exact scores: 25 scores + inny
    cs_items = [it for it in items if it.market in ("exact_score", "other")]
    assert len(cs_items) == 26
    margin = calculate_fortuna_overround(items, grid_max_goals=4)
    assert margin is not None
    # Real Fortuna margin on El Clasico exact score is ~32.7%
    assert 0.30 < margin < 0.35

    # 2. Multiwynik items: 11 outcomes
    multi_items = [it for it in items if it.market == "multiscore"]
    assert len(multi_items) == 11
    # Check Remis in multiwynik parsed as multi:remis
    remis_it = next(it for it in multi_items if it.selection == "multi:remis")
    assert remis_it.odds == pytest.approx(4.60)
    # Check other win phrases
    assert any(it.selection == "multi:home_other" for it in multi_items)
    assert any(it.selection == "multi:away_other" for it in multi_items)

    # 3. Totals items
    tot_items = [it for it in items if it.market == "totals"]
    assert len(tot_items) > 10
    tot_25_u = next(it for it in tot_items if it.selection == ("under", 2.5))
    tot_25_o = next(it for it in tot_items if it.selection == ("over", 2.5))
    assert tot_25_u.odds == pytest.approx(4.70)
    assert tot_25_o.odds == pytest.approx(1.21)

    # 4. Team totals
    tt_items = [it for it in items if it.market == "team_totals"]
    assert len(tt_items) > 0


def test_parse_real_fortuna_fixtures_udinese_and_nice() -> None:
    """Test parsing Udinese - Milan and Nice - Rennes real copy-paste fixtures."""
    from pathlib import Path

    for fname, exp_margin_range in [
        ("fortuna_udinese_milan_2026-10-25.txt", (0.30, 0.35)),
        ("fortuna_nice_rennes_2026-10-25.txt", (0.30, 0.35)),
    ]:
        p = Path(f"tests/fixtures/{fname}")
        assert p.exists()
        items = parse_fortuna_text(p.read_text(encoding="utf-8"))
        cs_items = [it for it in items if it.market in ("exact_score", "other")]
        assert len(cs_items) == 26
        margin = calculate_fortuna_overround(items, grid_max_goals=4)
        assert margin is not None
        assert exp_margin_range[0] < margin < exp_margin_range[1]

        # Multiwynik has 11 outcomes
        multi_items = [it for it in items if it.market == "multiscore"]
        assert len(multi_items) == 11
        assert any(it.selection == "multi:remis" for it in multi_items)


def test_glued_score_and_odds_raises_line_error() -> None:
    """Glued score and odds '2:19.50' must raise FortunaParseError, NOT be misparsed as 1X2."""
    with pytest.raises(FortunaParseError) as exc_info:
        parse_fortuna_text("1:0 15.00\n2:19.50\n0:0 12.00")
    assert exc_info.value.line_num == 2
    assert "Слипшийся счёт и коэффициент" in exc_info.value.reason


def test_score_on_one_line_and_odds_on_next() -> None:
    """Support score on line N and odds on line N+1 ('2:1\\n9.50')."""
    text = "2:1\n9.50\n1:0\n8.00\ninny\n15.00"
    items = parse_fortuna_text(text)
    assert len(items) == 3
    assert items[0].market == "exact_score"
    assert items[0].selection == (2, 1)
    assert items[0].odds == pytest.approx(9.50)
    assert items[1].selection == (1, 0)
    assert items[1].odds == pytest.approx(8.00)
    assert items[2].market == "other"
    assert items[2].odds == pytest.approx(15.00)

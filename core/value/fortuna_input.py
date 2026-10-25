"""Parser for manual Fortuna odds input (free text / Polish UI) (SPEC §5).

Supports:
- Exact score: '2:1 9.50', '2-1 @ 9,50', '2:1 - 9.5', '1–2 7.00' (en-dash normalized to ':')
- Grid bounds: exact scores are restricted to 0..4 (cs_grid_max_goals).
  Scores > 4 raise FortunaParseError.
- Other score: 'inny 4.60', 'inny wynik', 'pozostałe', 'other', 'любой другой'
- Multiscore / Multiwynik:
  - Score groups: '1:0, 2:0, 2:1 @ 3.40', '1:0 2:0 3:0 6.60'
  - Special Polish outcomes: 'Remis 4.60', 'Remis4.60' (all draws)
  - '1.drużyna wygra dowolnym innym wynikiem 9.20' (home other)
  - '2.drużyna wygra dowolnym innym wynikiem 45.00' (away other)
- Goals range: requires range keyword (gole/goli/gola/bramki/bramek/гола/голов/goals)
  e.g. '2-4 gole @ 1.80', '2–4 bramki 1,80'.
  Hyphens/dashes without keywords are parsed as exact scores.
- Totals (reference-only): 'over 2.5 1.95', 'mniej niż 2.5 4.70 więcej niż 2.5 1.21'
- Individual team totals: '- 0.5 8.40+ 0.5 1.04'
- Known headers ('Mecz: dokładny wynik', '2026-10-25 Barcelona - Real', etc.) skipped with logging
- Adjacent line support: outcome on one line, odds on the next line
- Overround calculation: exact score (requires full 25 scores + inny),
  multiwynik (11 groups), totals
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

FortunaMarketType = Literal[
    "exact_score", "other", "multiscore", "goals_range", "totals", "team_totals", "h2h"
]

ExactScoreSelection = tuple[int, int]
GoalsRangeSelection = tuple[int, int]
TotalsSelection = tuple[Literal["over", "under"], float]
TeamTotalsSelection = tuple[str, Literal["over", "under"], float]
H2HSelection = Literal["1", "X", "2"]
MultiscoreSpecial = Literal["multi:remis", "multi:home_other", "multi:away_other"]
MultiscoreSelection = frozenset[tuple[int, int]] | MultiscoreSpecial
FortunaSelection = (
    ExactScoreSelection
    | Literal["other"]
    | MultiscoreSelection
    | GoalsRangeSelection
    | TotalsSelection
    | TeamTotalsSelection
    | H2HSelection
)

RANGE_KEYWORDS = (
    "gole",
    "goli",
    "gola",
    "bramki",
    "bramek",
    "гола",
    "голов",
    "goals",
    "goal",
)


class FortunaParseError(ValueError):
    """Raised when manual input cannot be unambiguously parsed."""

    def __init__(self, line_num: int, line_text: str, reason: str) -> None:
        self.line_num = line_num
        self.line_text = line_text
        self.reason = reason
        super().__init__(f"Ошибка в строке {line_num} ('{line_text}'): {reason}")


@dataclass(frozen=True)
class FortunaPrice:
    """Parsed odds item entered from Fortuna."""

    market: FortunaMarketType
    selection: FortunaSelection
    odds: float
    raw_line: str
    line_number: int

    @property
    def selection_key(self) -> str:
        """Normalized ASCII unique key for the selection."""
        if self.market == "exact_score" and isinstance(self.selection, tuple):
            return f"score:{self.selection[0]}:{self.selection[1]}"
        if self.market == "other":
            return "score:other"
        if self.market == "multiscore":
            if isinstance(self.selection, frozenset):
                sorted_scores = sorted(self.selection)
                return "multi:" + ",".join(f"{h}:{a}" for h, a in sorted_scores)
            return str(self.selection)
        if (
            self.market == "goals_range"
            and isinstance(self.selection, tuple)
            and len(self.selection) == 2
        ):
            return f"range:{self.selection[0]}:{self.selection[1]}"
        if (
            self.market == "totals"
            and isinstance(self.selection, tuple)
            and len(self.selection) == 2
        ):
            return f"totals:{self.selection[0]}:{float(self.selection[1]):g}"
        if (
            self.market == "team_totals"
            and isinstance(self.selection, tuple)
            and len(self.selection) == 3
        ):
            line_str = f"{float(self.selection[2]):g}"
            return f"team_totals:{self.selection[0]}:{self.selection[1]}:{line_str}"
        if self.market == "h2h":
            return f"h2h:{self.selection}"
        return f"{self.market}:{self.selection}"


def _parse_odds_token(token: str, line_num: int, line_text: str) -> float:
    """Parse decimal odds token (supporting comma and dot decimals)."""
    cleaned = token.strip().replace(",", ".")
    try:
        val = float(cleaned)
    except ValueError as err:
        raise FortunaParseError(
            line_num, line_text, f"Некорректный формат коэффициента '{token}'"
        ) from err
    if val <= 1.0 or not math.isfinite(val):
        raise FortunaParseError(
            line_num, line_text, f"Коэффициент должен быть больше 1.0, получено {val}"
        )
    return val


def _clean_spacing_artifacts(line: str) -> str:
    """Clean web copy-paste digit and space concatenation artifacts."""
    s = line.strip()
    # 1. Glued 'Remis' and price e.g. 'Remis4.60' -> 'Remis 4.60'
    s = re.sub(r"(?i)\b(remis)(\d+([.,]\d+)?)\b", r"\1 \2", s)
    # 2. Number splitting artifact like '0:61 00.00' -> '0:6 100.00'
    s = re.sub(r"(\d+:\d+)(\d+)\s+(\d+([.,]\d+))", r"\1 \2\3", s)
    # 3. Glued team totals e.g. '8.40+ 0.5' -> '8.40 + 0.5' (odds has decimal point)
    s = re.sub(r"(\d+[.,]\d+)\s*([\+\-])\s*(\d+([.,]\d+)?)", r"\1 \2 \3", s)
    return s


def _is_known_header(text: str) -> bool:
    """Check if line is a known metadata / section header."""
    s = text.strip()
    if not s:
        return True
    if s.startswith("#"):
        return True
    # Match line: '2026-10-25 Barcelona - Real Madrid'
    if re.match(r"^\d{4}\s*[\-–—]\s*\d{2}\s*[\-–—]\s*\d{2}\b", s):
        return True
    # Section headers: 'Mecz: dokładny wynik', 'Mecz: multiwynik', etc.
    if s.lower().startswith("mecz:"):
        return True
    header_exact = (
        "dokładny wynik", "dokladny wynik", "exact score",
        "multiwynik", "multiscore",
        "liczba goli", "liczba bramek", "totals", "total goals",
    )
    return s.lower() in header_exact


def _try_parse_exact_score(text: str, max_goals: int = 4) -> tuple[int, int] | None:
    """Match exact score e.g. '2:1', '2-1', '1–2' (normalized). Raises error if > max_goals."""
    m = re.match(r"^(\d+)\s*[:\-–—]\s*(\d+)$", text.strip())
    if m:
        h, a = int(m.group(1)), int(m.group(2))
        return h, a
    return None


def _try_parse_other_score(text: str) -> bool:
    """Match phrases for 'other score' in Polish, Russian, English."""
    cleaned = text.strip().lower()
    other_phrases = (
        "inny wynik", "inny", "pozostałe", "pozostale", "other", "any other",
        "any other score", "любой другой", "другой счёт", "другой счет", "другой",
        "pozostałe wyniki", "pozostale wyniki",
    )
    return cleaned in other_phrases


def _try_parse_goals_range(text: str) -> tuple[int, int] | None:
    """Match goals range e.g. '2-4 gole', '2–4 bramki', '2-4 гола'.

    STRICT: Requires explicit range keyword (gole/bramki/etc.). Without keyword,
    hyphens/dashes are treated as exact scores.
    """
    cleaned = text.strip().lower()
    has_keyword = any(k in cleaned for k in RANGE_KEYWORDS)
    if not has_keyword:
        return None

    # Strip keyword
    stripped = re.sub(
        r"\s*(gole|goli|gola|bramki|bramek|гола|голов|goals?)\s*$", "", cleaned
    ).strip()
    m = re.match(r"^(\d+)\s*[\-–—\.]+\s*(\d+)$", stripped)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if 0 <= lo <= hi:
            return lo, hi
    return None


def _try_parse_multiscore(
    text: str,
    in_multiwynik_section: bool = False,
) -> MultiscoreSelection | None:
    """Match multiscore groups e.g. '1:0 2:0 3:0', 'Remis', or special other phrases."""
    cleaned = text.strip().lower()

    # Remis in multiwynik (all draws) - only if in multiwynik section
    if in_multiwynik_section and cleaned in ("remis", "draw", "ничья"):
        return "multi:remis"

    # Home / Away other phrases
    if "1.drużyna wygra dowolnym innym wynikiem" in cleaned or "1.druzyna" in cleaned:
        return "multi:home_other"
    if "2.drużyna wygra dowolnym innym wynikiem" in cleaned or "2.druzyna" in cleaned:
        return "multi:away_other"

    # Multiple scores separated by commas, slashes, or whitespace
    # E.g. '1:0, 2:0, 2:1' or '1:0 2:0 3:0'
    tokens = re.split(r"[,;/]+|\s+", cleaned)
    tokens = [t.strip() for t in tokens if t.strip()]
    if len(tokens) < 2:
        return None

    scores: list[tuple[int, int]] = []
    for tok in tokens:
        sc = _try_parse_exact_score(tok, max_goals=10)
        if sc is None:
            return None
        scores.append(sc)
    return frozenset(scores)


def _try_parse_totals(text: str) -> tuple[Literal["over", "under"], float] | None:
    """Match totals e.g. 'over 2.5', 'mniej niż 2.5', 'więcej niż 3'."""
    cleaned = text.strip().lower()

    # Polish: mniej niż / wiecej niz
    m_pl_under = re.match(r"^(mniej\s+ni[żz]|poni[żz]ej)\s*(\d+([.,]\d+)?)$", cleaned)
    if m_pl_under:
        return "under", float(m_pl_under.group(2).replace(",", "."))

    m_pl_over = re.match(r"^(wi[ęe]cej\s+ni[żz]|powy[żz]ej)\s*(\d+([.,]\d+)?)$", cleaned)
    if m_pl_over:
        return "over", float(m_pl_over.group(2).replace(",", "."))

    # Standard over/under
    m_over = re.match(r"^(over|больше|\+|o)\s*(\d+([.,]\d+)?)$", cleaned)
    if m_over:
        return "over", float(m_over.group(2).replace(",", "."))

    m_under = re.match(r"^(under|меньше|\-|u)\s*(\d+([.,]\d+)?)$", cleaned)
    if m_under:
        return "under", float(m_under.group(2).replace(",", "."))

    return None


def _try_parse_team_totals(text: str, current_team: str) -> TeamTotalsSelection | None:
    """Match team totals e.g. '- 0.5', '+ 1.5' under a team section."""
    cleaned = text.strip()
    m = re.match(r"^([\+\-])\s*(\d+([.,]\d+)?)$", cleaned)
    if m:
        sign = m.group(1)
        line_val = float(m.group(2).replace(",", "."))
        kind: Literal["over", "under"] = "over" if sign == "+" else "under"
        return current_team, kind, line_val
    return None


def _try_parse_h2h(text: str) -> Literal["1", "X", "2"] | None:
    """Match '1', 'X', '2'."""
    cleaned = text.strip().lower()
    if cleaned in ("1", "home", "gospodarze", "хозяева"):
        return "1"
    if cleaned in ("x", "draw", "remis", "ничья"):
        return "X"
    if cleaned in ("2", "away", "goście", "goscie", "гости"):
        return "2"
    return None


def parse_fortuna_line(
    line: str,
    line_num: int = 1,
    current_team: str | None = None,
    current_section: str | None = None,
    grid_max_goals: int = 4,
) -> FortunaPrice:
    """Parse a single outcome and odds line."""
    raw = _clean_spacing_artifacts(line)
    if not raw:
        raise FortunaParseError(line_num, line, "Пустая строка")

    # Check for glued score without separator e.g. '2:19.50'
    if re.search(r"\b\d+:\d+[.,]\d+\b", raw):
        raise FortunaParseError(
            line_num,
            line,
            f"Слипшийся счёт и коэффициент в '{raw}': "
            "отсутствует пробел между счётом и коэффициентом",
        )

    # Split into outcome and price
    # Look for trailing decimal odds
    # Pattern: space or delimiter followed by digits with decimal
    m = re.search(r"(\s*[@=:–—\-]\s*|\s+)(?=\d+([.,]\d+)?$)", raw)
    if not m:
        raise FortunaParseError(
            line_num, line, "Не удалось выделить коэффициент из строки"
        )

    outcome_str = raw[:m.start()].strip()
    price_str = raw[m.end():].strip()

    if not outcome_str or not price_str:
        raise FortunaParseError(
            line_num, line, "Не удалось выделить исход и коэффициент"
        )

    odds = _parse_odds_token(price_str, line_num, line)

    # 1. Other score
    if _try_parse_other_score(outcome_str):
        return FortunaPrice("other", "other", odds, line, line_num)

    # 2. Goals range (requires keyword)
    grange = _try_parse_goals_range(outcome_str)
    if grange is not None:
        return FortunaPrice("goals_range", grange, odds, line, line_num)

    # 3. Exact score (dashes without keywords are exact scores)
    exact = _try_parse_exact_score(outcome_str, max_goals=10)
    if exact is not None:
        h, a = exact
        if h > grid_max_goals or a > grid_max_goals:
            raise FortunaParseError(
                line_num,
                line,
                f"Счёт {h}:{a} вне стандартной сетки Fortuna 0..{grid_max_goals} "
                "(используйте 'inny')",
            )
        return FortunaPrice("exact_score", exact, odds, line, line_num)

    # 4. Multiwynik / Multiscore
    in_mw = current_section == "multiwynik"
    multi = _try_parse_multiscore(outcome_str, in_multiwynik_section=in_mw)
    if multi is not None:
        return FortunaPrice("multiscore", multi, odds, line, line_num)

    # 5. Team totals (under team header)
    if current_team:
        tt = _try_parse_team_totals(outcome_str, current_team)
        if tt is not None:
            return FortunaPrice("team_totals", tt, odds, line, line_num)

    # 6. Regular totals
    tot = _try_parse_totals(outcome_str)
    if tot is not None:
        return FortunaPrice("totals", tot, odds, line, line_num)

    # 7. H2H
    h2h_sel = _try_parse_h2h(outcome_str)
    if h2h_sel is not None:
        return FortunaPrice("h2h", h2h_sel, odds, line, line_num)

    raise FortunaParseError(
        line_num, line, f"Не удалось распознать рынок или исход: '{outcome_str}'"
    )


def _split_multi_outcome_line(line: str) -> list[str]:
    """Split a single line containing multiple outcomes into individual item strings."""
    cleaned = _clean_spacing_artifacts(line)

    # Case 1: Semicolon separation
    if ";" in cleaned:
        return [s.strip() for s in cleaned.split(";") if s.strip()]

    # Case 2: Total goals line: 'mniej niż X.X Y.YY więcej niż X.X Y.YY'
    # Or 'mniej niz ... wiecej niz ...'
    m_split = re.search(r"\s+(?=(?:wi[ęe]cej\s+ni[żz]|powy[żz]ej)\b)", cleaned, re.IGNORECASE)
    if m_split:
        part1 = cleaned[:m_split.start()].strip()
        part2 = cleaned[m_split.start():].strip()
        return [part1, part2]

    # Case 3: Team totals: '- 0.5 8.40 + 0.5 1.04'
    m_tt = re.search(r"(\d+([.,]\d+)?)\s+(?=\+\s*\d)", cleaned)
    if m_tt:
        split_pt = m_tt.end()
        part1 = cleaned[:split_pt].strip()
        part2 = cleaned[split_pt:].strip()
        return [part1, part2]

    return [cleaned]


def parse_fortuna_text(
    text: str,
    grid_max_goals: int = 4,
) -> list[FortunaPrice]:
    """Parse multi-line Fortuna odds text with robust support for real copy-paste layouts.

    Handles:
    - Headers skipping with logging ('Mecz:', match dates, section names);
    - Multi-outcome lines ('mniej niż 1.5 11.00 więcej niż 1.5 1.06');
    - Spacing and concatenation artifacts ('Remis4.60', '0:4 0:5 0:61 00.00');
    - Adjacent lines where outcome and odds are on separate lines;
    - Semicolon separated lines;
    - Conflict errors on duplicated outcomes with different odds;
    - Strict line number reporting on unparseable lines.
    """
    raw_lines = text.splitlines()
    if not raw_lines:
        return []

    # Phase A: Handle adjacent lines (outcome on line i, number on line i+1)
    merged_lines: list[tuple[int, str]] = []
    idx = 0
    while idx < len(raw_lines):
        line_num = idx + 1
        line_str = raw_lines[idx].strip()
        idx += 1

        if not line_str:
            continue

        # Check if line_str has no price, and next line is solely a decimal price
        if idx < len(raw_lines):
            next_str = raw_lines[idx].strip()
            is_exact_score_only = bool(re.match(r"^\d+\s*[:\-]\s*\d+$", line_str))
            has_separated_price = bool(re.search(r"(\s+|[@=–—\-])\d+([.,]\d+)?$", line_str))
            next_is_price = bool(re.match(r"^\d+([.,]\d+)?$", next_str))
            if (
                not _is_known_header(line_str)
                and next_is_price
                and (is_exact_score_only or not has_separated_price)
            ):
                merged_lines.append((line_num, f"{line_str} {next_str}"))
                idx += 1
                continue

        merged_lines.append((line_num, line_str))

    # Phase B: Parse lines with section tracking
    results: list[FortunaPrice] = []
    seen: dict[str, FortunaPrice] = {}
    current_team: str | None = None
    current_section: str | None = None

    for line_num, line_str in merged_lines:
        cleaned = _clean_spacing_artifacts(line_str)
        if not cleaned:
            continue

        # Header check & team context tracking
        if _is_known_header(cleaned):
            logger.debug("Skipping header at line %d: %s", line_num, cleaned)
            hdr_low = cleaned.lower()
            if "multiwynik" in hdr_low or "multiscore" in hdr_low:
                current_section = "multiwynik"
            elif any(
                k in hdr_low
                for k in ("dokładny wynik", "dokladny wynik", "exact score")
            ):
                current_section = "exact_score"
            elif "liczba goli" in hdr_low or "totals" in hdr_low:
                current_section = "totals"
            elif "1x2" in hdr_low or "wynik meczu" in hdr_low:
                current_section = "h2h"

            # Check for team total headers e.g. 'Mecz: Barcelona - liczba goli'
            m_team_tot = re.search(r"Mecz:\s*([^\-]+)-\s*liczba goli", cleaned, re.IGNORECASE)
            if m_team_tot:
                current_team = m_team_tot.group(1).strip()
                current_section = "team_totals"
            elif cleaned.lower().startswith("mecz:"):
                current_team = None
            continue

        # Split multi-outcome lines
        sub_items = _split_multi_outcome_line(cleaned)
        for sub in sub_items:
            item = parse_fortuna_line(
                sub,
                line_num=line_num,
                current_team=current_team,
                current_section=current_section,
                grid_max_goals=grid_max_goals,
            )
            key = item.selection_key
            if key in seen:
                prev = seen[key]
                if abs(prev.odds - item.odds) > 1e-4:
                    raise FortunaParseError(
                        line_num,
                        sub,
                        f"Конфликт коэффициентов для исхода '{key}': ранее было {prev.odds} "
                        f"(строка {prev.line_number}), получено {item.odds}",
                    )
                continue
            seen[key] = item
            results.append(item)

    return results


def calculate_fortuna_overround(
    prices: Sequence[FortunaPrice],
    grid_max_goals: int = 4,
) -> float | None:
    """Calculate Fortuna overround (margin) on exact score market.

    Only evaluates when the full grid is present (all (grid_max_goals+1)^2 scores + inny).
    For standard grid_max_goals=4, this requires exactly 25 scores + inny.
    Returns margin as float (e.g. 0.327 for 32.7%), or None if grid is incomplete.
    """
    exact_dict: dict[tuple[int, int], float] = {}
    other_odds: float | None = None

    for p in prices:
        if p.market == "exact_score" and isinstance(p.selection, tuple) and len(p.selection) == 2:
            exact_dict[(int(p.selection[0]), int(p.selection[1]))] = p.odds
        elif p.market == "other":
            other_odds = p.odds

    expected_scores = {
        (h, a) for h in range(grid_max_goals + 1) for a in range(grid_max_goals + 1)
    }
    if other_odds is None or not expected_scores.issubset(exact_dict.keys()):
        return None

    inv_sum = sum(1.0 / exact_dict[sc] for sc in expected_scores) + (1.0 / other_odds)
    return float(inv_sum - 1.0)


def calculate_section_overrounds(
    prices: Sequence[FortunaPrice],
    grid_max_goals: int = 4,
) -> dict[str, float]:
    """Calculate overrounds across parsed Fortuna sections."""
    res: dict[str, float] = {}

    # 1. Exact score
    cs_margin = calculate_fortuna_overround(prices, grid_max_goals=grid_max_goals)
    if cs_margin is not None:
        res["exact_score"] = cs_margin

    # 2. Multiwynik (11 groups)
    multi_prices = [p for p in prices if p.market == "multiscore"]
    if len(multi_prices) == 11:
        res["multiwynik"] = float(sum(1.0 / p.odds for p in multi_prices) - 1.0)

    # 3. Totals by line (over + under)
    totals_by_line: dict[float, dict[str, float]] = {}
    for p in prices:
        if p.market == "totals" and isinstance(p.selection, tuple) and len(p.selection) == 2:
            kind, line_val = str(p.selection[0]), float(p.selection[1])
            totals_by_line.setdefault(line_val, {})[kind] = p.odds

    for line_val, sub in totals_by_line.items():
        if "over" in sub and "under" in sub:
            margin = (1.0 / sub["over"] + 1.0 / sub["under"]) - 1.0
            res[f"totals_{line_val:g}"] = float(margin)

    # 4. Team totals by line
    tt_by_key: dict[tuple[str, float], dict[str, float]] = {}
    for p in prices:
        if p.market == "team_totals" and isinstance(p.selection, tuple) and len(p.selection) == 3:
            team_s, kind, line_val = str(p.selection[0]), str(p.selection[1]), float(p.selection[2])
            tt_by_key.setdefault((team_s, line_val), {})[kind] = p.odds

    for (team_s, line_val), sub in tt_by_key.items():
        if "over" in sub and "under" in sub:
            margin = (1.0 / sub["over"] + 1.0 / sub["under"]) - 1.0
            res[f"team_totals_{team_s}_{line_val:g}"] = float(margin)

    return res

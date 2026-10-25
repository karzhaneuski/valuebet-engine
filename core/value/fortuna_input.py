"""Parser for manual Fortuna odds input (free text / Polish UI) (SPEC §5).

Supports:
- Exact score: '2:1 9.50', '2-1 @ 9,50', '2:1 - 9.5'
- Any other score: 'inny wynik', 'pozostałe', 'other', 'любой другой'
- Multiscore: '1:0, 2:0, 2:1 @ 3.40', '1:0 / 2:0 / 2:1 3.40'
- Goals range: '2-4 gole @ 1.80', '2–4 bramki 1,80', '2-4 гола 1.80'
- Reference-only totals and 1X2: 'over 2.5 1.95', 'under 2.5 1.85', '1 @ 2.10', 'X @ 3.40'
- Explicit line-numbered error reporting on ambiguous or invalid lines (no silent drops)
- Duplicate detection with conflict errors
- Fortuna overround calculation on exact score grids
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

FortunaMarketType = Literal["exact_score", "other", "multiscore", "goals_range", "totals", "h2h"]

# Selection representation types
ExactScoreSelection = tuple[int, int]
GoalsRangeSelection = tuple[int, int]
TotalsSelection = tuple[Literal["over", "under"], float]
H2HSelection = Literal["1", "X", "2"]
MultiscoreSelection = frozenset[tuple[int, int]]
FortunaSelection = (
    ExactScoreSelection
    | Literal["other"]
    | MultiscoreSelection
    | GoalsRangeSelection
    | TotalsSelection
    | H2HSelection
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
        """Normalized unique key for the selection."""
        if self.market == "exact_score" and isinstance(self.selection, tuple):
            return f"score:{self.selection[0]}:{self.selection[1]}"
        if self.market == "other":
            return "score:other"
        if self.market == "multiscore" and isinstance(self.selection, frozenset):
            sorted_scores = sorted(self.selection)
            return "multi:" + ",".join(f"{h}:{a}" for h, a in sorted_scores)
        if self.market == "goals_range" and isinstance(self.selection, tuple):
            return f"range:{self.selection[0]}:{self.selection[1]}"
        if self.market == "totals" and isinstance(self.selection, tuple):
            return f"totals:{self.selection[0]}:{self.selection[1]}"
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


def _split_outcome_and_price(line: str) -> tuple[str, str]:
    """Split line into outcome text and price string.

    Recognizes delimiters: @, =, :, -, –, — or trailing space before decimal odds.
    """
    raw = line.strip()

    # 1. Explicit delimiter before trailing decimal odds: '@', '=', ':', '-', '–', '—'
    m_explicit = re.search(r"(\s*[@=]\s*|\s+[:\-–—]\s*|[:\-–—]\s*)(?=\d+([.,]\d+)?$)", raw)
    if m_explicit:
        idx = m_explicit.start()
        outcome = raw[:idx].strip()
        price = raw[m_explicit.end():].strip()
        if outcome and price:
            return outcome, price

    # 2. Trailing price separated by whitespace: '2:1 9.50', '2–4 bramki 1,80', 'inny wynik 45.0'
    space_match = re.search(r"\s+(\d+([.,]\d+)?)$", raw)
    if space_match:
        return raw[:space_match.start()].strip(), space_match.group(1).strip()

    raise ValueError(f"Не удалось выделить коэффициент из строки '{raw}'")


def _try_parse_exact_score(text: str) -> tuple[int, int] | None:
    """Match '2:1' or '2-1'."""
    m = re.match(r"^(\d+)\s*[:\-]\s*(\d+)$", text.strip())
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _try_parse_other_score(text: str) -> bool:
    """Match phrases for 'other score' in Polish, Russian, English."""
    cleaned = text.strip().lower()
    other_phrases = (
        "inny wynik", "inny", "pozostałe", "pozostale", "other", "any other",
        "any other score", "любой другой", "другой счёт", "другой счет", "другой",
    )
    return cleaned in other_phrases


def _try_parse_multiscore(text: str) -> frozenset[tuple[int, int]] | None:
    """Match comma/slash separated scores e.g. '1:0, 2:0, 2:1' or '1:0 / 2:0 / 2:1'."""
    tokens = re.split(r"[,;/]+", text.strip())
    if len(tokens) < 2:
        return None
    scores: list[tuple[int, int]] = []
    for tok in tokens:
        sc = _try_parse_exact_score(tok.strip())
        if sc is None:
            return None
        scores.append(sc)
    return frozenset(scores)


def _try_parse_goals_range(text: str) -> tuple[int, int] | None:
    """Match '2-4 gole', '2–4 bramki', '2-4 гола', '0-1 goli', '2–4' (with en-dash/keyword)."""
    cleaned = text.strip().lower()
    has_keyword = any(k in cleaned for k in ("gol", "goal", "bram", "гол"))
    has_dash_range = "–" in cleaned or "—" in cleaned or ".." in cleaned
    if not (has_keyword or has_dash_range):
        return None
    cleaned = re.sub(r"\s*(gole|goli|bramki|bramek|гола|голов|goals?)\s*$", "", cleaned).strip()
    m = re.match(r"^(\d+)\s*[\-–—\.]+\s*(\d+)$", cleaned)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if 0 <= lo <= hi:
            return lo, hi
    return None


def _try_parse_totals(text: str) -> tuple[Literal["over", "under"], float] | None:
    """Match 'over 2.5', 'powyżej 2.5', 'больше 2.5', 'under 2.5', 'poniżej 2.5'."""
    cleaned = text.strip().lower()
    # Over variations
    m_over = re.match(
        r"^(over|powyżej|powyzej|больше|\+|o)\s*(\d+([.,]\d+)?)$", cleaned
    )
    if m_over:
        line_val = float(m_over.group(2).replace(",", "."))
        return "over", line_val

    # Under variations
    m_under = re.match(
        r"^(under|poniżej|ponizej|меньше|\-|u)\s*(\d+([.,]\d+)?)$", cleaned
    )
    if m_under:
        line_val = float(m_under.group(2).replace(",", "."))
        return "under", line_val

    return None


def _try_parse_h2h(text: str) -> Literal["1", "X", "2"] | None:
    """Match '1', 'X', '2', 'remis'."""
    cleaned = text.strip().lower()
    if cleaned in ("1", "home", "gospodarze", "хозяева"):
        return "1"
    if cleaned in ("x", "draw", "remis", "ничья"):
        return "X"
    if cleaned in ("2", "away", "goście", "goscie", "гости"):
        return "2"
    return None


def parse_fortuna_line(line: str, line_num: int = 1) -> FortunaPrice:
    """Parse a single line of Fortuna odds text."""
    raw = line.strip()
    if not raw:
        raise FortunaParseError(line_num, raw, "Пустая строка")

    try:
        outcome_str, price_str = _split_outcome_and_price(raw)
    except Exception as err:
        raise FortunaParseError(
            line_num, raw, f"Не удалось выделить исход и коэффициент: {err}"
        ) from err

    odds = _parse_odds_token(price_str, line_num, raw)

    # 1. Other score
    if _try_parse_other_score(outcome_str):
        return FortunaPrice(
            market="other",
            selection="other",
            odds=odds,
            raw_line=raw,
            line_number=line_num,
        )

    # 2. Goals range (with keywords or en-dash)
    grange = _try_parse_goals_range(outcome_str)
    if grange is not None:
        return FortunaPrice(
            market="goals_range",
            selection=grange,
            odds=odds,
            raw_line=raw,
            line_number=line_num,
        )

    # 3. Exact score
    exact = _try_parse_exact_score(outcome_str)
    if exact is not None:
        return FortunaPrice(
            market="exact_score",
            selection=exact,
            odds=odds,
            raw_line=raw,
            line_number=line_num,
        )

    # 4. Multiscore
    multi = _try_parse_multiscore(outcome_str)
    if multi is not None:
        return FortunaPrice(
            market="multiscore",
            selection=multi,
            odds=odds,
            raw_line=raw,
            line_number=line_num,
        )

    # 5. Totals (reference-only)
    tot = _try_parse_totals(outcome_str)
    if tot is not None:
        return FortunaPrice(
            market="totals",
            selection=tot,
            odds=odds,
            raw_line=raw,
            line_number=line_num,
        )

    # 6. H2H 1X2 (reference-only)
    h2h_sel = _try_parse_h2h(outcome_str)
    if h2h_sel is not None:
        return FortunaPrice(
            market="h2h",
            selection=h2h_sel,
            odds=odds,
            raw_line=raw,
            line_number=line_num,
        )

    raise FortunaParseError(
        line_num, raw, f"Не удалось распознать рынок или исход: '{outcome_str}'"
    )


def parse_fortuna_text(text: str) -> list[FortunaPrice]:
    """Parse multi-line or delimited text with Fortuna odds into FortunaPrice items.

    Validates:
    - No empty non-whitespace lines;
    - Each line unambiguously parses into a supported market;
    - No conflicting prices for the same outcome (raises FortunaParseError);
    - Idempotent: identical odds on the same outcome are deduplicated.
    """
    raw_lines = text.splitlines()
    if not raw_lines:
        return []

    # If the user pasted a single line with items separated by semicolons
    processed_lines: list[tuple[int, str]] = []
    for line_idx, line in enumerate(raw_lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Check if line has multiple items separated by semicolons
        if ";" in stripped and not _try_parse_multiscore(stripped):
            sub_items = [s.strip() for s in stripped.split(";") if s.strip()]
            for sub in sub_items:
                processed_lines.append((line_idx, sub))
        else:
            processed_lines.append((line_idx, stripped))

    results: list[FortunaPrice] = []
    seen: dict[str, FortunaPrice] = {}

    for line_num, item_text in processed_lines:
        item = parse_fortuna_line(item_text, line_num=line_num)
        key = item.selection_key
        if key in seen:
            prev = seen[key]
            if abs(prev.odds - item.odds) > 1e-4:
                raise FortunaParseError(
                    line_num,
                    item_text,
                    f"Конфликт коэффициентов для исхода '{key}': ранее было {prev.odds} "
                    f"(строка {prev.line_number}), получено {item.odds}",
                )
            # Identical duplicate -> skip duplicate entry
            continue
        seen[key] = item
        results.append(item)

    return results


def calculate_fortuna_overround(prices: list[FortunaPrice]) -> float | None:
    """Calculate Fortuna bookmaker overround (margin) on exact score market.

    Returns margin as float (e.g. 0.154 for 15.4%), or None if exact score grid
    is too sparse to form a complete market (< 10 quoted scores).
    """
    exact_prices = [p.odds for p in prices if p.market in ("exact_score", "other")]
    if len(exact_prices) < 10:
        return None
    inv_sum = sum(1.0 / o for o in exact_prices)
    return inv_sum - 1.0

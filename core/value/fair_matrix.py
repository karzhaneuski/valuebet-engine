"""Fair market matrix construction from market odds (SPEC §4, decisions B6.1).

Builds the 11x11 score probability distribution entirely from devigged market odds:
1. lambda_total derived from devigged O/U 2.5 (or nearest available half line);
2. Home goal share s fitted by matching devigged 1X2 via fit_1x2_share;
3. Low-score correlation parameter rho fixed to 0.0 (B6.1);
4. High-draw inflation kappa_high (0.20) applied to diagonal cells >= 2:2 with renormalization.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal, Protocol

import numpy as np
import numpy.typing as npt
from scipy.optimize import brentq

from core.config import AppConfig, BooksConfig, FairMatrixConfig, load_config
from core.model.markets import (
    _check_matrix,
    _check_score,
    market_lambda_from_totals,
    proxy_score_matrix,
)
from core.model.markets import btts as calc_btts
from core.model.markets import exact_score as calc_exact_score
from core.model.markets import goals_range as calc_goals_range
from core.model.markets import multiscore_set as calc_multiscore_set
from core.model.markets import total_over as calc_total_over
from core.model.markets import total_under as calc_total_under

MarketSource = Literal["toa_snapshot", "fd_prematch", "fd_closing", "manual", "synthetic"]
MatrixStatus = Literal["ok", "no_market", "fallback"]


class ScoreMatrixModel(Protocol):
    """Protocol for fallback Dixon-Coles model."""

    def score_matrix(self, home_team: str, away_team: str) -> npt.NDArray[np.float64]:
        ...


@dataclass(frozen=True)
class MarketOdds1X2:
    """Decimal odds for match outcome 1X2."""

    home: float
    draw: float
    away: float


@dataclass(frozen=True)
class MarketTotalsLine:
    """Decimal odds for a totals half line (e.g. 2.5 Over/Under)."""

    line: float
    over: float
    under: float


@dataclass(frozen=True)
class MarketInputs:
    """Normalized container for pre-match market odds independent of source."""

    source: MarketSource
    snapshot_time: dt.datetime | None = None
    totals_by_book: dict[str, list[MarketTotalsLine]] = field(default_factory=dict)
    h2h_by_book: dict[str, MarketOdds1X2] = field(default_factory=dict)
    match_name: str | None = None

    def compute_hash(self) -> str:
        """Deterministic SHA-256 hash of all input odds for full traceability."""
        payload: dict[str, object] = {
            "source": self.source,
            "snapshot_time": self.snapshot_time.isoformat() if self.snapshot_time else None,
            "match_name": self.match_name,
            "totals": {
                b: [
                    {"line": t.line, "over": t.over, "under": t.under}
                    for t in sorted(lines, key=lambda x: x.line)
                ]
                for b, lines in sorted(self.totals_by_book.items())
            },
            "h2h": {
                b: {"home": h.home, "draw": h.draw, "away": h.away}
                for b, h in sorted(self.h2h_by_book.items())
            },
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class FairMatrix:
    """11x11 exact score probability distribution with metadata."""

    matrix: npt.NDArray[np.float64]
    lambda_home: float
    lambda_away: float
    lambda_total: float
    home_share: float
    line_used: float
    source: str
    status: MatrixStatus
    books_used: list[str]
    snapshot_time: dt.datetime | None
    input_hash: str
    p_over_devig: float
    p_home_devig: float
    p_draw_devig: float
    p_away_devig: float
    rho: float
    kappa_high: float

    def exact_score(self, h: int, a: int) -> float:
        """P(home h, away a)."""
        _check_matrix(self.matrix)
        _check_score(self.matrix, h, a)
        return calc_exact_score(self.matrix, h, a)

    def multiscore(self, scores: Iterable[tuple[int, int]]) -> float:
        """P(score in scores)."""
        _check_matrix(self.matrix)
        return calc_multiscore_set(self.matrix, scores)

    def goals_range(self, lo: int, hi: int) -> float:
        """P(lo <= home goals + away goals <= hi)."""
        _check_matrix(self.matrix)
        return calc_goals_range(self.matrix, lo, hi)

    def total_over(self, line: float) -> float:
        """P(total goals > line)."""
        _check_matrix(self.matrix)
        return calc_total_over(self.matrix, line)

    def total_under(self, line: float) -> float:
        """P(total goals < line)."""
        _check_matrix(self.matrix)
        return calc_total_under(self.matrix, line)

    def btts(self, yes: bool = True) -> float:
        """P(both teams to score)."""
        _check_matrix(self.matrix)
        return calc_btts(self.matrix, yes=yes)


def devig_totals(over: float, under: float, method: str = "proportional") -> tuple[float, float]:
    """Devig over/under prices using proportional method (implied probability normalization)."""
    if method != "proportional":
        raise ValueError(f"Unsupported devig method: {method}")
    if over <= 1.0 or under <= 1.0 or not math.isfinite(over) or not math.isfinite(under):
        raise ValueError(f"Odds must be finite and > 1.0, got over={over}, under={under}")
    inv_o, inv_u = 1.0 / over, 1.0 / under
    s = inv_o + inv_u
    return inv_o / s, inv_u / s


def devig_1x2(
    home: float,
    draw: float,
    away: float,
    method: str = "proportional",
) -> tuple[float, float, float]:
    """Devig 1X2 prices using proportional method (implied probability normalization)."""
    if method != "proportional":
        raise ValueError(f"Unsupported devig method: {method}")
    for val, name in ((home, "home"), (draw, "draw"), (away, "away")):
        if val <= 1.0 or not math.isfinite(val):
            raise ValueError(f"Odds must be finite and > 1.0, got {name}={val}")
    inv_h, inv_d, inv_a = 1.0 / home, 1.0 / draw, 1.0 / away
    s = inv_h + inv_d + inv_a
    return inv_h / s, inv_d / s, inv_a / s


def is_half_line(line: float) -> bool:
    """Check if line is a valid half-line x.5 (2*line is positive odd integer)."""
    doubled = line * 2.0
    return (
        line > 0.0
        and math.isfinite(line)
        and abs(doubled - round(doubled)) < 1e-9
        and round(doubled) % 2 == 1
    )


def fit_1x2_share(
    lam_total: float,
    ph: float,
    pa: float,
    rho: float = 0.0,
    s_fallback: float = 0.5,
) -> float:
    """Solve for home goal share s such that P(H)/(P(H)+P(A)) = ph/(ph+pa)."""
    if ph <= 0.0 or pa <= 0.0:
        return s_fallback
    target = ph / (ph + pa)

    def f(s: float) -> float:
        m = proxy_score_matrix(lam_total, s, rho=rho)
        sh = float(np.tril(m, -1).sum())
        sa = float(np.triu(m, 1).sum())
        if sh + sa <= 0.0:
            return 0.0
        return (sh / (sh + sa)) - target

    try:
        return float(brentq(f, 0.01, 0.99, xtol=1e-6))
    except Exception:
        return s_fallback


def apply_draw_inflation(
    m: npt.NDArray[np.float64],
    kappa_high: float,
) -> npt.NDArray[np.float64]:
    """Apply high-draw inflation kappa_high to diagonal cells >= 2:2 and renormalize."""
    if kappa_high == 0.0:
        return m.copy()
    out = m.copy()
    n = min(out.shape)
    for i in range(2, n):
        out[i, i] *= 1.0 + kappa_high
    out /= out.sum()
    return out


def _select_bookmakers(
    inputs: MarketInputs,
    books_cfg: BooksConfig,
) -> tuple[str, list[MarketTotalsLine], MarketOdds1X2 | None, list[str]]:
    """Select preferred bookmaker (Pinnacle if present, otherwise allowed consensus)."""
    excluded = {b.lower() for b in books_cfg.excluded_books}
    t_books = {
        b.lower(): lines
        for b, lines in inputs.totals_by_book.items()
        if b.lower() not in excluded
    }
    h_books = {
        b.lower(): odds
        for b, odds in inputs.h2h_by_book.items()
        if b.lower() not in excluded
    }

    # 1. Prefer Pinnacle if available for both
    if "pinnacle" in t_books and "pinnacle" in h_books:
        valid_pinn_totals = [t for t in t_books["pinnacle"] if is_half_line(t.line)]
        if valid_pinn_totals:
            return "market_pinnacle", valid_pinn_totals, h_books["pinnacle"], ["pinnacle"]

    # 2. Check for explicit consensus (e.g. 'avg', 'consensus', 'consensus_of_allowed')
    consensus_markers = {"avg", "consensus", "consensus_of_allowed"}
    for cm in consensus_markers:
        if cm in t_books and cm in h_books:
            valid_totals = [t for t in t_books[cm] if is_half_line(t.line)]
            if valid_totals:
                return "market_consensus", valid_totals, h_books[cm], [cm]

    # 3. Aggregate allowed books to consensus
    common_books = sorted(set(t_books.keys()) & set(h_books.keys()))
    if not common_books:
        # Check if totals and h2h are available from different allowed books
        if not t_books or not h_books:
            return "no_market", [], None, []
        t_sources = sorted(t_books.keys())
        h_sources = sorted(h_books.keys())
        # Aggregate lines across t_sources
        line_map: dict[float, list[tuple[float, float]]] = {}
        for b in t_sources:
            for tl in t_books[b]:
                if is_half_line(tl.line):
                    line_map.setdefault(tl.line, []).append((tl.over, tl.under))
        if not line_map:
            return "no_market", [], None, []
        agg_totals = [
            MarketTotalsLine(
                line=line_val,
                over=float(np.mean([pair[0] for pair in pairs])),
                under=float(np.mean([pair[1] for pair in pairs])),
            )
            for line_val, pairs in line_map.items()
        ]
        # Aggregate 1X2 across h_sources
        agg_h2h = MarketOdds1X2(
            home=float(np.mean([h_books[b].home for b in h_sources])),
            draw=float(np.mean([h_books[b].draw for b in h_sources])),
            away=float(np.mean([h_books[b].away for b in h_sources])),
        )
        return "market_consensus", agg_totals, agg_h2h, sorted(set(t_sources + h_sources))

    # Multiple allowed books with both markets
    line_map_common: dict[float, list[tuple[float, float]]] = {}
    for b in common_books:
        for tl in t_books[b]:
            if is_half_line(tl.line):
                line_map_common.setdefault(tl.line, []).append((tl.over, tl.under))
    if not line_map_common:
        return "no_market", [], None, []

    agg_totals_common = [
        MarketTotalsLine(
            line=line_val,
            over=float(np.mean([pair[0] for pair in pairs])),
            under=float(np.mean([pair[1] for pair in pairs])),
        )
        for line_val, pairs in line_map_common.items()
    ]
    agg_h2h_common = MarketOdds1X2(
        home=float(np.mean([h_books[b].home for b in common_books])),
        draw=float(np.mean([h_books[b].draw for b in common_books])),
        away=float(np.mean([h_books[b].away for b in common_books])),
    )
    return "market_consensus", agg_totals_common, agg_h2h_common, common_books


def _select_totals_line(
    lines: list[MarketTotalsLine],
    preferred_line: float = 2.5,
) -> MarketTotalsLine | None:
    """Select closest available half-line to preferred_line (default 2.5)."""
    valid = [t for t in lines if is_half_line(t.line) and t.over > 1.0 and t.under > 1.0]
    if not valid:
        return None
    # Find exact match first
    for item in valid:
        if abs(item.line - preferred_line) < 1e-9:
            return item
    # Otherwise find nearest
    return min(valid, key=lambda x: abs(x.line - preferred_line))


def build_fair_matrix(
    inputs: MarketInputs,
    config: AppConfig | None = None,
    fair_cfg: FairMatrixConfig | None = None,
    books_cfg: BooksConfig | None = None,
    allow_model_fallback: bool = False,
    model: ScoreMatrixModel | None = None,
    home_team: str | None = None,
    away_team: str | None = None,
    devig_method: str = "proportional",
) -> FairMatrix:
    """Build fair 11x11 score probability matrix from market inputs or model fallback."""
    cfg = config or load_config()
    fm_cfg = fair_cfg or cfg.fair_matrix
    b_cfg = books_cfg or cfg.books
    input_hash = inputs.compute_hash()

    # 1. Select bookmakers and lines
    source_label, lines, h2h_odds, books_used = _select_bookmakers(inputs, b_cfg)

    # 2. Check if market is available
    if source_label == "no_market" or h2h_odds is None or not lines:
        if allow_model_fallback and model is not None and home_team and away_team:
            raw_m = model.score_matrix(home_team, away_team)
            _check_matrix(raw_m)
            # Normalize to 11x11
            m = np.zeros((11, 11), dtype=np.float64)
            nr, nc = min(11, raw_m.shape[0]), min(11, raw_m.shape[1])
            m[:nr, :nc] = raw_m[:nr, :nc]
            m /= m.sum()
            # Calculate implied lambdas
            h_idx = np.arange(11, dtype=np.float64)[:, None]
            a_idx = np.arange(11, dtype=np.float64)[None, :]
            lh = float((h_idx * m).sum())
            la = float((a_idx * m).sum())
            lt = lh + la
            sh = lh / lt if lt > 0.0 else 0.5
            return FairMatrix(
                matrix=m,
                lambda_home=lh,
                lambda_away=la,
                lambda_total=lt,
                home_share=sh,
                line_used=fm_cfg.default_totals_line,
                source="model_fallback",
                status="fallback",
                books_used=["model"],
                snapshot_time=inputs.snapshot_time,
                input_hash=input_hash,
                p_over_devig=calc_total_over(m, fm_cfg.default_totals_line),
                p_home_devig=float(np.tril(m, -1).sum()),
                p_draw_devig=float(np.diag(m).sum()),
                p_away_devig=float(np.triu(m, 1).sum()),
                rho=fm_cfg.rho,
                kappa_high=fm_cfg.kappa_high,
            )
        # Explicit no_market status
        empty = np.zeros((11, 11), dtype=np.float64)
        return FairMatrix(
            matrix=empty,
            lambda_home=0.0,
            lambda_away=0.0,
            lambda_total=0.0,
            home_share=0.5,
            line_used=fm_cfg.default_totals_line,
            source="no_market",
            status="no_market",
            books_used=[],
            snapshot_time=inputs.snapshot_time,
            input_hash=input_hash,
            p_over_devig=0.0,
            p_home_devig=0.0,
            p_draw_devig=0.0,
            p_away_devig=0.0,
            rho=fm_cfg.rho,
            kappa_high=fm_cfg.kappa_high,
        )

    # 3. Select totals line
    selected_line = _select_totals_line(lines, preferred_line=fm_cfg.default_totals_line)
    if selected_line is None:
        empty = np.zeros((11, 11), dtype=np.float64)
        return FairMatrix(
            matrix=empty,
            lambda_home=0.0,
            lambda_away=0.0,
            lambda_total=0.0,
            home_share=0.5,
            line_used=fm_cfg.default_totals_line,
            source="no_market",
            status="no_market",
            books_used=[],
            snapshot_time=inputs.snapshot_time,
            input_hash=input_hash,
            p_over_devig=0.0,
            p_home_devig=0.0,
            p_draw_devig=0.0,
            p_away_devig=0.0,
            rho=fm_cfg.rho,
            kappa_high=fm_cfg.kappa_high,
        )

    # 4. Devig O/U and 1X2
    p_over, _ = devig_totals(selected_line.over, selected_line.under, method=devig_method)
    p_h, p_d, p_a = devig_1x2(h2h_odds.home, h2h_odds.draw, h2h_odds.away, method=devig_method)

    # 5. Invert lambda_total from O/U
    lam_total = market_lambda_from_totals(p_over, selected_line.line)

    # 6. Fit home goal share from 1X2
    s_1x2 = fit_1x2_share(lam_total, p_h, p_a, rho=fm_cfg.rho, s_fallback=0.5)
    lam_h = lam_total * s_1x2
    lam_a = lam_total * (1.0 - s_1x2)

    # 7. Build proxy matrix with rho=0 and apply kappa_high to diagonal >= 2:2
    m_base = proxy_score_matrix(lam_total, s_1x2, max_score=10, rho=fm_cfg.rho)
    m = apply_draw_inflation(m_base, kappa_high=fm_cfg.kappa_high)

    # Enforce strict normalization
    m /= float(m.sum())
    assert np.isclose(float(m.sum()), 1.0, atol=1e-9)

    return FairMatrix(
        matrix=m,
        lambda_home=lam_h,
        lambda_away=lam_a,
        lambda_total=lam_total,
        home_share=s_1x2,
        line_used=selected_line.line,
        source=source_label,
        status="ok",
        books_used=books_used,
        snapshot_time=inputs.snapshot_time,
        input_hash=input_hash,
        p_over_devig=p_over,
        p_home_devig=p_h,
        p_draw_devig=p_d,
        p_away_devig=p_a,
        rho=fm_cfg.rho,
        kappa_high=fm_cfg.kappa_high,
    )

"""Value Engine for Fortuna odds evaluation against the fair market matrix (SPEC §5).

Computes edge, Kelly staking, risk flags, anomaly warnings, and decision recommendations.
Thresholds:
- Exact score: edge >= 12%, p_fair >= 4%, odds <= 21.0, edge <= 40% (implausible edge guard).
- Multiscore: edge >= 8%, p_fair >= 6%, odds <= 15.0, edge <= 25% (implausible edge guard).
- Goals range: edge >= 8%, p_fair >= 15%, odds <= 8.0, edge <= 25% (implausible edge guard).
- Inny (other score): reference-only (P(5+) has empirical bias z=-2.25 on validation).
  Requires full 25-score grid.
- Totals and 1X2: strictly reference-only (no bets).
- Fallback model: strictly reference-only.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from core.config import AppConfig, ThresholdsConfig, load_config
from core.value.fair_matrix import FairMatrix
from core.value.fortuna_input import (
    FortunaPrice,
    calculate_fortuna_overround,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvaluatedOutcome:
    """Evaluation result for a single Fortuna odds entry."""

    market: str
    selection_display: str
    selection_key: str
    odds_fortuna: float
    p_fair: float
    fair_odds: float
    edge: float
    recommended: bool
    rejection_reason: str
    stake_flat: float
    kelly_quarter_capped: float
    flags: list[str] = field(default_factory=list)
    raw_price: FortunaPrice | None = None


@dataclass
class EvaluationResult:
    """Batch evaluation result for all Fortuna prices of a match."""

    match_name: str
    snapshot_time: dt.datetime | None
    source: str
    books_used: list[str]
    input_hash: str
    fortuna_overround: float | None
    outcomes: list[EvaluatedOutcome]
    risk_flags: list[str] = field(default_factory=list)
    model_anomaly: bool = False
    is_excluded: bool = False
    recommendations_count: int = 0

    def format_table(self) -> str:
        """Format Russian summary table for CLI output."""
        sep_double = "=" * 94
        sep_single = "-" * 94

        lines: list[str] = [
            sep_double,
            f"Матч: {self.match_name}",
        ]

        # Market source header
        b_str = ", ".join(self.books_used) if self.books_used else "нет"
        snap_str = (
            self.snapshot_time.strftime("%Y-%m-%d %H:%M UTC")
            if self.snapshot_time is not None
            else "неизвестно (football-data)"
        )
        hash_short = self.input_hash[:16] + "..." if len(self.input_hash) > 16 else self.input_hash
        lines.append(f"Источник рынка: {self.source} ({b_str}) | Время снимка: {snap_str}")

        if self.fortuna_overround is not None:
            overround_pct = f"{self.fortuna_overround:.1%}"
        else:
            overround_pct = "не определена (неполная сетка)"
        lines.append(f"Маржа Fortuna: {overround_pct} | Хеш входа: {hash_short}")

        # Risk and anomaly banners
        if self.risk_flags:
            lines.append(sep_single)
            for rf in self.risk_flags:
                lines.append(f"  {rf}")

        lines.append(sep_single)
        header = (
            f"{'Исход':<22} {'Fortuna':>7} {'p_fair':>7} {'Справедл.':>9} "
            f"{'Edge':>8} {'Реком.':>7} {'¼ Келли':>8} {'Причина отказа'}"
        )
        lines.append(header)
        lines.append(sep_single)

        for out in self.outcomes:
            rec_str = "ДА" if out.recommended else "НЕТ"
            pf_str = f"{out.p_fair:.1%}" if out.p_fair > 0 else "—"
            fair_odds_str = (
                f"{out.fair_odds:.2f}"
                if out.fair_odds < 9000 and out.p_fair > 0
                else "—"
            )
            edge_str = f"{out.edge:+.1%}" if out.p_fair > 0 else "—"
            k_str = f"{out.kelly_quarter_capped:.1%}" if out.recommended else "—"
            reason = out.rejection_reason if not out.recommended else ""

            row = (
                f"{out.selection_display:<22} "
                f"{out.odds_fortuna:>7.2f} "
                f"{pf_str:>7} "
                f"{fair_odds_str:>9} "
                f"{edge_str:>8} "
                f"{rec_str:>7} "
                f"{k_str:>8} "
                f"  {reason}"
            )
            lines.append(row)

        lines.append(sep_single)
        lines.append(f"Итого рекомендаций: {self.recommendations_count}")
        lines.append(sep_double)
        return "\n".join(lines)


def calculate_kelly_quarter_capped(
    p_fair: float,
    odds: float,
    cap: float = 0.02,
) -> float:
    """Calculate 1/4 Kelly criterion stake fraction capped at bankroll limit (default 2%)."""
    if p_fair <= 0.0 or odds <= 1.0:
        return 0.0
    edge = p_fair * odds - 1.0
    if edge <= 0.0:
        return 0.0
    f_star = edge / (odds - 1.0)
    quarter = 0.25 * f_star
    return float(min(quarter, cap))


def check_model_anomaly(
    market_home_share: float,
    model_home_share: float | None,
    threshold: float = 0.15,
) -> tuple[bool, str]:
    """Check if model home share diverges from market share beyond threshold."""
    if model_home_share is None:
        return False, ""
    diff = abs(market_home_share - model_home_share)
    if diff > threshold:
        msg = (
            f"⚠️ Аномалия модели: расхождение доли голов {diff:.1%} "
            f"(рынок {market_home_share:.1%}, модель {model_home_share:.1%}) "
            f"> порога {threshold:.0%}. Проверь составы/данные!"
        )
        return True, msg
    return False, ""


def _format_selection_display(item: FortunaPrice) -> str:
    """Format human-friendly outcome display for CLI table."""
    if item.market == "exact_score" and isinstance(item.selection, tuple):
        return f"{item.selection[0]}:{item.selection[1]}"
    if item.market == "other":
        return "Любой другой счёт"
    if item.market == "multiscore":
        if isinstance(item.selection, (frozenset, set, list)):
            return ", ".join(f"{h}:{a}" for h, a in sorted(item.selection))
        if item.selection == "multi:remis":
            return "Remis (все ничьи)"
        if item.selection == "multi:home_other":
            return "1.команда другой счёт"
        if item.selection == "multi:away_other":
            return "2.команда другой счёт"
        return str(item.selection)
    if (
        item.market == "goals_range"
        and isinstance(item.selection, tuple)
        and len(item.selection) == 2
    ):
        return f"{item.selection[0]}-{item.selection[1]} гола"
    if (
        item.market == "totals"
        and isinstance(item.selection, tuple)
        and len(item.selection) == 2
    ):
        lbl = "больше" if item.selection[0] == "over" else "меньше"
        return f"Тотал {lbl} {float(item.selection[1]):g}"
    if (
        item.market == "team_totals"
        and isinstance(item.selection, tuple)
        and len(item.selection) == 3
    ):
        lbl = "больше" if item.selection[1] == "over" else "меньше"
        return f"{item.selection[0]} {lbl} {float(item.selection[2]):g}"
    if item.market == "h2h":
        return f"Исход {item.selection}"
    return str(item.selection)


def evaluate_fortuna_prices(
    fair_matrix: FairMatrix,
    fortuna_prices: Sequence[FortunaPrice],
    match_name: str = "Unknown Match",
    config: AppConfig | None = None,
    thresholds_config: ThresholdsConfig | None = None,
    model_home_share: float | None = None,
    key_absences_home: list[str] | None = None,
    key_absences_away: list[str] | None = None,
    sources_conflict: bool = False,
    market_snapshot_time: dt.datetime | None = None,
    absence_news_time: dt.datetime | None = None,
) -> EvaluationResult:
    """Evaluate manual Fortuna prices against the fair market matrix."""
    cfg = config or load_config()
    thresh = thresholds_config or cfg.thresholds
    risk_cfg = cfg.risk_flags
    fm_cfg = cfg.fair_matrix
    fortuna_cfg = cfg.fortuna

    # Calculate exact score grid margin if full grid is present
    overround = calculate_fortuna_overround(
        fortuna_prices, grid_max_goals=fortuna_cfg.cs_grid_max_goals
    )

    flags: list[str] = []
    abs_h = key_absences_home or []
    abs_a = key_absences_away or []

    # Check >= 2 key absences per team -> total exclusion
    max_abs = risk_cfg.max_key_absences_per_team
    is_excluded = (len(abs_h) >= max_abs) or (len(abs_a) >= max_abs)

    if len(abs_h) >= max_abs:
        flags.append(
            f"⛔ Хозяева: отсутствуют {len(abs_h)} ключевых игрока "
            f"({', '.join(abs_h)}) -> ставки исключены"
        )
    if len(abs_a) >= max_abs:
        flags.append(
            f"⛔ Гости: отсутствуют {len(abs_a)} ключевых игрока "
            f"({', '.join(abs_a)}) -> ставки исключены"
        )

    # Check 1 key absence
    has_risk_multiplier = False
    if not is_excluded:
        if len(abs_h) == 1:
            flags.append(
                f"⚠️ Хозяева: отсутствует ключевой игрок {abs_h[0]} "
                f"(порог edge × {risk_cfg.absence_edge_multiplier})"
            )
            has_risk_multiplier = True
        if len(abs_a) == 1:
            flags.append(
                f"⚠️ Гости: отсутствует ключевой игрок {abs_a[0]} "
                f"(порог edge × {risk_cfg.absence_edge_multiplier})"
            )
            has_risk_multiplier = True

    # Sources conflict flag
    if sources_conflict:
        flags.append(
            f"⚠️ Противоречие источников информации по составам "
            f"(порог edge × {risk_cfg.absence_edge_multiplier})"
        )
        has_risk_multiplier = True

    # Freshness check: if absence news is newer than market snapshot
    if absence_news_time and market_snapshot_time and absence_news_time > market_snapshot_time:
        flags.append(
            "⚠️ Снимок рынка устарел относительно новости о составах: "
            "требуется обновление снимка или исключение"
        )

    # Model anomaly check
    is_anomaly, anomaly_msg = check_model_anomaly(
        fair_matrix.home_share,
        model_home_share,
        threshold=fm_cfg.anomaly_share_threshold,
    )
    if is_anomaly:
        flags.append(anomaly_msg)

    # Map of all exact scores in batch
    quoted_scores: set[tuple[int, int]] = set()
    for fp in fortuna_prices:
        if (
            fp.market == "exact_score"
            and isinstance(fp.selection, tuple)
            and isinstance(fp.selection[0], int)
        ):
            quoted_scores.add((fp.selection[0], fp.selection[1]))

    evaluated_outcomes: list[EvaluatedOutcome] = []
    reco_count = 0

    for item in fortuna_prices:
        p_fair = 0.0
        item_reason_override: str | None = None

        # Compute p_fair
        if fair_matrix.status == "no_market":
            p_fair = 0.0
        elif (
            item.market == "exact_score"
            and isinstance(item.selection, tuple)
            and isinstance(item.selection[0], int)
        ):
            h, a = item.selection[0], item.selection[1]
            p_fair = fair_matrix.exact_score(h, a) if 0 <= h <= 10 and 0 <= a <= 10 else 0.0
        elif item.market == "other":
            # Grid validation: full 25 scores (0..4) must be present in the batch
            expected_scores = {
                (h, a)
                for h in range(fortuna_cfg.cs_grid_max_goals + 1)
                for a in range(fortuna_cfg.cs_grid_max_goals + 1)
            }
            quoted_in_grid = expected_scores & quoted_scores
            if len(quoted_in_grid) < len(expected_scores):
                p_fair = 0.0
                item_reason_override = (
                    f"Неполная сетка Fortuna ({len(quoted_in_grid)} из {len(expected_scores)}): "
                    f"inny не оценивается"
                )
            else:
                p_fair = max(0.0, 1.0 - float(fair_matrix.matrix[:5, :5].sum()))
                item_reason_override = "Справочно: inny (хвост 5+ смещен, z=-2.25)"
        elif item.market == "multiscore":
            if isinstance(item.selection, (frozenset, set, list)):
                valid_scores = [
                    sc for sc in item.selection if 0 <= sc[0] <= 10 and 0 <= sc[1] <= 10
                ]
                p_fair = fair_matrix.multiscore(valid_scores)
            elif item.selection == "multi:remis":
                # All draws (0:0 .. 10:10)
                p_fair = float(np.diag(fair_matrix.matrix).sum())
            elif item.selection == "multi:home_other":
                # Home wins minus groups 1-4
                covered_home = {
                    (1, 0), (2, 0), (3, 0), (4, 0), (5, 0), (6, 0),
                    (2, 1), (3, 1), (4, 1), (5, 1),
                    (3, 2), (4, 2), (4, 3),
                }
                cov_p = sum(fair_matrix.exact_score(h, a) for h, a in covered_home)
                p_fair = max(0.0, fair_matrix.p_home_devig - cov_p)
            elif item.selection == "multi:away_other":
                # Away wins minus groups 1-4
                covered_away = {
                    (0, 1), (0, 2), (0, 3), (0, 4), (0, 5), (0, 6),
                    (1, 2), (1, 3), (1, 4), (1, 5),
                    (2, 3), (2, 4), (3, 4),
                }
                cov_p = sum(fair_matrix.exact_score(h, a) for h, a in covered_away)
                p_fair = max(0.0, fair_matrix.p_away_devig - cov_p)
        elif (
            item.market == "goals_range"
            and isinstance(item.selection, tuple)
            and isinstance(item.selection[0], int)
        ):
            lo, hi = item.selection[0], item.selection[1]
            p_fair = fair_matrix.goals_range(lo, hi)
        elif (
            item.market == "totals"
            and isinstance(item.selection, tuple)
            and isinstance(item.selection[0], str)
        ):
            t_kind, t_line = item.selection[0], float(item.selection[1])
            p_fair = (
                fair_matrix.total_over(t_line)
                if t_kind == "over"
                else fair_matrix.total_under(t_line)
            )
        elif (
            item.market == "team_totals"
            and isinstance(item.selection, tuple)
            and len(item.selection) == 3
        ):
            team_s = str(item.selection[0])
            t_kind = str(item.selection[1])
            t_line = float(item.selection[2])
            if team_s in ("away", "2") or "away" in team_s.lower():
                marg = fair_matrix.matrix.sum(axis=0)
            else:
                marg = fair_matrix.matrix.sum(axis=1)
            if t_kind == "over":
                p_fair = float(sum(marg[g] for g in range(11) if g > t_line))
            else:
                p_fair = float(sum(marg[g] for g in range(11) if g < t_line))
        elif item.market == "h2h":
            if item.selection == "1":
                p_fair = fair_matrix.p_home_devig
            elif item.selection == "X":
                p_fair = fair_matrix.p_draw_devig
            elif item.selection == "2":
                p_fair = fair_matrix.p_away_devig

        edge = item.odds * p_fair - 1.0
        fair_odds = 1.0 / p_fair if p_fair > 1e-9 else 9999.0
        kelly = calculate_kelly_quarter_capped(p_fair, item.odds)

        # Decision logic
        recommended = False
        reason = ""

        if fair_matrix.status == "no_market":
            recommended = False
            reason = "Рынок отсутствует"
        elif fair_matrix.status == "fallback":
            recommended = False
            reason = "Резервная модель (справочно)"
        elif item.market in ("totals", "team_totals", "h2h"):
            recommended = False
            reason = "Справочный рынок"
        elif item.market == "other":
            recommended = False
            reason = item_reason_override or "Справочно: inny"
        elif is_excluded:
            recommended = False
            reason = "Исключено (≥2 ключевых отсутствующих)"
        elif item.market == "exact_score":
            base_edge_req = thresh.exact_score_edge
            mult = risk_cfg.absence_edge_multiplier if has_risk_multiplier else 1.0
            effective_edge = base_edge_req * mult
            if edge > thresh.implausible_edge_exact_score:
                reason = (
                    f"Неправдоподобный edge ({edge:+.1%} > "
                    f"{thresh.implausible_edge_exact_score:.0%}): "
                    "проверь ввод / матч / свежесть рынка"
                )
            elif item.odds > thresh.exact_score_max_odds:
                reason = f"Длинный коэффициент (> {thresh.exact_score_max_odds:.0f})"
            elif p_fair < thresh.exact_score_min_p:
                reason = f"Мала p_fair (< {thresh.exact_score_min_p:.1%})"
            elif edge < effective_edge:
                reason = f"Ниже порога edge (< {effective_edge:.1%})"
            else:
                recommended = True
        elif item.market == "multiscore":
            base_edge_req = thresh.multiscore_edge
            mult = risk_cfg.absence_edge_multiplier if has_risk_multiplier else 1.0
            effective_edge = base_edge_req * mult
            if edge > thresh.implausible_edge_multiscore:
                reason = (
                    f"Неправдоподобный edge ({edge:+.1%} > "
                    f"{thresh.implausible_edge_multiscore:.0%}): "
                    "проверь ввод / матч / свежесть рынка"
                )
            elif item.odds > thresh.multiscore_max_odds:
                reason = f"Длинный коэффициент (> {thresh.multiscore_max_odds:.0f})"
            elif p_fair < thresh.multiscore_min_p:
                reason = f"Мала p_fair (< {thresh.multiscore_min_p:.1%})"
            elif edge < effective_edge:
                reason = f"Ниже порога edge (< {effective_edge:.1%})"
            else:
                recommended = True
        elif item.market == "goals_range":
            base_edge_req = thresh.multiscore_edge
            mult = risk_cfg.absence_edge_multiplier if has_risk_multiplier else 1.0
            effective_edge = base_edge_req * mult
            if edge > thresh.implausible_edge_goals_range:
                reason = (
                    f"Неправдоподобный edge ({edge:+.1%} > "
                    f"{thresh.implausible_edge_goals_range:.0%}): "
                    "проверь ввод / матч / свежесть рынка"
                )
            elif item.odds > thresh.goals_range_max_odds:
                reason = f"Длинный коэффициент (> {thresh.goals_range_max_odds:.0f})"
            elif p_fair < thresh.goals_range_min_p:
                reason = f"Мала p_fair (< {thresh.goals_range_min_p:.1%})"
            elif edge < effective_edge:
                reason = f"Ниже порога edge (< {effective_edge:.1%})"
            else:
                recommended = True

        if recommended:
            reco_count += 1

        disp = _format_selection_display(item)
        outcome_flags: list[str] = []
        if fair_matrix.status == "fallback":
            outcome_flags.append("model_fallback")

        evaluated_outcomes.append(
            EvaluatedOutcome(
                market=item.market,
                selection_display=disp,
                selection_key=item.selection_key,
                odds_fortuna=item.odds,
                p_fair=p_fair,
                fair_odds=fair_odds,
                edge=edge,
                recommended=recommended,
                rejection_reason=reason,
                stake_flat=1.0 if recommended else 0.0,
                kelly_quarter_capped=kelly,
                flags=outcome_flags,
                raw_price=item,
            )
        )

    return EvaluationResult(
        match_name=match_name,
        snapshot_time=fair_matrix.snapshot_time,
        source=fair_matrix.source,
        books_used=fair_matrix.books_used,
        input_hash=fair_matrix.input_hash,
        fortuna_overround=overround,
        outcomes=evaluated_outcomes,
        risk_flags=flags,
        model_anomaly=is_anomaly,
        is_excluded=is_excluded,
        recommendations_count=reco_count,
    )

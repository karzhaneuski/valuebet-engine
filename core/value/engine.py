"""Value engine: edge calculation, thresholds, risk flags, and Russian formatting (SPEC §5, §6, §7).

Calculates edge and bet sizing against fair market matrix:
- edge = odds_fortuna * p_fair - 1;
- thresholds from config (exact_score >= 12%, p >= 4%, odds <= 21; multiscore >= 8%);
- totals, BTTS, and 1X2 are reference-only (no recommendations);
- risk flags: 1 key absence or conflicting sources -> ⚠️ and threshold x 1.5;
  >= 2 key absences -> excluded;
- model anomaly flag: discrepancy in home goal share > threshold (default 15%);
- bet sizing: flat 1.0 unit, 1/4 Kelly informational capped at 2% bankroll;
- Russian human-readable CLI summary table output.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from core.config import AppConfig, load_config
from core.value.fair_matrix import FairMatrix
from core.value.fortuna_input import (
    FortunaPrice,
    calculate_fortuna_overround,
)


@dataclass(frozen=True)
class EvaluatedOutcome:
    """Evaluation result for an individual Fortuna outcome."""

    market: str
    selection_display: str
    odds_fortuna: float
    p_fair: float
    fair_odds: float
    edge: float
    recommended: bool
    rejection_reason: str
    stake_flat: float
    kelly_quarter_capped: float
    flags: list[str]
    raw_price: FortunaPrice


@dataclass(frozen=True)
class EvaluationResult:
    """Full match evaluation result containing all evaluated outcomes and metadata."""

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
        """Format human-readable Russian summary table for CLI output."""
        sep_double = "=" * 94
        sep_single = "-" * 94
        lines: list[str] = [sep_double]

        # Header metadata
        time_str = (
            self.snapshot_time.strftime("%Y-%m-%d %H:%M UTC")
            if self.snapshot_time
            else "не указано"
        )
        books_str = ", ".join(self.books_used) if self.books_used else "нет"
        lines.append(f"Матч: {self.match_name}")
        lines.append(f"Источник рынка: {self.source} ({books_str}) | Время снимка: {time_str}")

        margin_str = (
            f"{self.fortuna_overround:.1%}"
            if self.fortuna_overround is not None
            else "не определена"
        )
        lines.append(f"Маржа Fortuna: {margin_str} | Хеш входа: {self.input_hash[:16]}...")

        # Flags section
        if self.risk_flags:
            lines.append("Флаги риска:")
            for flag in self.risk_flags:
                lines.append(f"  • {flag}")

        lines.append(sep_single)
        # Table columns
        col_hdr = (
            f"{'Исход':<22} "
            f"{'Fortuna':>7} "
            f"{'p_fair':>7} "
            f"{'Справедл.':>9} "
            f"{'Edge':>8} "
            f"{'Реком.':>7} "
            f"{'¼ Келли':>8} "
            f"{'Причина отказа'}"
        )
        lines.append(col_hdr)
        lines.append(sep_single)

        for out in self.outcomes:
            rec_str = "ДА" if out.recommended else "НЕТ"
            edge_str = f"{out.edge:+.1%}"
            pf_str = f"{out.p_fair:.1%}"
            fair_odds_str = f"{out.fair_odds:.2f}" if out.fair_odds < 1000.0 else ">999"
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
    """Format outcome selection in readable text."""
    if (
        item.market == "exact_score"
        and isinstance(item.selection, tuple)
        and isinstance(item.selection[0], int)
    ):
        return f"{item.selection[0]}:{item.selection[1]}"
    if item.market == "other":
        return "Любой другой счёт"
    if item.market == "multiscore" and isinstance(item.selection, (frozenset, set, list)):
        sorted_sc = sorted(item.selection)
        return ", ".join(f"{h}:{a}" for h, a in sorted_sc)
    if (
        item.market == "goals_range"
        and isinstance(item.selection, tuple)
        and isinstance(item.selection[0], int)
    ):
        return f"{item.selection[0]}-{item.selection[1]} гола"
    if (
        item.market == "totals"
        and isinstance(item.selection, tuple)
        and isinstance(item.selection[0], str)
    ):
        t_kind, t_line = item.selection[0], float(item.selection[1])
        kind_ru = "больше" if t_kind == "over" else "меньше"
        return f"Тотал {kind_ru} {t_line}"
    if item.market == "h2h":
        return f"Исход {item.selection}"
    return str(item.selection)


def evaluate_fortuna_prices(
    fair_matrix: FairMatrix,
    fortuna_prices: list[FortunaPrice],
    config: AppConfig | None = None,
    model_home_share: float | None = None,
    key_absences_home: list[str] | None = None,
    key_absences_away: list[str] | None = None,
    sources_conflict: bool = False,
    match_name: str = "Матч",
) -> EvaluationResult:
    """Evaluate manual Fortuna prices against fair market matrix.

    Evaluates exact_score, multiscore, goals_range against fair probabilities,
    applies thresholds from config, risk multiplier x1.5 on ⚠️, and exclusions.
    Totals and 1X2 are evaluated for reference only.
    """
    cfg = config or load_config()
    thresh = cfg.thresholds
    risk_cfg = cfg.risk_flags
    fm_cfg = cfg.fair_matrix

    overround = calculate_fortuna_overround(fortuna_prices)

    # 1. Evaluate risk flags
    abs_h = key_absences_home or []
    abs_a = key_absences_away or []
    flags: list[str] = []

    # Check exclusions (>= 2 key absences in any team)
    max_abs = risk_cfg.max_key_absences_per_team
    is_excluded = len(abs_h) >= max_abs or len(abs_a) >= max_abs
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

    # Model anomaly check
    is_anomaly, anomaly_msg = check_model_anomaly(
        fair_matrix.home_share,
        model_home_share,
        threshold=fm_cfg.anomaly_share_threshold,
    )
    if is_anomaly:
        flags.append(anomaly_msg)

    # Map of all exact scores in batch (to compute remaining mass for 'other')
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
            # Tail mass of all scores not quoted in this Fortuna batch
            if quoted_scores:
                sum_quoted = sum(
                    fair_matrix.exact_score(h, a)
                    for h, a in quoted_scores
                    if 0 <= h <= 10 and 0 <= a <= 10
                )
                p_fair = max(0.0, 1.0 - sum_quoted)
            else:
                p_fair = max(0.0, 1.0 - fair_matrix.matrix[:5, :5].sum())
        elif item.market == "multiscore" and isinstance(item.selection, (frozenset, set, list)):
            valid_scores = [sc for sc in item.selection if 0 <= sc[0] <= 10 and 0 <= sc[1] <= 10]
            p_fair = fair_matrix.multiscore(valid_scores)
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
        elif item.market in ("totals", "h2h"):
            # Reference-only by SPEC §1
            recommended = False
            reason = "Справочный рынок"
        elif is_excluded:
            recommended = False
            reason = "Исключено (≥2 ключевых отсутствующих)"
        elif item.market in ("exact_score", "other"):
            base_edge_req = thresh.exact_score_edge
            mult = risk_cfg.absence_edge_multiplier if has_risk_multiplier else 1.0
            effective_edge = base_edge_req * mult
            if item.odds > thresh.exact_score_max_odds:
                reason = f"Длинный коэффициент (> {thresh.exact_score_max_odds:.0f})"
            elif p_fair < thresh.exact_score_min_p:
                reason = f"Мала p_fair (< {thresh.exact_score_min_p:.1%})"
            elif edge < effective_edge:
                reason = f"Ниже порога edge (< {effective_edge:.1%})"
            else:
                recommended = True
        elif item.market in ("multiscore", "goals_range"):
            base_edge_req = thresh.multiscore_edge
            mult = risk_cfg.absence_edge_multiplier if has_risk_multiplier else 1.0
            effective_edge = base_edge_req * mult
            if edge < effective_edge:
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

"""Candidate prefilter for manual Fortuna odds entry and Pinnacle correct_score purchasing.

Implements selection criteria per SPEC §5 and docs/decisions.md (B6.4):
1. Fortuna grid availability (boolean confirmation; default: unknown/pass);
2. Mass of high draws in the fair market matrix (zone where kappa_high=0.20 operates);
3. League rotation (round-robin interleaving across active leagues with configurable cap);
4. Weekly budget limit (<= 10 matches/week per Pinnacle correct_score budget);
5. Model discrepancy is EXCLUSIVELY an anomaly flag, NOT a ranking booster (alpha = 0).

Every decision is logged to the `prefilter_decisions` table.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import AppConfig, load_config
from core.orm import PrefilterDecision
from core.value.fair_matrix import FairMatrix

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CandidateMatch:
    """Input match candidate for prefilter evaluation."""

    match_id: int
    match_name: str
    league: str
    kickoff: dt.datetime
    fair_matrix: FairMatrix
    has_fortuna_grid: bool | None = None  # None = unknown (pass); False = confirmed absent
    model_home_share: float | None = None  # optional Dixon-Coles model share (anomaly flag only)


@dataclass(frozen=True)
class PrefilterDecisionRecord:
    """Outcome of prefilter evaluation for a single match."""

    match_id: int
    match_name: str
    league: str
    kickoff: dt.datetime
    high_draws_mass: float
    accepted: bool
    reason: str
    rank: int | None = None
    flags: tuple[str, ...] = field(default_factory=tuple)
    p_fair_btts: float = 0.0
    p_model_btts: float | None = None
    totals_edge: float = 0.0


@dataclass
class PrefilterReport:
    """Complete prefilter evaluation summary."""

    total_evaluated: int
    total_accepted: int
    items: list[PrefilterDecisionRecord]
    accepted_by_league: dict[str, int]

    @property
    def candidates(self) -> list[PrefilterDecisionRecord]:
        """Return only accepted candidates sorted by rank."""
        return [it for it in self.items if it.accepted]

    def format_table(self) -> str:
        """Format candidate prefilter summary table in Russian."""
        sep_double = "=" * 88
        sep_single = "-" * 88

        header = (
            f"{'Ранг':<6} {'Матч':<30} {'Лига':<12} "
            f"{'Время (UTC)':<16} {'Ничьи 2:2+':<11} {'Статус':<10}"
        )

        lines: list[str] = [
            sep_double,
            "ПРЕФИЛЬТР КАНДИДАТОВ (Fortuna / Pinnacle correct_score)",
            f"Всего рассмотрено: {self.total_evaluated} | Отобрано: {self.total_accepted}",
            sep_single,
            header,
            sep_single,
        ]

        for item in self.items:
            rank_str = f"#{item.rank}" if item.rank is not None else "—"
            kickoff_str = item.kickoff.strftime("%Y-%m-%d %H:%M")
            draw_str = f"{item.high_draws_mass:.2%}"
            status_str = "ОТОБРАН" if item.accepted else "ОТКЛОНЁН"

            row = (
                f"{rank_str:<6} "
                f"{item.match_name:<30} "
                f"{item.league:<12} "
                f"{kickoff_str:<16} "
                f"{draw_str:<11} "
                f"{status_str:<10} "
                f"  {item.reason}"
            )
            lines.append(row)

            for flag in item.flags:
                lines.append(f"       └── {flag}")

        lines.append(sep_single)
        if self.accepted_by_league:
            league_breakdown = ", ".join(
                f"{lg}: {cnt}" for lg, cnt in sorted(self.accepted_by_league.items())
            )
            lines.append(f"Распределение по лигам: {league_breakdown}")
        lines.append(sep_double)
        return "\n".join(lines)


def compute_high_draws_mass(matrix: FairMatrix) -> float:
    """Sum fair probabilities for high draws (2:2, 3:3, ..., 10:10)."""
    grid = matrix.matrix
    n = min(grid.shape[0], grid.shape[1])
    return float(sum(grid[i, i] for i in range(2, n)))


def filter_candidates(
    matches: Sequence[CandidateMatch],
    config: AppConfig | None = None,
    weekly_cap: int | None = None,
    max_per_league: int | None = None,
) -> PrefilterReport:
    """Filter and rank candidate matches according to B6.4 and SPEC §5.

    1. Checks Fortuna grid availability (rejects if has_fortuna_grid is False).
    2. Rejects if fair_matrix has no_market status.
    3. Calculates high_draws_mass (sum of M_{i,i} for i >= 2).
    4. Detects model anomaly without changing ranking priority.
    5. Sorts matches by high_draws_mass descending within each league.
    6. Interleaves round-robin across leagues with max_per_league cap up to weekly_cap.
    """
    cfg = config or load_config()
    cap = (
        weekly_cap
        if weekly_cap is not None
        else cfg.odds_policy.pinnacle_correct_score_max_weekly
    )
    league_cap = (
        max_per_league
        if max_per_league is not None
        else getattr(cfg.prefilter, "max_per_league", 3)
    )
    anomaly_thresh = cfg.fair_matrix.anomaly_share_threshold

    evaluated: list[PrefilterDecisionRecord] = []
    eligible: list[CandidateMatch] = []
    high_draw_map: dict[int, float] = {}
    flags_map: dict[int, list[str]] = {}

    for m in matches:
        flags: list[str] = []

        # 1. Fortuna grid availability check
        if m.has_fortuna_grid is False:
            evaluated.append(
                PrefilterDecisionRecord(
                    match_id=m.match_id,
                    match_name=m.match_name,
                    league=m.league,
                    kickoff=m.kickoff,
                    high_draws_mass=0.0,
                    accepted=False,
                    reason="Нет сетки точного счёта Fortuna",
                    p_fair_btts=0.0,
                )
            )
            continue

        # 2. Market presence check
        if m.fair_matrix.status == "no_market":
            evaluated.append(
                PrefilterDecisionRecord(
                    match_id=m.match_id,
                    match_name=m.match_name,
                    league=m.league,
                    kickoff=m.kickoff,
                    high_draws_mass=0.0,
                    accepted=False,
                    reason="Отсутствуют рыночные котировки (no_market)",
                    p_fair_btts=0.0,
                )
            )
            continue

        # 3. High draws mass
        h_mass = compute_high_draws_mass(m.fair_matrix)
        high_draw_map[m.match_id] = h_mass

        # 4. Dixon-Coles model anomaly check (flag only, alpha = 0)
        if m.model_home_share is not None:
            diff = abs(m.fair_matrix.home_share - m.model_home_share)
            if diff > anomaly_thresh:
                flags.append(
                    f"⚠️ Аномалия модели: расхождение доли голов {diff:.1%} "
                    f"(рынок {m.fair_matrix.home_share:.1%}, модель {m.model_home_share:.1%}) "
                    f"> порога {anomaly_thresh:.0%}. Проверь составы/данные!"
                )

        flags_map[m.match_id] = flags
        eligible.append(m)

    # Group eligible matches by league
    by_league: dict[str, list[CandidateMatch]] = {}
    for m in eligible:
        by_league.setdefault(m.league, []).append(m)

    # Sort each league's matches by high_draws_mass descending
    for lg in by_league:
        by_league[lg].sort(
            key=lambda x: high_draw_map[x.match_id],
            reverse=True,
        )

    # Order leagues by the strength of their top candidate
    ordered_leagues = sorted(
        by_league.keys(),
        key=lambda lg: high_draw_map[by_league[lg][0].match_id],
        reverse=True,
    )

    accepted_matches: list[CandidateMatch] = []
    accepted_by_league: dict[str, int] = {lg: 0 for lg in ordered_leagues}
    accepted_set: set[int] = set()

    # Round-robin selection across leagues
    # Max rounds is the maximum number of matches in any league
    max_rounds = max((len(by_league[lg]) for lg in ordered_leagues), default=0)

    for round_idx in range(max_rounds):
        for lg in ordered_leagues:
            if len(accepted_matches) >= cap:
                break
            if accepted_by_league[lg] >= league_cap:
                continue
            if round_idx < len(by_league[lg]):
                cand = by_league[lg][round_idx]
                accepted_matches.append(cand)
                accepted_set.add(cand.match_id)
                accepted_by_league[lg] += 1
        if len(accepted_matches) >= cap:
            break

    # If weekly cap is not yet reached and some leagues have remaining matches beyond league_cap,
    # fill remaining slots up to weekly_cap
    if len(accepted_matches) < cap:
        remaining_pool = [
            m for m in eligible if m.match_id not in accepted_set
        ]
        remaining_pool.sort(
            key=lambda x: high_draw_map[x.match_id],
            reverse=True,
        )
        for cand in remaining_pool:
            if len(accepted_matches) >= cap:
                break
            accepted_matches.append(cand)
            accepted_set.add(cand.match_id)
            accepted_by_league[cand.league] = accepted_by_league.get(cand.league, 0) + 1

    # Record accepted candidates
    accepted_records: list[PrefilterDecisionRecord] = []
    for rank, m in enumerate(accepted_matches, start=1):
        h_mass = high_draw_map[m.match_id]
        item_flags: tuple[str, ...] = tuple(flags_map.get(m.match_id, []))
        p_btts = m.fair_matrix.btts()
        reason = (
            f"Отобран (масса ничьих 2:2+: {h_mass:.2%}, ротация: {m.league})"
        )
        accepted_records.append(
            PrefilterDecisionRecord(
                match_id=m.match_id,
                match_name=m.match_name,
                league=m.league,
                kickoff=m.kickoff,
                high_draws_mass=h_mass,
                accepted=True,
                reason=reason,
                rank=rank,
                flags=item_flags,
                p_fair_btts=p_btts,
            )
        )

    # Record rejected eligible matches
    rejected_eligible: list[PrefilterDecisionRecord] = []
    for m in eligible:
        if m.match_id in accepted_set:
            continue
        h_mass = high_draw_map[m.match_id]
        cand_flags: tuple[str, ...] = tuple(flags_map.get(m.match_id, []))
        p_btts = m.fair_matrix.btts()

        hit_league = accepted_by_league.get(m.league, 0) >= league_cap
        hit_weekly = len(accepted_matches) >= cap

        if hit_league and hit_weekly:
            reason = (
                f"Превышен лимит лиги {m.league} (макс. {league_cap}) "
                f"и недельный лимит ({cap})"
            )
        elif hit_league:
            reason = f"Превышен лимит лиги {m.league} (макс. {league_cap})"
        else:
            reason = f"Превышен недельный бюджетный лимит ({cap})"

        rejected_eligible.append(
            PrefilterDecisionRecord(
                match_id=m.match_id,
                match_name=m.match_name,
                league=m.league,
                kickoff=m.kickoff,
                high_draws_mass=h_mass,
                accepted=False,
                reason=reason,
                flags=cand_flags,
                p_fair_btts=p_btts,
            )
        )

    # Combine all items: accepted first, then rejected eligible, then other rejected
    rejected_eligible.sort(key=lambda x: x.high_draws_mass, reverse=True)
    all_items = accepted_records + rejected_eligible + evaluated

    # Filter accepted_by_league to non-zero leagues
    active_accepted_by_league = {lg: cnt for lg, cnt in accepted_by_league.items() if cnt > 0}

    return PrefilterReport(
        total_evaluated=len(matches),
        total_accepted=len(accepted_records),
        items=all_items,
        accepted_by_league=active_accepted_by_league,
    )


def log_prefilter_decisions_to_db(
    session: Session,
    report: PrefilterReport,
    decision_date: dt.date | None = None,
) -> int:
    """Persist prefilter decisions into the database prefilter_decisions table.

    Updates existing decision if (match_id, decision_date) exists, or inserts new row.
    Returns the count of saved/updated records.
    """
    d_date = decision_date or dt.date.today()
    saved_count = 0

    for item in report.items:
        # Check existing decision for this match and date
        existing = session.scalar(
            select(PrefilterDecision).where(
                PrefilterDecision.match_id == item.match_id,
                PrefilterDecision.decision_date == d_date,
            )
        )

        if item.p_model_btts is not None:
            p_model = Decimal(f"{item.p_model_btts:.4f}")
        else:
            p_model = Decimal(f"{item.p_fair_btts:.4f}")

        p_proxy = Decimal(f"{item.p_fair_btts:.4f}")
        edge = Decimal(f"{item.totals_edge:.4f}")

        if existing is not None:
            existing.p_model_btts = p_model
            existing.p_proxy_btts = p_proxy
            existing.totals_edge = edge
            existing.accepted = item.accepted
            existing.reason = item.reason
        else:
            rec = PrefilterDecision(
                match_id=item.match_id,
                decision_date=d_date,
                p_model_btts=p_model,
                p_proxy_btts=p_proxy,
                totals_edge=edge,
                accepted=item.accepted,
                reason=item.reason,
            )
            session.add(rec)
        saved_count += 1

    session.flush()
    return saved_count

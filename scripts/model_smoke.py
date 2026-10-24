"""Phase-3 smoke test on real data (read-only against a DB copy).

Fits all five leagues at as_of=now, reports per-league parameters, mean
expected goals vs the actual means of the last two seasons, top attacks/
defences, promoted-team priors, and an out-of-sample comparison of the
model's P(over 2.5) against de-vigged market probabilities from
odds_snapshots (Pinnacle when present, else consensus of allowed books,
docs/decisions.md S10).

Usage:
    DATABASE_URL=sqlite:////tmp/model-smoke.db uv run python scripts/model_smoke.py
Never point this at valuebet.db: the report must be reproducible on a copy.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import time
from decimal import Decimal
from pathlib import Path

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from core.config import load_config
from core.db import session_scope
from core.model.dataset import load_quarantined_match_ids
from core.model.dixon_coles import FittedModel, fit_league
from core.model.markets import total_over
from core.orm import Match, MatchStat, OddsSnapshot, Team

TOP_LEAGUES = ["premier_league", "la_liga", "serie_a", "bundesliga", "ligue_1"]
LAST_TWO_SEASONS = [2025, 2026]  # 2025 complete, 2026 in progress
LINE = Decimal("2.5")


def expected_vs_actual(
    db_url: str, league: str, as_of: dt.datetime, model: FittedModel,
    seasons: list[int],
) -> dict[str, object]:
    """Mean model E[home/away goals] vs actual means over league seasons.

    The actual means are also split per season: with time decay the model
    level tracks the RECENT season, so a rising-scoring league shows a
    positive delta against the flat two-season average.
    """
    away_team = aliased(Team)
    with session_scope(db_url) as s:
        rows = s.execute(
            select(Match, Team.name, away_team.name)
            .join(Team, Match.home_team_id == Team.id)
            .join(away_team, Match.away_team_id == away_team.id)
            .where(Match.league == league, Match.status == "finished",
                   Match.commence_time_utc < as_of,
                   Match.season.in_(seasons))
        ).all()
        stats = s.scalars(select(MatchStat).where(
            MatchStat.source == "understat",
            MatchStat.match_id.in_([m.id for m, _, _ in rows]))).all()
    goals = {st.match_id: (st.home_goals, st.away_goals)
             for st in stats if st.home_goals is not None}
    exp_h = exp_a = act_h = act_a = 0.0
    n = 0
    for m, home, away in rows:
        if m.id not in goals:
            continue
        eh, ea = model.expected_goals(home, away)
        exp_h += eh
        exp_a += ea
        act_h += goals[m.id][0]
        act_a += goals[m.id][1]
        n += 1
    per_season: dict[int, list[float]] = {}
    for m, _, _ in rows:
        if m.id not in goals:
            continue
        acc = per_season.setdefault(m.season, [0.0, 0.0, 0.0])
        acc[0] += goals[m.id][0]
        acc[1] += goals[m.id][1]
        acc[2] += 1.0
    season_means = {
        season: (v[0] / v[2], v[1] / v[2]) for season, v in per_season.items()
    }
    return {
        "n": n,
        "exp_home": exp_h / n, "exp_away": exp_a / n,
        "act_home": act_h / n, "act_away": act_a / n,
        "season_means": season_means,
    }


def market_probabilities(
    db_url: str, as_of: dt.datetime, excluded_books: set[str],
) -> dict[int, dict[str, object]]:
    """Latest de-vigged P(over 2.5) per upcoming match.

    Pinnacle when a complete Over/Under pair exists, else the mean of the
    de-vigged probabilities over allowed books (S10: Pinnacle de-vigged +
    consensus of allowed books).
    """
    with session_scope(db_url) as s:
        snaps = s.execute(
            select(OddsSnapshot)
            .join(Match, Match.id == OddsSnapshot.match_id)
            .where(OddsSnapshot.market == "totals",
                   OddsSnapshot.line == LINE,
                   Match.status == "scheduled",
                   Match.commence_time_utc > as_of)
        ).scalars().all()
        away_team = aliased(Team)
        matches = s.execute(
            select(Match, Team.name, away_team.name)
            .join(Team, Match.home_team_id == Team.id)
            .join(away_team, Match.away_team_id == away_team.id)
            .where(Match.status == "scheduled", Match.commence_time_utc > as_of)
        ).all()

    by_match: dict[int, dict[str, dict[str, Decimal]]] = {}
    for o in snaps:
        if o.match_id is None:
            continue
        book = by_match.setdefault(o.match_id, {}).setdefault(o.bookmaker, {})
        cur = book.get(o.outcome)
        if cur is None or o.captured_at_utc >= cur[1]:
            book[o.outcome] = (o.price, o.captured_at_utc)

    result: dict[int, dict[str, object]] = {}
    for m, home, away in matches:
        books = by_match.get(m.id, {})
        fair: list[tuple[str, float]] = []
        for name, pair in sorted(books.items()):
            if "Over" not in pair or "Under" not in pair:
                continue
            o, u = float(pair["Over"][0]), float(pair["Under"][0])
            if o <= 1.0 or u <= 1.0:
                continue
            fair.append((name, (1.0 / o) / (1.0 / o + 1.0 / u)))
        if not fair:
            continue
        pin = [p for name, p in fair if name == "pinnacle"]
        allowed = [p for name, p in fair if name not in excluded_books]
        if pin:
            p_market, source, n_books = pin[0], "pinnacle", 1
        elif allowed:
            p_market, source, n_books = (
                float(np.mean(allowed)), "consensus", len(allowed))
        else:
            continue
        result[m.id] = {
            "home": home, "away": away, "league": m.league,
            "kickoff": m.commence_time_utc, "p_market": p_market,
            "source": source, "n_books": n_books,
        }
    return result


def team_match_counts(
    db_url: str, league: str, as_of: dt.datetime,
) -> dict[str, int]:
    """Finished matches per team before as_of (history depth)."""
    with session_scope(db_url) as s:
        homes = s.execute(
            select(func.count(), Team.name)
            .join(Match, Match.home_team_id == Team.id)
            .where(Match.league == league, Match.status == "finished",
                   Match.commence_time_utc < as_of)
            .group_by(Team.name)).all()
        aways = s.execute(
            select(func.count(), Team.name)
            .join(Match, Match.away_team_id == Team.id)
            .where(Match.league == league, Match.status == "finished",
                   Match.commence_time_utc < as_of)
            .group_by(Team.name)).all()
    out: dict[str, int] = {}
    for c, name in homes:
        out[name] = out.get(name, 0) + int(c)
    for c, name in aways:
        out[name] = out.get(name, 0) + int(c)
    return out


def xg_per_goal(db_url: str, seasons: list[int]) -> dict[str, float]:
    """Understat sum(xG)/sum(goals) per league over the seasons."""
    with session_scope(db_url) as s:
        rows = s.execute(
            select(Match.league, MatchStat)
            .join(MatchStat, MatchStat.match_id == Match.id)
            .where(MatchStat.source == "understat",
                   Match.status == "finished",
                   Match.season.in_(seasons))
        ).all()
    goals: dict[str, float] = {}
    xg: dict[str, float] = {}
    for league, st in rows:
        if st.home_goals is None or st.home_xg is None:
            continue
        goals[league] = goals.get(league, 0.0) + st.home_goals + st.away_goals
        xg[league] = xg.get(league, 0.0) + float(st.home_xg) + float(st.away_xg)
    return {lg: xg[lg] / goals[lg] for lg in xg if goals[lg] > 0}


def pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 2:
        return float("nan")
    x, y = np.array(xs), np.array(ys)
    if x.std() == 0 or y.std() == 0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None,
                        help="database URL (default: DATABASE_URL env)")
    parser.add_argument("--report-dir", default="reports/model")
    args = parser.parse_args()
    db_url = args.db
    if db_url is None:
        db_url = os.environ.get("DATABASE_URL")
        if db_url is None:
            parser.error("pass --db or set DATABASE_URL (use a COPY of the db)")

    config = load_config()
    as_of = dt.datetime.now(dt.UTC)
    quarantine = load_quarantined_match_ids(
        Path("reports/ingest/backfill_report.json"))

    lines: list[str] = [
        "# Phase-3 model smoke test (real data)",
        "",
        f"- as_of: `{as_of.isoformat()}`",
        f"- db: `{db_url}` (copy; valuebet.db untouched)",
        f"- config: xi={config.model.xi}, w={config.model.weight_xg}, "
        f"home_advantage_prior={config.model.home_advantage_prior}, "
        f"promoted_team_prior={config.model.promoted_team_prior}",
        f"- B5 quarantined match ids excluded: {len(quarantine)}",
        "",
    ]

    models: dict[str, FittedModel] = {}
    for league in TOP_LEAGUES:
        t0 = time.perf_counter()
        model, stats = fit_league(db_url, league, as_of, config.model,
                                  excluded_match_ids=quarantine)
        elapsed = time.perf_counter() - t0
        models[league] = model
        eva = expected_vs_actual(db_url, league, as_of, model, LAST_TWO_SEASONS)
        top_att = sorted(model.attacks, key=lambda t: model.attacks[t], reverse=True)[:5]
        top_def = sorted(model.defences, key=lambda t: model.defences[t])[:5]
        lines += [
            f"## {league}",
            "",
            f"- matches: {model.n_matches} (loader: selected={stats.n_selected}, "
            f"no_score={stats.n_skipped_no_score}, fd_goals="
            f"{stats.n_goals_from_football_data}, missing_xg={stats.n_missing_xg})",
            f"- converged: {model.converged}, iterations: {model.iterations}, "
            f"loglik: {model.loglik:.1f}, fit time: {elapsed:.2f}s",
            f"- intercept mu0: {model.intercept:+.4f}, home advantage gamma: "
            f"{model.home_advantage:+.4f}, rho: {model.rho:+.4f}, "
            f"xi: {model.xi}, w: {model.w}",
            f"- mean goals {LAST_TWO_SEASONS} (n={eva['n']}): model home/away "
            f"{eva['exp_home']:.3f}/{eva['exp_away']:.3f} vs actual "
            f"{eva['act_home']:.3f}/{eva['act_away']:.3f} "
            f"(delta {eva['exp_home'] - eva['act_home']:+.3f}/"
            f"{eva['exp_away'] - eva['act_away']:+.3f}); actual per season "
            + "; ".join(
                f"{se}: {v[0]:.3f}/{v[1]:.3f}"
                for se, v in sorted(eva["season_means"].items())),
            f"- top-5 attacks: {', '.join(top_att)}",
            f"- top-5 defences: {', '.join(top_def)}",
        ]
        if model.prior_teams:
            prior = sorted(model.prior_teams)[0]
            lines.append(
                f"- promoted/prior teams ({len(model.prior_teams)}): "
                f"{', '.join(sorted(model.prior_teams))}; prior applied: "
                f"attack {model.attacks[prior]:+.4f}, defence "
                f"{model.defences[prior]:+.4f} (weakest-third rule)")
        else:
            lines.append(
                "- promoted/prior teams: none - every team in the league's "
                "match rows has played before as_of (season is ~9 rounds "
                "in); priors activate only before a promoted team's first "
                "match (season start / phase-4 walk-forward)")
        lines.append("")

    # --- market comparison (out of sample) ------------------------------
    excluded = set(config.books.excluded_books)
    history = {lg: team_match_counts(db_url, lg, as_of) for lg in TOP_LEAGUES}
    market = market_probabilities(db_url, as_of, excluded)
    rows: list[tuple[str, str, str, float, float, float, str, int]] = []
    for _, info in sorted(market.items(), key=lambda kv: kv[1]["kickoff"]):
        league = str(info["league"])
        model_ = models.get(league)
        if model_ is None:
            continue
        p_model = total_over(model_.score_matrix(str(info["home"]),
                                                 str(info["away"])), 2.5)
        p_market = float(info["p_market"])
        rows.append((league, str(info["home"]), str(info["away"]), p_model,
                     p_market, p_model - p_market, str(info["source"]),
                     int(info["n_books"])))

    lines += ["## Model vs market, P(over 2.5) - upcoming matches", "",
              "| league | match | model | market | delta (model-market) | market source (books) |",
              "|---|---|---|---|---|---|"]
    for league, home, away, pm, pk, d, src, nb in rows:
        lines.append(f"| {league} | {home} - {away} | {pm:.3f} | {pk:.3f} "
                     f"| {d:+.3f} | {src} ({nb}) |")

    # --- summary with confidence intervals and slopes (review S6) ----
    def corr_ci(r: float, n: int) -> tuple[float, float]:
        if n < 4 or not -1.0 < r < 1.0:
            return (float("nan"), float("nan"))
        z = np.arctanh(r)
        se = 1.0 / np.sqrt(n - 3)
        return (float(np.tanh(z - 1.96 * se)), float(np.tanh(z + 1.96 * se)))

    lines += ["", "### Per-league summary", "",
              "| league | n | mean delta [95% CI] | corr [95% CI] | "
              "sd(model) / sd(market) | bias flag (>0.05) |",
              "|---|---|---|---|---|---|"]
    pooled_d: list[float] = []
    pooled_m: list[float] = []
    pooled_k: list[float] = []
    for league in TOP_LEAGUES:
        sub = [r for r in rows if r[0] == league]
        if not sub:
            lines.append(f"| {league} | 0 | - | - | - | - |")
            continue
        diffs = [r[5] for r in sub]
        pooled_d += diffs
        pooled_m += [r[3] for r in sub]
        pooled_k += [r[4] for r in sub]
        mean_d = float(np.mean(diffs))
        half = 1.96 * float(np.std(diffs, ddof=1)) / np.sqrt(len(diffs))
        corr = pearson([r[3] for r in sub], [r[4] for r in sub])
        lo, hi = corr_ci(corr, len(sub))
        flag = "**BIAS?**" if abs(mean_d) > 0.05 else "ok"
        lines.append(
            f"| {league} | {len(sub)} | {mean_d:+.4f} "
            f"[{mean_d - half:+.4f}, {mean_d + half:+.4f}] | "
            f"{corr:.3f} [{lo:.3f}, {hi:.3f}] | "
            f"{float(np.std([r[3] for r in sub], ddof=1)):.3f} / "
            f"{float(np.std([r[4] for r in sub], ddof=1)):.3f} | {flag} |")
    if pooled_d:
        slope = float(np.polyfit(pooled_m, pooled_k, 1)[0])
        mean_d = float(np.mean(pooled_d))
        half = 1.96 * float(np.std(pooled_d, ddof=1)) / np.sqrt(len(pooled_d))
        lines.append(
            f"| **pooled** | {len(pooled_d)} | {mean_d:+.4f} "
            f"[{mean_d - half:+.4f}, {mean_d + half:+.4f}] | - | "
            f"{float(np.std(pooled_m, ddof=1)):.3f} / "
            f"{float(np.std(pooled_k, ddof=1)):.3f} | "
            f"market-on-model slope **{slope:.3f}** |")

    top = sorted(rows, key=lambda r: -abs(r[5]))[:5]
    thin_in_top = sum(
        1 for lg, h, a, *_ in top
        if min(history[lg].get(h, 0), history[lg].get(a, 0)) < 100
        or models[lg].prior_teams & {h, a})
    lines += ["", "### Largest disagreements", ""]
    for league, home, away, pm, pk, d, src, nb in top:
        nh = history[league].get(home, 0)
        na = history[league].get(away, 0)
        note = "prior (no-history) team" if models[league].prior_teams & {
            home, away} else f"history depth: {nh}/{na} matches"
        lines.append(f"- {league}: {home} - {away}: model {pm:.3f} vs market "
                     f"{pk:.3f} ({d:+.3f}, {src}, {nb} "
                     f"book{'s' if nb != 1 else ''}) - {note}")

    # xG conversion per league over the two seasons (from the data)
    xg_ratios = xg_per_goal(db_url, LAST_TWO_SEASONS)
    lines += ["", "### What the disagreements may be caused by", ""]
    lines.append(
        f"- Thin top-flight history: {thin_in_top} of the top-5 "
        "disagreements involve a team with <100 league matches in the DB "
        "(no second-division data) or a prior-only team; the market "
        "additionally prices form, injuries and lineups the model cannot "
        "see (phase-5/6 job).")
    lines.append(
        "- Low-scoring matchups disagree through the teams' low RATES, "
        "not through rho: the Dixon-Coles tau only moves mass inside the "
        "total-goals bands 0, 1 and 2 while preserving the marginals, so "
        "P(total >= 3) - and with it over 2.5 - is exactly rho-invariant "
        "(tested in tests/test_dixon_coles.py).")
    lines.append(
        "- The 50/50 goals/xG mix smooths hot/cold scoring streaks more "
        "than the market does.")
    lines.append(
        "- Some consensus de-vig values rest on few books (see the "
        "source column); Pinnacle is available for a minority of events.")

    lines += [
        "",
        "### Notes",
        "",
        "- IN-SAMPLE check: the model-vs-actual-goals comparison above "
        "uses matches the model was trained on (weighted by recency); "
        "leakage-free calibration is phase 4.",
        "- Why model means sit above the flat two-season actuals: the "
        "mixed target itself converts above goals - Understat "
        "sum(xG)/sum(goals) over these two seasons is "
        + ", ".join(f"{lg}: {xg_ratios.get(lg, float('nan')):.3f}"
                    for lg in TOP_LEAGUES if lg in xg_ratios)
        + f" (at w={config.model.weight_xg} that lifts the fitted level "
          "by (1-w)*(ratio-1)) - and the model is recency-weighted "
          f"(xi={config.model.xi}) while the actual average includes the "
          "older, lower-scoring season (see the per-season split).",
        "- The market comparison is out of sample: fitted at as_of, "
        "upcoming matches only, latest snapshot per bookmaker, excluded "
        "books removed, Pinnacle preferred (decisions S10).",
        "- Mean-delta CIs are normal-approximation (n is small); "
        "correlation CIs use Fisher z.",
        "",
    ]

    out = Path(args.report_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"smoke_{as_of.date().isoformat()}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"report written: {path} ({len(rows)} market rows)")


if __name__ == "__main__":
    main()

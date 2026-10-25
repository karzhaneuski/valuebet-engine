"""Walk-forward evidence for the phase-3 review (step A): log-score by
history-depth bin and the model-vs-market slope at as_of=now.

Runs weekly refits from 2025-08 to now over the five top leagues on a DB
copy, predicts each week's finished matches (kickoff within the week) and
bins the per-match score log-probability by the SHALLOWER team's number
of training matches (0 / 1-5 / 6-15 / >15). Reports the mean log-score
per bin for w=0.5 and w=1.0, plus the OLS slope of the de-vigged market
P(over 2.5) on the model probability for matches with totals snapshots.

Usage:
    DATABASE_URL=sqlite:////tmp/wf-smoke.db uv run python -m scripts.model_walkforward
"""

from __future__ import annotations

import datetime as dt
import os
from collections import defaultdict

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import aliased, selectinload

from core.config import ModelConfig, load_config
from core.db import session_scope
from core.model.dixon_coles import fit_league
from core.model.markets import total_over
from core.orm import Match, MatchStat, Team
from scripts.model_smoke import market_probabilities

TOP_LEAGUES = ["premier_league", "la_liga", "serie_a", "bundesliga", "ligue_1"]
START = dt.datetime(2025, 8, 8, 12, 0, tzinfo=dt.UTC)
BINS = [("0", lambda d: d == 0), ("1-5", lambda d: 1 <= d <= 5),
        ("6-15", lambda d: 6 <= d <= 15), (">15", lambda d: d > 15)]


def week_matches(db_url: str, lo: dt.datetime, hi: dt.datetime) -> list[tuple]:
    """Finished, scored matches with kickoff in [lo, hi)."""
    away_team = aliased(Team)
    with session_scope(db_url) as s:
        rows = s.execute(
            select(Match, Team.name, away_team.name)
            .join(Team, Match.home_team_id == Team.id)
            .join(away_team, Match.away_team_id == away_team.id)
            .where(Match.status == "finished",
                   Match.commence_time_utc >= lo,
                   Match.commence_time_utc < hi,
                   Match.league.in_(TOP_LEAGUES))
        ).all()
        ids = [m.id for m, _, _ in rows]
        stats = {st.match_id: (st.home_goals, st.away_goals) for st in
                 s.scalars(select(MatchStat).where(
                     MatchStat.source == "understat",
                     MatchStat.match_id.in_(ids)))
                 if st.home_goals is not None}
    return [(m, h, a, stats[m.id]) for m, h, a in rows if m.id in stats]


def run(db_url: str, weight_xg: float) -> dict[str, dict[str, list[float]]]:
    from core.backtest.periods import ensure_window_allowed

    ensure_window_allowed(START, dt.datetime.now(dt.UTC))
    config = ModelConfig(xi=0.005, weight_xg=weight_xg)
    now = dt.datetime.now(dt.UTC)
    scores: dict[str, dict[str, list[float]]] = {
        name: defaultdict(list) for name, _ in BINS}
    week = START
    n_fits = 0
    while week + dt.timedelta(days=7) <= now:
        for league in TOP_LEAGUES:
            model, _ = fit_league(db_url, league, week, config)
            n_fits += 1
            depth = team_depth(db_url, league, week)
            for _m, home, away, (gh, ga) in week_matches(
                    db_url, week, week + dt.timedelta(days=7)):
                if home not in model.attacks or away not in model.attacks:
                    continue
                p = float(model.score_matrix(home, away)[gh, ga])
                d = min(depth.get(home, 0), depth.get(away, 0))
                for name, in_bin in BINS:
                    if in_bin(d):
                        scores[name][league].append(float(np.log(max(p, 1e-300))))
                        break
        week += dt.timedelta(days=7)
    print(f"[w={weight_xg}] fits: {n_fits}")
    return scores


def team_depth(db_url: str, league: str, as_of: dt.datetime) -> dict[str, int]:
    """Finished matches per team before as_of (history depth)."""
    with session_scope(db_url) as s:
        rows = s.scalars(
            select(Match).where(Match.league == league,
                                Match.status == "finished",
                                Match.commence_time_utc < as_of)
            .options(selectinload(Match.home_team), selectinload(Match.away_team))
        ).all()
    depth: dict[str, int] = defaultdict(int)
    for m in rows:
        depth[m.home_team.name] += 1
        depth[m.away_team.name] += 1
    return dict(depth)


def market_slope(db_url: str) -> None:
    config = ModelConfig(xi=0.005, weight_xg=0.5)
    app_config = load_config()
    now = dt.datetime.now(dt.UTC)
    market = market_probabilities(db_url, now, set(app_config.books.excluded_books))
    xs, ys = [], []
    models: dict[str, object] = {}
    for _, info in market.items():
        lg = str(info["league"])
        if lg not in models:
            models[lg], _ = fit_league(db_url, lg, now, config)
        model_ = models[lg]
        pm = total_over(model_.score_matrix(str(info["home"]), str(info["away"])), 2.5)  # type: ignore[attr-defined]
        xs.append(pm)
        ys.append(float(info["p_market"]))
    x, y = np.array(xs), np.array(ys)
    slope = float(np.polyfit(x, y, 1)[0])
    corr = float(np.corrcoef(x, y)[0, 1])
    print(f"market-on-model slope: {slope:.3f}, corr {corr:.3f}, n={len(x)}")


def main() -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise SystemExit("set DATABASE_URL to a DB copy")
    for w in (0.5, 1.0):
        scores = run(db_url, w)
        print(f"\n=== mean log-score per history bin, w={w} ===")
        print(f"{'bin':>6} {'n':>6} {'mean':>8}")
        for name, _ in BINS:
            allv = [v for lg in scores[name] for v in scores[name][lg]]
            if allv:
                print(f"{name:>6} {len(allv):>6} {float(np.mean(allv)):>8.3f}")
            else:
                print(f"{name:>6} {0:>6} {'-':>8}")
    market_slope(db_url)


if __name__ == "__main__":
    main()

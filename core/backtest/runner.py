"""Walk-forward runner with an on-disk fitted-model cache (phase 4).

Weekly refits per league; each week predicts the following week's
finished matches. Models are cached under
``<cache_dir>/<league>__<as_of>__<config_hash>.json`` so a rerun (or a
new tuning configuration that shares nothing) reuses fits, and any
config change naturally invalidates through the hash in the filename.

Training data always goes through core/model/dataset with the result lag
(kickoff + lag < as_of) - the loader enforces it in SQL.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterator, Sequence
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.orm import aliased, selectinload

from core.backtest.periods import ensure_window_allowed
from core.config import ModelConfig
from core.db import session_scope
from core.model.dataset import load_quarantined_match_ids
from core.model.dixon_coles import FittedModel, config_hash, fit_league
from core.orm import Match, MatchStat, Team

log = structlog.get_logger(__name__)

DEFAULT_BACKFILL_REPORT = Path("reports/ingest/backfill_report.json")
TOP_LEAGUES = ("premier_league", "la_liga", "serie_a", "bundesliga",
               "ligue_1")
WEEK = dt.timedelta(days=7)
# history-depth bins (matches of the SHALLOWER team in the last 400 days)
DEPTH_WINDOW_DAYS = 400
DEPTH_BINS = (("0", 0, 0), ("1-5", 1, 5), ("6-15", 6, 15), (">15", 16, 10**9))


def load_quarantine_or_empty(
        path: Path = DEFAULT_BACKFILL_REPORT) -> frozenset[int]:
    """B5-quarantined match ids from the ingest report (empty if absent)."""
    if not path.exists():
        return frozenset()
    return frozenset(load_quarantined_match_ids(path))


def week_starts(start: dt.datetime, end: dt.datetime,
                step: dt.timedelta = WEEK) -> Iterator[dt.datetime]:
    cur = start
    while cur < end:
        yield cur
        cur += step


def _cache_path(cache_dir: Path, league: str, as_of: dt.datetime,
                chash: str) -> Path:
    return cache_dir / f"{league}__{as_of.strftime('%Y%m%dT%H%M')}__{chash}.json"


def fitted_model(db_url: str, league: str, as_of: dt.datetime,
                 config: ModelConfig,
                 cache_dir: Path | None = None,
                 excluded_match_ids: frozenset[int] = frozenset(),
                 fixed_params: dict[str, float] | None = None,
                 ) -> tuple[FittedModel, bool]:
    """Fit one (league, as_of); ``cached`` tells whether it came from disk.

    Cache key = league + as_of + config_hash + hash(fixed_params), so
    any config change invalidates the entry by construction.
    """
    chash = config_hash(config)
    if fixed_params:
        digest = hashlib.sha1(
            repr(sorted(fixed_params.items())).encode()).hexdigest()[:10]
        chash = f"{chash}_f{digest}"
    path = None
    if cache_dir is not None:
        path = _cache_path(cache_dir, league, as_of, chash)
        if path.exists():
            try:
                return FittedModel.from_json(path.read_text()), True
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                log.warning("backtest.cache.corrupt", path=str(path),
                            error=str(exc))
    model, _ = fit_league(db_url, league, as_of, config,
                          excluded_match_ids=excluded_match_ids,
                          fixed_params=fixed_params)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(model.to_json())
    return model, False


def week_models(db_url: str, leagues: Sequence[str], as_of: dt.datetime,
                config: ModelConfig, cache_dir: Path | None,
                excluded: frozenset[int],
                ) -> dict[str, FittedModel]:
    """Models for one walk-forward week, applying the phase-4 two-pass
    options (common rho / slower league decay, decisions S3).

    Pass A fits every league (for league decay with xi * multiplier);
    pass B refits with the league-level scalars pinned: a shared
    rho (common_rho) or each league's own mu0/gamma/rho (decay).
    """
    decay = config.league_decay_multiplier != 1.0
    if not (config.common_rho or decay):
        return {lg: fitted_model(db_url, lg, as_of, config, cache_dir,
                                 excluded)[0] for lg in leagues}
    cfg_a = config.model_copy(
        update={"xi": config.xi * config.league_decay_multiplier})         if decay else config
    pass_a = {lg: fitted_model(db_url, lg, as_of, cfg_a, cache_dir,
                               excluded)[0] for lg in leagues}
    fixed_per_league: dict[str, dict[str, float]] = {}
    if config.common_rho:
        total = sum(m.n_matches for m in pass_a.values())
        rho_bar = sum(m.rho * m.n_matches for m in pass_a.values()) / total
        for lg in leagues:
            fixed_per_league[lg] = {"rho": float(rho_bar)}
    if decay:
        for lg, m in pass_a.items():
            fixed_per_league.setdefault(lg, {})
            fixed_per_league[lg].update({
                "mu0": m.intercept_mixed
                if m.intercept_mixed is not None else m.intercept,
                "gamma": m.home_advantage_mixed
                if m.home_advantage_mixed is not None else m.home_advantage,
                "rho": m.rho if not config.common_rho
                else fixed_per_league[lg]["rho"],
            })
    return {lg: fitted_model(db_url, lg, as_of, config, cache_dir,
                             excluded, fixed_params=fixed)[0]
            for lg, fixed in fixed_per_league.items()}


def team_depths(db_url: str, league: str, as_of: dt.datetime,
                window_days: int = DEPTH_WINDOW_DAYS) -> dict[str, int]:
    """Finished matches per team in the last ``window_days`` before as_of
    (no lag subtraction: the depth describes how much RECENT league
    history the team has, not what the loader may use)."""
    lo = as_of - dt.timedelta(days=window_days)
    with session_scope(db_url) as s:
        rows = s.scalars(
            select(Match).where(
                Match.league == league, Match.status == "finished",
                Match.commence_time_utc >= lo,
                Match.commence_time_utc < as_of)
            .options(selectinload(Match.home_team),
                     selectinload(Match.away_team))
        ).all()
    depth: dict[str, int] = {}
    for m in rows:
        depth[m.home_team.name] = depth.get(m.home_team.name, 0) + 1
        depth[m.away_team.name] = depth.get(m.away_team.name, 0) + 1
    return depth


def depth_bin(min_depth: int) -> str:
    for name, lo, hi in DEPTH_BINS:
        if lo <= min_depth <= hi:
            return name
    return ">15"


def finished_week_matches(db_url: str, league: str, lo: dt.datetime,
                          hi: dt.datetime) -> list[tuple]:
    """Scored matches with kickoff in [lo, hi) - the prediction targets."""
    away = aliased(Team)
    with session_scope(db_url) as s:
        rows = s.execute(
            select(Match, Team.name, away.name)
            .join(Team, Match.home_team_id == Team.id)
            .join(away, Match.away_team_id == away.id)
            .where(Match.league == league, Match.status == "finished",
                   Match.commence_time_utc >= lo,
                   Match.commence_time_utc < hi)
            .order_by(Match.commence_time_utc, Match.id)
        ).all()
        ids = [m.id for m, _, _ in rows]
        stats = {st.match_id: (st.home_goals, st.away_goals) for st in
                 s.scalars(select(MatchStat).where(
                     MatchStat.source == "understat",
                     MatchStat.match_id.in_(ids)))
                 if st.home_goals is not None}
    return [(m, h, a, stats[m.id][0], stats[m.id][1]) for m, h, a in rows
            if m.id in stats]


def predictions(db_url: str, config: ModelConfig,
                start: dt.datetime, end: dt.datetime,
                leagues: Sequence[str] = TOP_LEAGUES,
                cache_dir: Path | None = None,
                excluded_match_ids: frozenset[int] = frozenset(),
                ) -> list[dict]:
    """Walk-forward predictions for matches whose kickoff falls in
    [start + 1 week? no - see below].

    Weeks start at ``start`` and step by 7 days; for each week the model
    is fitted at the week start (as_of) and predicts matches with
    kickoff in [as_of, as_of + 7 days). ``end`` bounds the last week.
    Returns one record per predicted match.
    """
    from core.model.markets import btts, total_over

    ensure_window_allowed(start, end)
    records: list[dict] = []
    for as_of in week_starts(start, end):
        models = week_models(db_url, leagues, as_of, config, cache_dir,
                             excluded_match_ids)
        for league, model in models.items():
            depths = team_depths(db_url, league, as_of)
            for m, home, away, gh, ga in finished_week_matches(
                    db_url, league, as_of, as_of + WEEK):
                if home not in model.attacks or away not in model.attacks:
                    continue  # team unknown even after priors (should not happen)
                matrix = model.score_matrix(home, away)
                records.append({
                    "league": league, "season": m.season,
                    "match_id": m.id, "as_of": as_of,
                    "kickoff": m.commence_time_utc,
                    "home": home, "away": away,
                    "home_goals": gh, "away_goals": ga,
                    # probability of the observed exact score
                    "p_score": float(matrix[gh, ga]),
                    "p_over_15": total_over(matrix, 1.5),
                    "p_over_25": total_over(matrix, 2.5),
                    "p_over_35": total_over(matrix, 3.5),
                    "p_btts": btts(matrix, yes=True),
                    "depth_bin": depth_bin(min(depths.get(home, 0),
                                               depths.get(away, 0))),
                })
    return records

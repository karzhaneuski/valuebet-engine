"""Seed of the 82 phase-0 TOA<->Understat team pairs (idempotent)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select

from core.db import get_engine, session_scope
from core.orm import Base, Team, TeamAlias
from scripts.seed_team_aliases import load_pairs, seed

RECON = Path(__file__).resolve().parents[1] / "reports/recon/odds_budget.json"


def make_db(tmp_path):
    url = f"sqlite:///{tmp_path}/seed.db"
    Base.metadata.create_all(get_engine(url))
    return url


def test_report_pairs_present() -> None:
    pairs = load_pairs(RECON)
    assert len(pairs) == 82  # phase-0 finding (odds_budget.json)


def test_seed_creates_teams_and_aliases(tmp_path) -> None:
    url = make_db(tmp_path)
    stats = seed(load_pairs(RECON), url)
    assert stats["teams_created"] >= 80
    assert stats["aliases_created"] == 2 * stats["teams_created"]
    with session_scope(url) as s:
        n_teams = s.scalar(select(func.count()).select_from(Team))
        n_alias = s.scalar(select(func.count()).select_from(TeamAlias))
        assert n_teams == stats["teams_created"]
        assert n_alias == stats["aliases_created"]


def test_seed_idempotent(tmp_path) -> None:
    url = make_db(tmp_path)
    seed(load_pairs(RECON), url)
    second = seed(load_pairs(RECON), url)
    assert second == {"teams_created": 0, "aliases_created": 0}


def test_canonical_name_is_toa(tmp_path) -> None:
    url = make_db(tmp_path)
    seed(load_pairs(RECON), url)
    with session_scope(url) as s:
        # phase-0 example pair: TOA "AS Roma" vs Understat "Roma"
        team = s.scalars(select(Team).where(Team.name == "AS Roma")).first()
        assert team is not None
        alias = s.scalars(select(TeamAlias).where(
            TeamAlias.alias == "Roma", TeamAlias.source == "understat")).first()
        assert alias is not None and alias.team_id == team.id

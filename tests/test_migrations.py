"""Alembic migrations must be reproducible from scratch (review checklist)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sqlalchemy import inspect

REPO = Path(__file__).resolve().parents[1]

EXPECTED_TABLES = {
    "teams", "team_aliases", "matches", "match_stats", "odds_snapshots",
    "player_availability", "prefilter_decisions", "recommendations",
    "results", "api_quota_usage", "quota_counters", "llm_calls",
}


def run_alembic(db_url: str, *args: str) -> None:
    cmd = [sys.executable, "-m", "alembic", "-x", f"db={db_url}", *args]
    result = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_upgrade_head_and_downgrade(tmp_path) -> None:
    db = tmp_path / "mig.db"
    url = f"sqlite:///{db}"

    run_alembic(url, "upgrade", "head")
    from core.db import get_engine
    tables = set(inspect(get_engine(url)).get_table_names())
    assert tables >= EXPECTED_TABLES

    # key indexes/constraints exist after migration
    insp = inspect(get_engine(url))
    match_idx = {i["name"] for i in insp.get_indexes("matches")}
    assert "ix_matches_date_league" in match_idx
    uq = {c["name"] for c in insp.get_unique_constraints("team_aliases")}
    assert "uq_team_alias_source_alias" in uq

    run_alembic(url, "downgrade", "base")
    tables_after = set(inspect(get_engine(url)).get_table_names())
    assert not (EXPECTED_TABLES & tables_after)

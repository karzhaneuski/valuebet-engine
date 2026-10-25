"""Phase-4 review S5: MaxC arbitrage forks and AvgC margin by period.

Reads closing odds (and, once loaded, prematch odds) on a DB copy and
reports: matches where 1/MaxO + 1/MaxU < 1 (a guaranteed-profit fork
that never exists in practice - the MaxC book is NOT executable, any
ROI computed against it is an unreachable upper bound), and the mean
AvgC margin split by period (validation seasons vs test seasons).

Usage:
    DATABASE_URL=sqlite:////tmp/bt3.db uv run python -m scripts.backtest_margin
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from sqlalchemy import select

from core.db import session_scope
from core.orm import ClosingOdds, Match


def main() -> None:
    db = os.environ["DATABASE_URL"]
    by_match: dict[int, dict[str, tuple[float, float]]] = {}
    seasons: dict[int, int] = {}
    with session_scope(db) as s:
        for c in s.scalars(select(ClosingOdds)):
            if c.over_price and c.under_price:
                by_match.setdefault(c.match_id, {})[c.bookmaker] = (
                    float(c.over_price), float(c.under_price))
        for m in s.scalars(select(Match)):
            seasons[m.id] = m.season

    validation_ids = {mid for mid, season in seasons.items()
                      if season in (2023, 2024)}
    test_ids = {mid for mid, season in seasons.items()
                if season in (2025, 2026)}

    def stats(ids: set[int], label: str) -> dict:
        margins, forks, n = [], 0, 0
        for mid in ids:
            books = by_match.get(mid, {})
            if "AvgC" not in books:
                continue
            o, u = books["AvgC"]
            margins.append(1 / o + 1 / u - 1)
            n += 1
            if "MaxC" in books:
                mo, mu = books["MaxC"]
                if 1 / mo + 1 / mu < 1:
                    forks += 1
        return {"period": label, "matches_with_avgc": n,
                "mean_avgc_margin": sum(margins) / len(margins) if margins
                else float("nan"),
                "maxc_forks": forks,
                "fork_share": forks / n if n else float("nan")}

    out = [stats(validation_ids, "validation (2023-24)"),
           stats(test_ids, "test (2025-26)")]
    Path("reports/backtest/part3_margin.json").write_text(
        json.dumps(out, indent=1))
    for row in out:
        print(f"{row['period']}: n={row['matches_with_avgc']} "
              f"mean AvgC margin={row['mean_avgc_margin']:.4f} "
              f"MaxC forks={row['maxc_forks']} ({row['fork_share']:.1%})")


if __name__ == "__main__":
    main()

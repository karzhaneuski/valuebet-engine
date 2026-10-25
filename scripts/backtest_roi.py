"""Phase-4 part 3.2: ROI and CLV on O/U 2.5 against football-data closings.

Reads the TEST-period prediction records (generated after the config
freeze) and the closing_odds table; market probability = de-vigged AvgC
pair (decisions: CLV/backtest on AvgC, not PC). A bet fires when
edge = p_model * price - 1 >= threshold; execution is priced twice:
AvgC (realistic) and MaxC (optimistic upper bound).

Usage (after FROZEN_CONFIG.json exists and test records are generated):
    DATABASE_URL=sqlite:////tmp/bt.db uv run python -m scripts.backtest_roi \
        --records .cache/bt_records/<test-tag>.json \
        --out reports/backtest/part3_roi.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict
from pathlib import Path

import numpy as np
from sqlalchemy import select

from core.backtest.periods import ensure_period_allowed, period_of_season
from core.db import session_scope
from core.orm import ClosingOdds

THRESHOLDS = (0.02, 0.04, 0.06, 0.08)


def load_closings(db_url: str) -> dict[int, dict[str, tuple[float, float]]]:
    """match_id -> bookmaker -> (over, under) decimal prices."""
    with session_scope(db_url) as s:
        rows = s.scalars(select(ClosingOdds)).all()
    out: dict[int, dict[str, tuple[float, float]]] = defaultdict(dict)
    for c in rows:
        if c.over_price is None or c.under_price is None:
            continue
        out[c.match_id][c.bookmaker] = (float(c.over_price),
                                        float(c.under_price))
    return dict(out)


def devig_over(pair: tuple[float, float]) -> float:
    o, u = 1.0 / pair[0], 1.0 / pair[1]
    return o / (o + u)


def bet_outcome(p_model: float, price: float, over: bool,
                gh: int, ga: int) -> float:
    total = gh + ga
    won = (total > 2.5) if over else (total < 2.5)
    return price - 1.0 if won else -1.0


def bootstrap_roi(profit: list[float], weeks: list[str], n: int = 2000,
                  seed: int = 11) -> tuple[float, float]:
    """95% CI of the flat ROI by resampling weeks with replacement."""
    if not profit:
        return float("nan"), float("nan")
    arr = np.array(profit)
    wk = np.array(weeks)
    uniq = np.unique(wk)
    groups = [arr[wk == w] for w in uniq]
    rng = np.random.default_rng(seed)
    means = np.empty(n)
    for i in range(n):
        pick = rng.integers(0, len(groups), len(groups))
        means[i] = np.concatenate([groups[j] for j in pick]).mean()
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(lo), float(hi)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    ensure_period_allowed("test")   # hard gate: this is test-period scoring
    records = json.loads(Path(args.records).read_text())
    records = [r for r in records if period_of_season(r["season"]) == "test"]
    db_url = os.environ["DATABASE_URL"]
    closings = load_closings(db_url)

    rows_out = []
    for exec_book in ("AvgC", "MaxC"):
        bets: list[dict] = []
        for r in records:
            cl = closings.get(r["match_id"], {})
            if "AvgC" not in cl:
                continue
            p_market = devig_over(cl["AvgC"])
            price_pair = cl.get(exec_book)
            if price_pair is None or min(price_pair) <= 1.0:
                continue
            for over, p_model, price in (
                    (True, r["p_over_25"], price_pair[0]),
                    (False, 1.0 - r["p_over_25"], price_pair[1])):
                edge = p_model * price - 1.0
                if edge < 0:
                    continue
                bets.append({
                    "league": r["league"], "as_of": r["as_of"][:10],
                    "match_id": r["match_id"], "over": over,
                    "edge": edge, "price": price, "p_model": p_model,
                    "p_market": p_market,
                    "profit": bet_outcome(p_model, price, over,
                                          r["home_goals"], r["away_goals"]),
                })
        for thr in THRESHOLDS:
            sel = [b for b in bets if b["edge"] >= thr]
            profits = [b["profit"] for b in sel]
            weeks = [f"{b['league']}|{b['as_of']}" for b in sel]
            lo, hi = bootstrap_roi(profits, weeks)
            hit = float(np.mean([p > 0 for p in profits])) if profits \
                else float("nan")
            entry = {
                "execution": exec_book, "threshold": thr,
                "bets": len(sel), "hit_rate": hit,
                "roi": float(np.mean(profits)) if profits else float("nan"),
                "roi_ci95": [lo, hi],
                "n_over": sum(1 for b in sel if b["over"]),
                "n_under": sum(1 for b in sel if not b["over"]),
            }
            for lg in sorted({b["league"] for b in sel}):
                lg_sel = [b["profit"] for b in sel if b["league"] == lg]
                entry[f"roi_{lg}"] = float(np.mean(lg_sel))
                entry[f"n_{lg}"] = len(lg_sel)
            over_p = [b["profit"] for b in sel if b["over"]]
            under_p = [b["profit"] for b in sel if not b["over"]]
            entry["roi_over"] = float(np.mean(over_p)) if over_p else float("nan")
            entry["roi_under"] = float(np.mean(under_p)) if under_p else float("nan")
            rows_out.append(entry)
    Path(args.out).write_text(json.dumps(rows_out, indent=1))
    hdr = (f"{'exec':>5} {'thr':>5} {'bets':>5} {'hit':>6} {'ROI':>7} "
           f"{'CI95':>17} over/under")
    print(hdr)
    for e in rows_out:
        ci = f"[{e['roi_ci95'][0]:+.3f},{e['roi_ci95'][1]:+.3f}]" \
            if not math.isnan(e['roi_ci95'][0]) else "-"
        print(f"{e['execution']:>5} {e['threshold']:>5.2f} {e['bets']:>5} "
              f"{e['hit_rate']:>6.3f} {e['roi']:>+7.3f} {ci:>17} "
              f"{e['n_over']}/{e['n_under']}")


if __name__ == "__main__":
    main()

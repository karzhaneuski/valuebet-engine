"""Phase-4 review S1: model vs de-vigged market on O/U 2.5, honest ROI.

Works on the SAVED test records (frozen config) plus the closing_odds
table on a DB copy - no refits, no new test-period parameters.

Outputs (json + markdown lines to stdout):
- Brier and binary log loss of p_model vs p_market (de-vigged AvgC) on
  O/U 2.5 over the same matches, difference with a 95% CI from a
  calendar-week bootstrap (weeks built from kickoff dates);
- fair ROI -m/(1+m) per AvgC pair, random-side / always-over /
  always-under ROI on the same matches, and the model's own ROI by
  threshold with mean expected win rates (model / market / actual);
- ROI split by side (over/under) and league at the 2% threshold.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
from sqlalchemy import select

from core.backtest.periods import ensure_period_allowed
from core.db import session_scope
from core.orm import ClosingOdds


def week_of(iso: str) -> str:
    # records store kickoff as isoformat; use the ISO calendar week
    import datetime as dt

    d = dt.datetime.fromisoformat(iso)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def brier(p, y):
    return float(np.mean((np.asarray(p) - np.asarray(y)) ** 2))


def logloss(p, y):
    p = np.clip(np.asarray(p), 1e-12, 1 - 1e-12)
    y = np.asarray(y)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def boot_diff_ci(a, b, weeks, n=2000, seed=7):
    """CI95 of mean(a) - mean(b), resampling calendar weeks."""
    a, b, weeks = np.asarray(a), np.asarray(b), np.asarray(weeks)
    uniq = np.unique(weeks)
    groups = [np.where(weeks == w)[0] for w in uniq]
    rng = np.random.default_rng(seed)
    out = np.empty(n)
    for i in range(n):
        idx = np.concatenate([groups[j]
                              for j in rng.integers(0, len(groups), len(groups))])
        out[i] = a[idx].mean() - b[idx].mean()
    lo, hi = np.percentile(out, [2.5, 97.5])
    return float(lo), float(hi)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    ensure_period_allowed("test")

    records = json.loads(Path(args.records).read_text())
    by_match: dict[int, dict[str, tuple[float, float]]] = {}
    with session_scope(os.environ["DATABASE_URL"]) as s:
        for c in s.scalars(select(ClosingOdds)):
            if c.over_price and c.under_price:
                by_match.setdefault(c.match_id, {})[c.bookmaker] = (
                    float(c.over_price), float(c.under_price))
    avg = {mid: pair["AvgC"] for mid, pair in by_match.items()
           if "AvgC" in pair}

    rows = []
    for r in records:
        pair = avg.get(r["match_id"])
        if not pair or min(pair) <= 1.0:
            continue
        o, u = 1.0 / pair[0], 1.0 / pair[1]
        p_mkt = o / (o + u)
        y = 1.0 if r["home_goals"] + r["away_goals"] > 2.5 else 0.0
        rows.append({
            "league": r["league"], "week": week_of(r["kickoff"]),
            "p_model": r["p_over_25"], "p_under_model": 1 - r["p_over_25"],
            "p_mkt_over": p_mkt, "p_mkt_under": 1 - p_mkt,
            "y_over": y, "over_price": pair[0], "under_price": pair[1],
            "margin": o + u - 1.0,
        })
    y = [r["y_over"] for r in rows]
    weeks = [r["week"] for r in rows]
    pm = [r["p_model"] for r in rows]
    pk = [r["p_mkt_over"] for r in rows]

    out: dict = {"n_matches": len(rows)}
    b_m, b_k = brier(pm, y), brier(pk, y)
    out["brier_model"], out["brier_market"] = b_m, b_k
    out["brier_diff_ci95"] = boot_diff_ci(
        [(p - yy) ** 2 for p, yy in zip(pm, y, strict=True)],
        [(p - yy) ** 2 for p, yy in zip(pk, y, strict=True)], weeks)
    ll_m, ll_k = logloss(pm, y), logloss(pk, y)
    out["logloss_model"], out["logloss_market"] = ll_m, ll_k
    def ll_terms(p_list):
        p = np.clip(np.asarray(p_list), 1e-12, 1 - 1e-12)
        yy = np.asarray(y)
        return -(yy * np.log(p) + (1 - yy) * np.log(1 - p))

    out["logloss_diff_ci95"] = boot_diff_ci(ll_terms(pm), ll_terms(pk),
                                            weeks)

    # fair ROI and benchmark strategies on the same matches
    margins = [r["margin"] for r in rows]
    fair_roi = float(np.mean([-m / (1 + m) for m in margins]))
    out["fair_roi_avgc"] = fair_roi
    out["mean_margin_avgc"] = float(np.mean(margins))
    rng = np.random.default_rng(3)
    sides = rng.integers(0, 2, len(rows))  # 1 = over
    random_profit, over_profit, under_profit = [], [], []
    for r, side in zip(rows, sides, strict=True):
        for prof_list, take_over in ((random_profit, bool(side)),
                                     (over_profit, True),
                                     (under_profit, False)):
            p_win = r["y_over"] if take_over else 1 - r["y_over"]
            price = r["over_price"] if take_over else r["under_price"]
            prof_list.append(price - 1.0 if p_win else -1.0)
    out["roi_random_side"] = float(np.mean(random_profit))
    out["roi_always_over"] = float(np.mean(over_profit))
    out["roi_always_under"] = float(np.mean(under_profit))

    # the model's own bets (AvgC execution), with expectation audit
    thresholds = (0.0, 0.02, 0.04, 0.06, 0.08)
    per_threshold = []
    for thr in thresholds:
        bets = []
        for r in rows:
            for take_over, p, price in ((True, r["p_model"],
                                         r["over_price"]),
                                        (False, r["p_under_model"],
                                         r["under_price"])):
                p_mkt = r["p_mkt_over"] if take_over else r["p_mkt_under"]
                edge = p * price - 1.0
                if edge >= thr:
                    win = r["y_over"] if take_over else 1 - r["y_over"]
                    bets.append({"p_model": p, "p_mkt": p_mkt,
                                 "win": win, "profit": price - 1.0 if win
                                 else -1.0,
                                 "over": take_over, "league": r["league"],
                                 "week": r["week"]})
        if not bets:
            continue
        profits = [b["profit"] for b in bets]
        bw = [b["week"] for b in bets]
        rng2 = np.random.default_rng(11)
        uniq = np.unique(bw)
        groups = [np.asarray(profits)[np.asarray(bw) == w] for w in uniq]
        boots = np.empty(2000)
        for i in range(2000):
            pick = rng2.integers(0, len(groups), len(groups))
            boots[i] = np.concatenate([groups[j]
                                       for j in pick]).mean()
        lo, hi = np.percentile(boots, [2.5, 97.5])
        entry = {
            "threshold": thr, "bets": len(bets),
            "roi": float(np.mean(profits)), "roi_ci95": [float(lo), float(hi)],
            "mean_p_model": float(np.mean([b["p_model"] for b in bets])),
            "mean_p_market": float(np.mean([b["p_mkt"] for b in bets])),
            "actual_win_rate": float(np.mean([b["win"] for b in bets])),
            "roi_minus_fair": float(np.mean(profits)) - fair_roi,
        }
        for side_name, flag in (("over", True), ("under", False)):
            sub = [b["profit"] for b in bets if b["over"] == flag]
            entry[f"n_{side_name}"] = len(sub)
            entry[f"roi_{side_name}"] = float(np.mean(sub)) if sub \
                else float("nan")
        for lg in sorted({b["league"] for b in bets}):
            sub = [b["profit"] for b in bets if b["league"] == lg]
            entry[f"roi_{lg}"] = float(np.mean(sub))
            entry[f"n_{lg}"] = len(sub)
        per_threshold.append(entry)
    out["model_bets_avgc"] = per_threshold

    Path(args.out).write_text(json.dumps(out, indent=1))
    print(json.dumps({k: v for k, v in out.items()
                      if k != "model_bets_avgc"}, indent=1))
    for e in out["model_bets_avgc"]:
        print(f"thr {e['threshold']:.2f}: bets {e['bets']} "
              f"roi {e['roi']:+.4f} (fair {e['roi_minus_fair']:+.4f}) "
              f"E[p_model] {e['mean_p_model']:.3f} "
              f"E[p_mkt] {e['mean_p_market']:.3f} "
              f"actual {e['actual_win_rate']:.3f}")


if __name__ == "__main__":
    main()

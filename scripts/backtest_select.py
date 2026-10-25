"""Phase-4 model selection (part 2.2): paired bootstrap over weeks.

Compares the top tuning configurations on their cached validation
records: the difference of mean exact-score log loss between two tags is
assessed with a WEEK-clustered bootstrap (resample as_of weeks with
replacement), which respects the correlation of matches inside a week.

Decision rule (agreed in the phase-4 task): if |dLL| between the best
and a challenger is within 1.96*SE(dLL), prefer the SIMPLER config
(fewer switched-on options, higher prior_sigma is not simpler, w=0.5
over w=0.3/0.7 ties go to the more central value).

Usage:
    uv run python -m scripts.backtest_select --tags a b [c ...] \
        [--out reports/backtest/part2_selection.json]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

RECORDS = Path(".cache/bt_records")


def load(tag: str) -> list[dict]:
    return json.loads((RECORDS / f"{tag}.json").read_text())


def mean_ll(rec: list[dict]) -> float:
    import math

    return float(-sum(math.log(max(r["log_p"], 1e-300)) for r in rec)
                 / len(rec))


def paired_delta_boot(rec_a: list[dict], rec_b: list[dict],
                      n_boot: int = 2000, seed: int = 7,
                      ) -> tuple[float, float]:
    """Mean LL(a) - LL(b) on the common matches + bootstrap SE (weeks)."""
    by_id_a = {r["match_id"]: r for r in rec_a}
    common = [(by_id_a[r["match_id"]], r) for r in rec_b
              if r["match_id"] in by_id_a]
    import math

    la = np.array([-math.log(max(x["log_p"], 1e-300)) for x, _ in common])
    lb = np.array([-math.log(max(y["log_p"], 1e-300)) for _, y in common])
    weeks = np.array([f"{x['league']}|{x['as_of'][:10]}" for x, _ in common])
    uniq = np.unique(weeks)
    groups = [np.where(weeks == w)[0] for w in uniq]
    rng = np.random.default_rng(seed)
    deltas = np.empty(n_boot)
    for i in range(n_boot):
        pick = rng.integers(0, len(groups), len(groups))
        idx = np.concatenate([groups[j] for j in pick])
        deltas[i] = la[idx].mean() - lb[idx].mean()
    return float(la.mean() - lb.mean()), float(deltas.std(ddof=1))


SIMPLICITY = {
    # lower = simpler: baseline flags first, then single options
    "opt_base_xi0.002_w0.5": 0,
    "xiw_xi0.002_w0.5": 0,
    "opt_xgscale": 1,
    "opt_commonrho": 1,
    "opt_levelgoals": 1,
    "opt_decay0.5": 1,
    "opt_decay0.25": 1,
    "sig0.75_xi0.002_w0.5": 0,
    "sig0.75_xi0.002_w0.3": 0,
    "xiw_xi0.002_w0.3": 0,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tags", nargs="+", required=True)
    parser.add_argument("--out", default="reports/backtest/part2_selection.json")
    args = parser.parse_args()

    recs = {t: load(t) for t in args.tags}
    stats = {t: {"n": len(recs[t]), "log_loss": mean_ll(recs[t]),
                 "bin_1_5": None} for t in args.tags}
    for t, rec in recs.items():
        sub = [r for r in rec if r["depth_bin"] == "1-5"]
        stats[t]["bin_1_5"] = mean_ll(sub) if sub else float("nan")

    ranked = sorted(args.tags, key=lambda t: stats[t]["log_loss"])
    best = ranked[0]
    comparisons = []
    for challenger in ranked[1:]:
        delta, se = paired_delta_boot(recs[best], recs[challenger])
        comparisons.append({
            "best": best, "challenger": challenger,
            "delta_log_loss": delta, "se": se,
            "ci95": [delta - 1.96 * se, delta + 1.96 * se],
            "indistinguishable": abs(delta) < 1.96 * se,
            "simpler_challenger":
                SIMPLICITY.get(challenger, 99) < SIMPLICITY.get(best, 99),
        })
    chosen = best
    for comp in comparisons:
        if comp["indistinguishable"] and comp["simpler_challenger"]:
            chosen = comp["challenger"]
            break
    out = {"stats": stats, "ranked": ranked, "comparisons": comparisons,
           "chosen": chosen}
    Path(args.out).write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()

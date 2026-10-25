"""Phase-4 tuning grids (validation only).

Runs scripts.backtest_run.run for each configuration and appends one row
per config to reports/backtest/grid_<name>.json. Records are cached
under .cache/bt_records/<tag>.json, so a rerun resumes.

Usage:
    DATABASE_URL=sqlite:////tmp/bt.db uv run python -m scripts.backtest_grid --stage xiw
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.backtest_run import run

REPORTS = Path("reports/backtest")

STAGES: dict[str, list[tuple[str, dict]]] = {
    "xiw": [(f"xiw_xi{xi}_w{w}", {"xi": xi, "weight_xg": w})
            for xi in (0.002, 0.0035, 0.005, 0.0075)
            for w in (0.3, 0.5, 0.7, 1.0)],
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--configs", default=None,
                        help="explicit JSON list of (tag, overrides)")
    args = parser.parse_args()
    jobs = [(d["tag"], d["ov"]) for d in json.loads(args.configs)] \
        if args.configs else STAGES[args.stage]

    REPORTS.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS / f"grid_{args.stage}.jsonl"
    done = {json.loads(line)["tag"]
            for line in out_path.read_text().splitlines()} \
        if out_path.exists() else set()
    for tag, overrides in jobs:
        if tag in done:
            print(f"skip {tag} (already in {out_path})")
            continue
        summary = run(tag, "validation", overrides)
        row = {"tag": tag, "overrides": overrides,
               "n": summary["n"], "log_loss": summary["log_loss"],
               "bins": {k: v["mean"] for k, v in summary["bins"].items()
                        if v["n"]},
               "bin_n": {k: v["n"] for k, v in summary["bins"].items()},
               "brier_ou25": summary["ou_brier"]["p_over_25"]["brier"]}
        with out_path.open("a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        print(f"{tag}: log_loss {row['log_loss']:.4f} "
              f"bin1-5 {row['bins'].get('1-5', float('nan')):.4f}")


if __name__ == "__main__":
    main()

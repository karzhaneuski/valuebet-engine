"""Phase-4 report generator: markdown + reliability PNG from records.

Usage:
    DATABASE_URL=... uv run python -m scripts.backtest_report \
        --records .cache/bt_records/baseline.json --out reports/backtest/part1_baseline.md \
        --png reports/backtest/part1_reliability.png --title "Part 1 baseline"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.backtest.metrics import (
    OVER_LINES,
    binary_metrics,
    reliability,
    summarize,
)

BIN_ORDER = ("0", "1-5", "6-15", ">15")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--png", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--extra", default="",
                        help="extra markdown lines appended to the report")
    args = parser.parse_args()

    records = json.loads(Path(args.records).read_text())
    summary = summarize(records)

    lines = [f"# {args.title}", "",
             f"- predictions: {summary['n']}",
             f"- exact-score log loss: {summary['log_loss']:.4f}",
             "", "## Log loss by history bin (shallower team, 400d)", "",
             "| bin | n | mean log loss |", "|---|---|---|"]
    for name in BIN_ORDER:
        b = summary["bins"][name]
        if b["n"]:
            lines.append(f"| {name} | {b['n']} | {b['mean']:.4f} |")
        else:
            lines.append(f"| {name} | 0 | - |")
    lines += ["", "## Brier scores", "",
              "| market | n | Brier | base rate |", "|---|---|---|---|"]
    for key, line in OVER_LINES:
        m = summary["ou_brier"][key]
        lines.append(f"| O/U {line} | {m['n']} | {m['brier']:.4f} | "
                     f"{m['base_rate']:.3f} |")
    m = summary["btts_brier"]
    lines.append(f"| BTTS | {m['n']} | {m['brier']:.4f} | "
                 f"{m['base_rate']:.3f} |")
    lines += ["", "## Per league", "",
              "| league | n | log loss |", "|---|---|---|"]
    for lg, sub in summary["by_league"].items():
        lines.append(f"| {lg} | {sub['n']} | {sub['log_loss']:.4f} |")
    if args.extra:
        lines += ["", args.extra]
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")

    # reliability figure: 2x2 panels
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 2, figsize=(10, 8), sharex=True, sharey=True)
    panels = [("p_over_15", "Over 1.5"), ("p_over_25", "Over 2.5"),
              ("p_over_35", "Over 3.5"), ("p_btts", "BTTS")]
    for ax, (key, title) in zip(axes.flat, panels, strict=True):
        pts = reliability(records, key)
        xs = [(p["bin_lo"] + p["bin_hi"]) / 2 for p in pts]
        ns = [p["n"] for p in pts]
        ys = [p["observed"] for p in pts]
        sizes = [max(12, 2.2 * n ** 0.5) for n in ns]
        ax.plot([0, 1], [0, 1], "k--", lw=0.8, label="perfect")
        ax.scatter(xs, ys, s=sizes, alpha=0.7)
        ax.set_title(title)
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        brier = binary_metrics(records, key)["brier"]
        ax.text(0.02, 0.94, f"Brier {brier:.4f} (n={len(records)})",
                transform=ax.transAxes, fontsize=9,
                verticalalignment="top")
        if ax.get_subplotspec().is_first_col():
            ax.set_ylabel("observed")
        if ax.get_subplotspec().is_last_row():
            ax.set_xlabel("predicted")
    fig.suptitle(args.title)
    fig.tight_layout()
    fig.savefig(args.png, dpi=130)
    print(f"report: {args.out}\npng: {args.png}")


if __name__ == "__main__":
    main()

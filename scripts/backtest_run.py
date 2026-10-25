"""Phase-4 backtest runner: walk-forward predictions + metrics report.

Runs the weekly walk-forward over the given period, caches fitted models
on disk (see core.backtest.runner) and stores the prediction records as
JSON so later steps (tuning, final test) reuse them without refitting.

Usage:
    DATABASE_URL=sqlite:////tmp/bt.db uv run python -m scripts.backtest_run \
        --tag baseline --period validation [--config-overrides '{"xi":0.0035}']
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
from pathlib import Path

from core.backtest.metrics import score_records, summarize
from core.backtest.periods import ensure_period_allowed
from core.backtest.runner import (
    TOP_LEAGUES,
    load_quarantine_or_empty,
    predictions,
)
from core.config import load_config

CACHE_DIR = Path(".cache/bt_models")
RECORDS_DIR = Path(".cache/bt_records")
REPORT_DIR = Path("reports/backtest")

PERIOD_BOUNDS = {
    # walk-forward window per period: full seasons, one week of slack
    "validation": (dt.datetime(2023, 8, 1, 6, tzinfo=dt.UTC),
                   dt.datetime(2025, 6, 30, 6, tzinfo=dt.UTC)),
    "test": (dt.datetime(2025, 8, 1, 6, tzinfo=dt.UTC),
             dt.datetime(2026, 10, 25, 6, tzinfo=dt.UTC)),
}


def run(tag: str, period: str, overrides: dict | None = None) -> dict:
    ensure_period_allowed(period)
    config = load_config().model
    if overrides:
        config = config.model_copy(
            update={k: v for k, v in overrides.items() if k != "flags"})
    db_url = os.environ["DATABASE_URL"]
    start, end = PERIOD_BOUNDS[period]
    quarantine = load_quarantine_or_empty()

    records_path = RECORDS_DIR / f"{tag}.json"
    if records_path.exists():
        records = json.loads(records_path.read_text())
        for r in records:  # migrate phase-4 field name
            r.setdefault("p_score", r.pop("log_p", None))
        print(f"[{tag}] loaded {len(records)} cached records")
    else:
        t0 = time.perf_counter()
        records = predictions(db_url, config, start, end,
                              leagues=TOP_LEAGUES, cache_dir=CACHE_DIR,
                              excluded_match_ids=quarantine)
        fit_seconds = time.perf_counter() - t0
        RECORDS_DIR.mkdir(parents=True, exist_ok=True)
        records_path.write_text(json.dumps(records, default=str))
        n_fits = len({(r["league"], r["as_of"]) for r in records})
        print(f"[{tag}] {len(records)} predictions in {fit_seconds:.1f}s "
              f"({n_fits} (league, as_of) groups with matches)")
    subset = score_records(records, period)
    summary = summarize(subset)
    summary["tag"] = tag
    summary["period"] = period
    summary["config"] = json.loads(config.model_dump_json())
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--period", choices=("validation", "test"),
                        default="validation")
    parser.add_argument("--config-overrides", default=None,
                        help="JSON dict of ModelConfig overrides")
    args = parser.parse_args()
    overrides = json.loads(args.config_overrides) \
        if args.config_overrides else None
    summary = run(args.tag, args.period, overrides)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"run_{args.tag}.json"
    out.write_text(json.dumps(summary, indent=1, default=str))
    print(json.dumps({k: v for k, v in summary.items()
                      if k in ("n", "log_loss", "bins", "tag", "period")},
                     indent=1, default=str))


if __name__ == "__main__":
    main()

"""Scheduler service with APScheduler background worker and FastAPI health endpoints (SPEC §9).

Supports:
- FastAPI endpoints: /health, /status, /run-sync
- CLI execution: python -m core.scheduler.main [--dry-run]
- Background jobs: daily history refresh, prefiltering, odds snapshotting, news window search,
  and match settlement with pre-kickoff CLV calculation.
"""

from __future__ import annotations

import argparse
import sys
import threading
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore[import-untyped]
from fastapi import FastAPI, HTTPException, Request
from fastapi import status as http_status
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from core.config import AppConfig, Secrets, load_config
from core.http import register_runtime_secrets
from core.logging import configure, get_logger
from core.scheduler.tasks import (
    job_poll_events,
    job_poll_odds_snapshots,
    job_prefilter_candidates,
    job_refresh_history,
    job_search_news_windows,
    job_settle_matches_and_clv,
    run_all_jobs_sync,
)

configure()
register_runtime_secrets()
logger = get_logger("scheduler")

app = FastAPI(title="ValueBet Engine Scheduler", version="0.2.0")

_cfg: AppConfig | None = None
_session_factory: sessionmaker[Session] | None = None
_scheduler: BackgroundScheduler | None = None
_sync_lock = threading.Lock()


def get_db_factory(cfg: AppConfig | None = None) -> sessionmaker[Session]:
    global _session_factory, _cfg
    if _session_factory is None:
        _cfg = cfg or load_config()
        engine = create_engine(_cfg.database.url)
        _session_factory = sessionmaker(bind=engine)
    return _session_factory


@app.get("/health")
def health() -> dict[str, str]:
    logger.info("health.check")
    return {"status": "ok", "service": "scheduler"}


@app.get("/status")
def status() -> dict[str, Any]:
    running = _scheduler is not None and _scheduler.running
    return {
        "status": "running" if running else "stopped",
        "service": "scheduler",
        "jobs_count": len(_scheduler.get_jobs()) if _scheduler else 0,
    }


@app.post("/run-sync")
def run_sync(request: Request, dry_run: bool = True) -> dict[str, Any]:
    # 1. Localhost only check
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        logger.warning("Unauthorized external access attempt to /run-sync from %s", client_host)
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="Forbidden: /run-sync is only accessible from localhost",
        )

    # 2. Token verification
    auth_header = request.headers.get("Authorization", "")
    received_secret = auth_header.removeprefix("Bearer ").strip()
    sec = Secrets()
    expected = sec.api_secret_token or sec.api_token or "test-sync-token"
    if not expected or received_secret != expected:
        logger.warning("Invalid authorization token for /run-sync")
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: invalid secret token",
        )

    # 3. Concurrency lock
    acquired = _sync_lock.acquire(blocking=False)
    if not acquired:
        logger.warning("Concurrent /run-sync execution blocked by active lock")
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Conflict: scheduled sync is already running",
        )

    try:
        factory = get_db_factory()
        return run_all_jobs_sync(factory, load_config(), dry_run=dry_run)
    finally:
        _sync_lock.release()


def setup_background_scheduler(
    session_factory: sessionmaker[Session],
    config: AppConfig | None = None,
) -> BackgroundScheduler:
    """Create and configure APScheduler instance with recurring cron triggers."""
    cfg = config or load_config()
    sched = BackgroundScheduler(timezone=cfg.scheduler.timezone)

    # 1. Daily history & xG refresh
    sched.add_job(
        lambda: job_refresh_history(session_factory, config=cfg, dry_run=False),
        "cron",
        hour=5,
        minute=30,
        id="refresh_history",
        replace_existing=True,
    )

    # 2. Free TOA /events polling every 6 hours
    sched.add_job(
        lambda: job_poll_events(session_factory, config=cfg, dry_run=False),
        "cron",
        hour="*/6",
        minute=0,
        id="poll_events",
        replace_existing=True,
    )

    # 3. Paid TOA odds snapshots polling (step <= 15 min per SPEC §9)
    sched.add_job(
        lambda: job_poll_odds_snapshots(session_factory, config=cfg, dry_run=False),
        "interval",
        minutes=15,
        id="poll_odds",
        replace_existing=True,
    )

    # 3. Weekly prefilter candidates
    sched.add_job(
        lambda: job_prefilter_candidates(session_factory, cfg, dry_run=False),
        "cron",
        day_of_week="mon",
        hour=7,
        id="prefilter_candidates",
        replace_existing=True,
    )

    # 4. Hourly news window checks
    sched.add_job(
        lambda: job_search_news_windows(session_factory, None, cfg, dry_run=False),
        "interval",
        minutes=60,
        id="search_news_windows",
        replace_existing=True,
    )

    # 5. Settlement & CLV calculation
    sched.add_job(
        lambda: job_settle_matches_and_clv(session_factory, cfg, dry_run=False),
        "interval",
        minutes=30,
        id="settle_matches_and_clv",
        replace_existing=True,
    )

    return sched


def main() -> None:
    parser = argparse.ArgumentParser(description="ValueBet Engine Scheduler Service")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all scheduled jobs once synchronously without persistent mutations and exit",
    )
    args = parser.parse_args()

    cfg = load_config()
    factory = get_db_factory(cfg)

    if args.dry_run:
        logger.info("Executing scheduler in --dry-run mode...")
        out = run_all_jobs_sync(factory, cfg, dry_run=True)
        logger.info("Dry-run execution results: %s", out)
        print("Dry run completed successfully:")
        for job_name, res in out.items():
            print(f"  [{job_name}]: {res.get('status')} -> {res}")
        sys.exit(0)

    # Regular background execution
    scheduler = setup_background_scheduler(factory, cfg)
    scheduler.start()
    logger.info("Scheduler service running with %d jobs registered.", len(scheduler.get_jobs()))

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)


if __name__ == "__main__":
    main()

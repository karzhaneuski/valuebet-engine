"""Freshness detection: comparing news publication time with market odds snapshot timestamp.

SPEC §6, decisions S5:
If published_at of an impactful player absence is strictly later than the captured_at/snapshot_time
of the Pinnacle closing/prematch odds, a warning banner is shown:
'⚡ Свежая новость: рынок мог не успеть учесть отсутствие игрока X, обновите снимок'.
"""

from __future__ import annotations

import datetime as dt

from core.news.extractor import NewsItem


def normalize_to_utc(dt_val: dt.datetime | None) -> dt.datetime | None:
    """Ensure datetime is UTC timezone-aware."""
    if dt_val is None:
        return None
    if dt_val.tzinfo is None:
        return dt_val.replace(tzinfo=dt.UTC)
    return dt_val.astimezone(dt.UTC)


def is_news_newer_than_market(
    published_at: dt.datetime | None,
    snapshot_time: dt.datetime | None,
) -> bool:
    """Return True if published_at is strictly newer than snapshot_time.

    If either timestamp is missing, returns False (conservative, no false alarms).
    """
    if published_at is None or snapshot_time is None:
        return False

    pub_utc = normalize_to_utc(published_at)
    snap_utc = normalize_to_utc(snapshot_time)
    if pub_utc is None or snap_utc is None:
        return False

    return pub_utc > snap_utc


def get_freshness_warnings(
    news_items: list[NewsItem],
    snapshot_time: dt.datetime | None,
) -> list[str]:
    """Generate warning banners for news items published strictly after the odds snapshot.

    Only flags impactful statuses (out, suspended, doubtful).
    """
    if snapshot_time is None:
        return []

    warnings: list[str] = []
    impactful_statuses = {"out", "suspended", "doubtful"}

    for item in news_items:
        if item.status in impactful_statuses and is_news_newer_than_market(
            item.published_at, snapshot_time
        ):
            warnings.append(
                f"⚡ Свежая новость: рынок мог не успеть учесть отсутствие игрока {item.player}, "
                "обновите снимок"
            )

    return warnings

"""Recon (review B1/S5): The Odds API budget simulation on real events.

Inputs (all fetched here, no manual data):
- TOA /events for the 6 sport keys (free — credit deltas recorded, must be 0);
- Understat current-season schedule via soccerdata (whole season incl.
  future), aligned to TOA commence times to measure the Understat
  naive-datetime -> UTC offset (review S5) and team-name pairs.

Known data quirk (S5): Understat lists far-future rounds with placeholder
kickoff times (whole round at one default time). Real kickoff spread
(Fri-Mon) is only known where TOA lists events. Therefore the simulation
runs two event-time variants:
  - "asis": Understat placeholder times untouched (lower bound);
  - "retimed" (primary): placeholder rounds (>=6 matches at one identical
    time) are re-timed deterministically (seeded RNG) using the empirical
    (weekday, kickoff) distribution of real rounds from TOA + near Understat
    rounds. Retimed rounds are flagged and counted in the report.

Simulation: minute-by-minute over a 28-day window from the run date, plus a
second 28-day window without an international break (break detected from
Understat league dates; gap >= 7 days without any top-5 match). Policies:
  a) flat 6h          b) flat 12h          c) events-gated 6h with 72h horizon
  d) snapshots T-48/T-24/T-3 (tolerances [40,56]/[18,30]/[1,4] h), merged per
     sport: one /odds call covers all events of that sport
  e) d + T-1h (window [0.5, 2] h — assumed, review did not specify)
  f) d + T-72h (window [64, 80] h — assumed)
Cost model (measured): 1 credit = markets(1) x regions(1) per /odds call,
independent of the number of events or the horizon.

UCL note: Understat has no UCL; TOA /events lists only the near horizon.
Matchdays beyond it are ESTIMATED (flagged) by extending the observed
matchday cadence (default 14 days).

Writes reports/recon/odds_budget.json and reports/recon/toa_events.json.
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import soccerdata

sys.path.insert(0, str(Path(__file__).parent))
from envutil import has, load_env, register_secrets, scrub  # noqa: E402

OUT = Path("reports/recon/odds_budget.json")
EVENTS_OUT = Path("reports/recon/toa_events.json")
BASE = "https://api.the-odds-api.com/v4"
CACHE = Path(".cache/understat")
WINDOW_DAYS = 28
BREAK_GAP_DAYS = 7
POLICIES = ("a_flat_6h", "b_flat_12h", "c_gated_6h_72h",
            "d_snapshots", "e_snapshots_plus_t1h", "f_snapshots_plus_t72",
            "d_snapshots_day_slots")

SPORTS = {
    "soccer_epl": "ENG-Premier League",
    "soccer_spain_la_liga": "ESP-La Liga",
    "soccer_italy_serie_a": "ITA-Serie A",
    "soccer_germany_bundesliga": "GER-Bundesliga",
    "soccer_france_ligue_one": "FRA-Ligue 1",
    "soccer_uefa_champs_league": None,
}
TIERS = {
    "d": [(48, 40, 56), (24, 18, 30), (3, 1, 4)],
    "e": [(48, 40, 56), (24, 18, 30), (3, 1, 4), (1, 0.5, 2)],
    "f": [(48, 40, 56), (24, 18, 30), (3, 1, 4), (72, 64, 80)],
}


def norm(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if t}


def teams_match(a: str, b: str) -> bool:
    na, nb = norm(a), norm(b)
    return bool(na & nb) or na <= nb or nb <= na


def fetch_toa_events(session: requests.Session, key: str) -> tuple[dict, list]:
    events: dict[str, list[dict]] = {}
    deltas: list[dict] = []
    used = None
    for sport in SPORTS:
        resp = session.get(f"{BASE}/sports/{sport}/events",
                           params={"apiKey": key, "regions": "eu",
                                   "dateFormat": "iso"}, timeout=30)
        after = resp.headers.get("x-requests-used")
        delta = int(after) - used if (after and used is not None) else 0
        used = int(after) if after else used
        deltas.append({"sport": sport, "credits_delta": delta})
        time.sleep(0.8)
        if resp.status_code == 200:
            events[sport] = [
                {"id": e["id"], "home": e["home_team"], "away": e["away_team"],
                 "start": datetime.fromisoformat(
                     e["commence_time"].replace("Z", "+00:00"))}
                for e in resp.json()
            ]
    return events, deltas


def load_understat() -> dict[str, list[dict]]:
    reader = soccerdata.Understat(
        leagues=[lg for lg in SPORTS.values() if lg], seasons=[2026],
        data_dir=CACHE,
    )
    sched = reader.read_schedule().reset_index()
    by_league: dict[str, list[dict]] = defaultdict(list)
    for _, row in sched.iterrows():
        by_league[str(row["league"])].append(
            {"home": str(row["home_team"]), "away": str(row["away_team"]),
             "naive_start": row["date"].to_pydatetime().replace(tzinfo=None),
             "is_result": bool(row["is_result"])}
        )
    return by_league


def align(understat: dict[str, list[dict]], toa: dict[str, list[dict]]) -> dict:
    """Match Understat matches to TOA events (best |delta| in +/-48h); S5."""
    pairs: list[dict] = []
    for sport, league in SPORTS.items():
        if not league:
            continue
        for ev in toa.get(sport, []):
            toa_naive = ev["start"].astimezone(timezone.utc).replace(tzinfo=None)
            best: tuple[dict, float] | None = None
            for m in understat.get(league, []):
                delta_min = (m["naive_start"] - toa_naive).total_seconds() / 60
                if abs(delta_min) <= 48 * 60 \
                        and teams_match(ev["home"], m["home"]) \
                        and teams_match(ev["away"], m["away"]):
                    if best is None or abs(delta_min) < abs(best[1]):
                        best = (m, delta_min)
            if best:
                pairs.append({"sport": sport, "toa_home": ev["home"],
                              "understat_home": best[0]["home"],
                              "delta_minutes": round(best[1])})
    deltas = sorted(p["delta_minutes"] for p in pairs)
    median = deltas[len(deltas) // 2] if deltas else 0
    zero_share = (round(sum(d == 0 for d in deltas) / len(deltas), 3)
                  if deltas else None)
    return {
        "n_matched": len(pairs),
        "median_delta_minutes": median,
        "share_delta_zero": zero_share,
        "delta_min": deltas[0] if deltas else None,
        "delta_max": deltas[-1] if deltas else None,
        "nonzero_deltas": [d for d in deltas if d != 0],
        "interpretation": "Understat naive datetimes are UTC when this "
                          "offset is 0. Nonzero deltas cluster at -24..-30 h "
                          "(-1425..-1785 min) — these are placeholder DAYS "
                          "(Understat places far rounds on a default day "
                          "while the real round spans Fri-Mon), not "
                          "postponements; small deltas (minutes..hours) are "
                          "kickoff-time drift or short postponements.",
        "team_name_pairs": sorted({(p["toa_home"], p["understat_home"])
                                   for p in pairs}),
        "pairs": pairs,
    }


# --- placeholder detection and re-timing -----------------------------------

def saturdate(d: date) -> date:
    """The Saturday anchoring the round that a date belongs to."""
    wd = d.weekday()  # Mon=0 .. Sun=6
    if wd == 4:
        return d + timedelta(days=1)      # Friday opens the weekend round
    if wd == 5:
        return d
    if wd == 6:
        return d - timedelta(days=1)      # Sunday
    if wd == 0:
        return d - timedelta(days=2)      # Monday closes the round
    return d + timedelta(days=5 - wd)     # rare midweek rounds


def sample_to_date(sat: date, weekday: int) -> date:
    if weekday == 4:
        return sat - timedelta(days=1)
    if weekday == 5:
        return sat
    if weekday == 6:
        return sat + timedelta(days=1)
    if weekday == 0:
        return sat + timedelta(days=2)
    return sat - timedelta(days=5 - weekday)


def build_pool(samples: list[tuple[int, object]]) -> Counter:
    """Empirical (weekday, kickoff) distribution from non-placeholder rounds."""
    by_date: dict[date, list[tuple[int, object]]] = defaultdict(list)
    for wd, dt in samples:
        by_date[dt.date()].append((wd, dt))
    pool: Counter = Counter()
    for _, group in by_date.items():
        if len(group) >= 6 and len({dt.time() for _, dt in group}) == 1:
            continue  # placeholder round — exclude from the pool
        for wd, dt in group:
            pool[(wd, dt.time())] += 1
    return pool


def retime_placeholders(league: str, events: list[dict], pool: Counter):
    by_date: dict[date, list[dict]] = defaultdict(list)
    for e in events:
        by_date[e["start"].date()].append(e)
    out: list[dict] = []
    n_rounds = 0
    for d in sorted(by_date):
        group = by_date[d]
        if len(group) >= 6 and len({e["start"].time() for e in group}) == 1 \
                and sum(pool.values()) >= 15:
            rng = random.Random(f"{league}:{d}")
            keys = sorted(pool)
            drawn = sorted(rng.choices(keys, weights=[pool[k] for k in keys],
                                       k=len(group)))
            sat = saturdate(d)
            group_sorted = sorted(group, key=lambda e: e["home"])
            for e, (wd, tm) in zip(group_sorted, drawn):
                out.append({**e, "start": datetime.combine(
                    sample_to_date(sat, wd), tm, tzinfo=timezone.utc),
                    "retimed": True})
            n_rounds += 1
        else:
            out.extend(group)
    out.sort(key=lambda e: e["start"])
    return out, n_rounds


# --- event merging ----------------------------------------------------------

def merged_events(sport: str, league: str | None, toa_events: list[dict],
                  understat: dict[str, list[dict]], offset_min: int,
                  win_start: datetime, win_end: datetime, retime: bool):
    events = [dict(e) for e in toa_events
              if win_start <= e["start"] <= win_end]
    stats = {"n_toa": len(events), "n_understat_added": 0,
             "n_retimed_rounds": 0}
    if league:
        toa_naive_max = max(
            (e["start"].replace(tzinfo=None) for e in toa_events), default=None)
        offset = timedelta(minutes=offset_min)
        added: list[dict] = []
        for m in understat.get(league, []):
            start = (m["naive_start"] - offset).replace(tzinfo=timezone.utc)
            beyond = toa_naive_max is None or \
                m["naive_start"] > toa_naive_max + timedelta(hours=12)
            if beyond and win_start <= start <= win_end:
                added.append({"id": f"understat:{m['home']}/{m['naive_start']}",
                              "home": m["home"], "away": m["away"],
                              "start": start})
        if retime and added:
            pool_samples = [
                (m["naive_start"].weekday(), m["naive_start"])
                for m in understat[league]
            ] + [
                (e["start"].weekday(), e["start"].replace(tzinfo=None))
                for e in toa_events
            ]
            pool = build_pool(pool_samples)
            added, n_rounds = retime_placeholders(league, added, pool)
            stats["n_retimed_rounds"] = n_rounds
        events += added
        stats["n_understat_added"] = len(added)
    events.sort(key=lambda e: e["start"])
    return events, stats


def break_windows(understat: dict[str, list[dict]]) -> list[tuple[date, date]]:
    """International-break windows: gaps of >= BREAK_GAP_DAYS days without any
    top-5 league match anywhere in the season calendar (open interval)."""
    days = league_match_days(
        understat,
        datetime(2000, 1, 1), datetime(2100, 1, 1))
    windows = []
    for a, b in zip(days, days[1:]):
        if (b - a).days >= BREAK_GAP_DAYS:
            windows.append((a + timedelta(days=1), b - timedelta(days=1)))
    return windows


def estimate_ucl(ucl_events: list[dict], win_start: datetime, win_end: datetime,
                 breaks: list[tuple[date, date]]) -> tuple[list[dict], list[str],
                                                           list[str]]:
    """Extend observed UCL matchday cadence to cover the window (flagged).

    Estimated matchdays that would land inside an international-break window
    are dropped: no UCL is played during FIFA breaks (the cadence extension
    is an estimate anyway; dropped dates are reported).
    """
    days = sorted({e["start"].date() for e in ucl_events})
    if not days:
        return [], [], []
    cadence = 14  # league-phase matchday every ~2 weeks (assumed default)
    if len(days) >= 2:
        gaps = [(b - a).days for a, b in zip(days, days[1:])
                if 7 <= (b - a).days <= 28]
        if gaps:
            cadence = round(sum(gaps) / len(gaps))
    est_events: list[dict] = []
    dropped: set[str] = set()

    def in_break(d: date) -> bool:
        return any(bs <= d <= be for bs, be in breaks)

    for e in ucl_events:
        k = 0
        while (s := e["start"] + timedelta(days=cadence * k)) <= win_end:
            if s >= win_start:
                if in_break(s.date()):
                    dropped.add(str(s.date()))
                else:
                    est_events.append({**e, "id": f"ucl-est:{e['id']}:{k}",
                                       "start": s, "estimated": True})
            k += 1
        k = -1
        while (s := e["start"] + timedelta(days=cadence * k)) >= win_start:
            if s <= win_end:
                if in_break(s.date()):
                    dropped.add(str(s.date()))
                else:
                    est_events.append({**e, "id": f"ucl-est:{e['id']}:{k}",
                                       "start": s, "estimated": True})
            k -= 1
    est_events.sort(key=lambda e: e["start"])
    return (est_events, sorted({str(e["start"].date()) for e in est_events}),
            sorted(dropped))


# --- window helpers ---------------------------------------------------------

def league_match_days(understat: dict[str, list[dict]],
                      start: datetime, end: datetime) -> list[date]:
    days = set()
    for matches in understat.values():
        for m in matches:
            if start <= m["naive_start"] <= end:
                days.add(m["naive_start"].date())
    return sorted(days)


def max_gap(days: list) -> int:
    return max(((b - a).days for a, b in zip(days, days[1:])), default=0)


# --- policy simulation ------------------------------------------------------

def simulate(policy: str, events_by_sport: dict[str, list[dict]],
             start: datetime, end: datetime) -> dict:
    per_sport: dict[str, int] = defaultdict(int)
    per_week = [0] * 4
    weeks_start = [start + timedelta(days=7 * i) for i in range(4)]

    def week_idx(t: datetime) -> int:
        for i in reversed(range(4)):
            if t >= weeks_start[i]:
                return i
        return 0

    total_minutes = int((end - start).total_seconds() // 60)
    late_skipped_events = 0

    if policy in ("a_flat_6h", "b_flat_12h", "c_gated_6h_72h"):
        ticks = {0, 360, 720, 1080} if policy != "b_flat_12h" else {0, 720}
        for minute in range(total_minutes):
            t = start + timedelta(minutes=minute)
            if (t.hour * 60 + t.minute) not in ticks:
                continue
            for sport, events in events_by_sport.items():
                if policy.startswith(("a_", "b_")):
                    per_sport[sport] += 1
                    per_week[week_idx(t)] += 1
                else:
                    horizon = t + timedelta(hours=72)
                    if any(t < e["start"] <= horizon for e in events):
                        per_sport[sport] += 1
                        per_week[week_idx(t)] += 1
    else:
        tiers = TIERS["d" if policy == "d_snapshots_day_slots"
                      else policy.split("_", 1)[0]]
        per_tier: dict[str, int] = defaultdict(int)
        for sport, events in events_by_sport.items():
            if policy == "d_snapshots_day_slots":
                # slot = calendar day of the round; anchored at the day's
                # first kickoff; one snapshot serves all events of that day
                by_day: dict[date, list[dict]] = defaultdict(list)
                for e in events:
                    by_day[e["start"].date()].append(e)
                slots = [{"start": min(e["start"] for e in group),
                          "member_starts": [e["start"] for e in group]}
                         for group in by_day.values()]
            else:
                slots = [{"start": e["start"], "member_starts": [e["start"]]}
                         for e in events]
            covered: dict[int, set] = {i: set() for i in range(len(tiers))}
            for minute in range(total_minutes):
                t = start + timedelta(minutes=minute)
                for ti, (label, lo, hi) in enumerate(tiers):
                    leads = [(i, (s["start"] - t).total_seconds() / 60)
                             for i, s in enumerate(slots)
                             if t < s["start"] <= end]
                    # lazy firing: act at the LAST minute of an uncovered
                    # event's tolerance window — maximizes merging
                    closing = [i for i, l in leads if lo * 60 <= l < lo * 60 + 1]
                    if any(i not in covered[ti] for i in closing):
                        per_sport[sport] += 1
                        per_tier[str(label)] += 1
                        per_week[week_idx(t)] += 1
                        covered[ti].update(
                            i for i, l in leads if lo * 60 <= l <= hi * 60)
            late_skipped_events += sum(
                1 for e in events
                if start < e["start"] <= start + timedelta(hours=56))
        result_extra = {"per_tier": dict(sorted(per_tier.items())),
                        "firing": "lazy (last minute of tolerance window)"}

    total = sum(per_sport.values())
    result = {"per_sport": dict(sorted(per_sport.items())),
              "per_week": per_week,
              "total_28d": total,
              "per_30d": round(total * 30 / 28),
              "events_within_56h_of_window_start": late_skipped_events}
    if policy.startswith(("d_", "e_", "f_")):
        result.update(result_extra)
    return result


def main() -> None:
    load_env()
    register_secrets()
    if not has("ODDS_API_KEY"):
        print("ODDS_API_KEY missing — cannot run.")
        sys.exit(2)

    key = os.environ["ODDS_API_KEY"]
    session = requests.Session()
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    print("fetching TOA /events (free)...")
    toa_events, deltas = fetch_toa_events(session, key)
    assert all(d["credits_delta"] == 0 for d in deltas), "events must be free"
    EVENTS_OUT.write_text(json.dumps(
        {sport: [{"home": e["home"], "away": e["away"],
                  "start": e["start"].isoformat()} for e in evs]
         for sport, evs in toa_events.items()}, indent=2))

    print("loading Understat schedule...")
    understat = load_understat()
    alignment = align(understat, toa_events)
    print(f"alignment: matched={alignment['n_matched']} "
          f"median_offset={alignment['median_delta_minutes']}min "
          f"zero_share={alignment['share_delta_zero']}")
    offset = alignment["median_delta_minutes"]

    fwd_start, fwd_end = now, now + timedelta(days=WINDOW_DAYS)
    days = league_match_days(understat, fwd_start.replace(tzinfo=None),
                             fwd_end.replace(tzinfo=None))
    fwd_gap = max_gap([fwd_start.date()] + days + [fwd_end.date()])
    windows = {"forward_28d": {
        "start": fwd_start.isoformat(), "end": fwd_end.isoformat(),
        "league_max_gap_days": fwd_gap,
        "contains_intl_break": fwd_gap >= BREAK_GAP_DAYS}}

    nb_start = fwd_end
    season_end = max(m["naive_start"] for ms in understat.values()
                     for m in ms).replace(tzinfo=timezone.utc)
    while nb_start + timedelta(days=WINDOW_DAYS) < season_end:
        d = league_match_days(
            understat, nb_start.replace(tzinfo=None),
            (nb_start + timedelta(days=WINDOW_DAYS)).replace(tzinfo=None))
        if max_gap([nb_start.date()] + d) < BREAK_GAP_DAYS:
            break
        nb_start += timedelta(days=1)
    windows["no_break_28d"] = {
        "start": nb_start.isoformat(),
        "end": (nb_start + timedelta(days=WINDOW_DAYS)).isoformat(),
        "contains_intl_break": False}

    breaks = break_windows(understat)

    report: dict = {
        "run_date_utc": now.isoformat(),
        "cost_model": {
            "odds_call_credits": 1, "markets": "totals", "regions": "eu",
            "note": "measured: cost = markets x regions per /odds call, "
                    "independent of event count and horizon"},
        "events_calls_credit_deltas": deltas,
        "understat_alignment": {k: v for k, v in alignment.items()
                                if k != "pairs"},
        "windows": windows,
        "international_break_windows": [[str(a), str(b)] for a, b in breaks],
        "time_variants": {
            "retimed": "placeholder rounds re-timed from the empirical "
                       "(weekday, kickoff) distribution of real rounds "
                       "(primary; realistic Fri-Mon spread)",
            "asis": "Understat placeholder times left as-is (lower bound)"},
        "policies": {}, "policies_asis": {},
        "events_input": {}, "retimed_rounds": {},
        "assumptions": {
            "t1h_window_hours": [0.5, 2], "t72_window_hours": [64, 80],
            "ucl_cadence_default_days": 14,
            "retime_seed": "deterministic per (league, round date)",
            "understat_utc_offset_minutes_applied": offset},
        "reviewer_expected_per_30d_no_break": {
            "c_gated_6h_72h": "480-520", "d_snapshots": "170-200"},
    }

    for win_name in ("forward_28d", "no_break_28d"):
        ws = datetime.fromisoformat(windows[win_name]["start"])
        we = datetime.fromisoformat(windows[win_name]["end"])
        merged_retimed: dict[str, list[dict]] = {}
        merged_asis: dict[str, list[dict]] = {}
        inputs: dict[str, dict] = {}
        for sport, league in SPORTS.items():
            if sport == "soccer_uefa_champs_league":
                observed = [dict(e, estimated=False)
                            for e in toa_events.get(sport, [])
                            if ws <= e["start"] <= we]
                est, est_days, dropped_days = estimate_ucl(
                    [dict(e) for e in toa_events.get(sport, [])], ws, we,
                    breaks)
                evs = sorted(observed + est, key=lambda e: e["start"])
                merged_retimed[sport] = evs
                merged_asis[sport] = evs  # TOA times; no placeholders here
                inputs[sport] = {"n_observed": len(observed),
                                 "n_estimated": len(est),
                                 "estimated_matchdays": est_days,
                                 "estimated_matchdays_dropped_in_break":
                                     dropped_days}
            else:
                evs, stats = merged_events(sport, league,
                                           toa_events.get(sport, []),
                                           understat, offset, ws, we,
                                           retime=True)
                merged_retimed[sport] = evs
                inputs[sport] = {**stats, "n_events": len(evs),
                                 "first": evs[0]["start"].isoformat()
                                 if evs else None,
                                 "last": evs[-1]["start"].isoformat()
                                 if evs else None}
                evs_a, _ = merged_events(sport, league,
                                         toa_events.get(sport, []),
                                         understat, offset, ws, we,
                                         retime=False)
                merged_asis[sport] = evs_a
                report["retimed_rounds"].setdefault(win_name, {})[sport] = \
                    stats["n_retimed_rounds"]
        report["events_input"][win_name] = inputs

        for policy in POLICIES:
            result = simulate(policy, merged_retimed, ws, we)
            report["policies"].setdefault(policy, {})[win_name] = result
            print(f"{win_name} {policy}: 28d={result['total_28d']} "
                  f"30d={result['per_30d']}")
            if policy in ("c_gated_6h_72h", "d_snapshots"):
                a = simulate(policy, merged_asis, ws, we)
                report["policies_asis"].setdefault(policy, {})[win_name] = a
                print(f"  (asis) 28d={a['total_28d']} 30d={a['per_30d']}")

    # S6: combined budget scenarios — policy + per-candidate event markets
    # + /scores polling. Arithmetic from the measured cost model; per 28 days.
    base = report["policies"]["d_snapshots"]["no_break_28d"]["total_28d"]
    scores_28d = 2 * 4  # 2 credits per /scores call, ~twice a week
    scenarios: dict = {
        "base_policy": "d_snapshots (T-48/24/3 per kickoff slot), no-break 28d",
        "base_28d": base,
        "per_event": {"t24_correct_score_btts_credits": 2,
                      "t1_bet_market_credits": 1},
        "scores": {"credits_per_call": 2, "calls_per_28d": 4,
                   "total_28d": scores_28d},
    }
    for cand in (5, 10, 15):
        events = cand * 4  # candidates per 28 days
        t24 = events * 2
        t1 = events
        scenarios[f"candidates_{cand}_per_week"] = {
            "events_28d": events,
            "extras_t24_28d": t24,
            "extras_t24_plus_t1_28d": t24 + t1,
            "combined_t24_28d": base + t24 + scores_28d,
            "combined_t24_30d": round((base + t24 + scores_28d) * 30 / 28),
            "combined_t24_t1_28d": base + t24 + t1 + scores_28d,
            "combined_t24_t1_30d": round(
                (base + t24 + t1 + scores_28d) * 30 / 28),
        }
    report["budget_scenarios"] = scenarios
    print("budget_scenarios:",
          {k: v.get("combined_t24_30d")
           for k, v in scenarios.items() if isinstance(v, dict)})

    OUT.write_text(json.dumps(report, indent=2, default=str))
    print(f"saved {OUT} and {EVENTS_OUT}")


if __name__ == "__main__":
    main()

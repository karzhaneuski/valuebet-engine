# Data sources — phase 0 recon

Status date: 2026-10-23. Branch: `phase-0-recon`.

Legend: ✅ verified with real requests · ⏸ not verified yet (no API key — no
conclusions until the key arrives).

Evidence: raw summaries in `reports/recon/*.json`, scripts in `scripts/recon/`.

## 1. football-data.co.uk — ✅ verified (no key needed)

Method: downloaded all 30 CSVs (5 leagues × 6 seasons) on 2026-10-23
(`scripts/recon/check_football_data.py`, evidence in `reports/recon/football_data.json`).

| League | Code | 2021-22 … 2025-26 | 2026-27 (current) |
|---|---|---|---|
| Premier League | E0 | 380 matches/season | 70 played |
| La Liga | SP1 | 380 | 89 |
| Serie A | I1 | 380 | 70 |
| Bundesliga | D1 | 306 (18 teams) | 54 |
| Ligue 1 | F1 | 380 in 21/22–22/23, 306 from 23/24 (league reduced to 18 teams) | 63 |

- All result columns (`Date`, `HomeTeam`, `AwayTeam`, `FTHG`, `FTAG`, `FTR`) present
  in every file, no missing values in closing totals.
- **Closing O/U 2.5**: past seasons have `PC>/<` (Pinnacle), `MaxC>/<`, `AvgC>/<`;
  fill rate 1.0 everywhere. The in-progress 2026-27 files currently have only
  `MaxC>/<` and `AvgC>/<` (no `PC` columns yet) — closing-line analysis for the
  current season must use `AvgC`/`MaxC`.
- ~30 bookmaker column groups for 1X2 (B365, Pinnacle `PS`/`PSC`, Betfair exchange
  `BF*`, etc.), each with closing variants; totals markets are limited to the 2.5
  line (20 totals columns per file) — as expected, **no exact-score odds here**.
- Dates in the CSVs are UK local time strings; phase 2 must parse and store them
  as UTC-aware datetimes.
- Cost: free, no key, no visible rate limiting (we throttled to 1 req/s).

**Recommendation: use as-is** for results history and closing O/U 2.5 (backtest base).

## 2. Understat xG via soccerdata — ✅ verified (no key needed)

Method: `soccerdata` 2026.43.0, `Understat.read_schedule()`, 5 leagues × 5 seasons
+ direct JSON-API probe for the 6th season (`scripts/recon/check_understat.py`,
evidence in `reports/recon/understat.json`).

Important API findings (soccerdata 2026.43.0 differs from older docs):

- League codes: `ENG-Premier League`, `ESP-La Liga`, `ITA-Serie A`,
  `GER-Bundesliga`, `FRA-Ligue 1`. Seasons: ints, `2022` = 2022/23.
- `read_schedule()` gives per-match rows with `home_xg` / `away_xg`, goals, teams,
  dates — exactly what the model needs.
- **Limitation**: soccerdata's season discovery lists only the **last 5 seasons**
  (2022/23 … 2026/27). Older seasons are still served by Understat's JSON API
  (`https://understat.com/getLeagueData/{slug}/{season}`); the 2021/22 probe
  through that API worked for all 5 leagues (~0.5 s per league-season). Phase 2
  must fetch seasons older than the 5-season window through that endpoint
  (the reader's `_read_league_season` does this; pin the soccerdata version).

Coverage check (played matches with xG, 2026-10-23):

| League | 21/22 | 22/23 | 23/24 | 24/25 | 25/26 | 26/27 current |
|---|---|---|---|---|---|---|
| Premier League | 380 | 380 | 380 | 380 | 380 | 70 |
| La Liga | 380 | 380 | 380 | 380 | 380 | 88 |
| Serie A | 379* | 380 | 380 | 380 | 380 | 70 |
| Bundesliga | 306 | 306 | 306 | 306 | 306 | 54 |
| Ligue 1 | 380 | 380 | 306 | 306 | 306 | 63 |

\* Serie A 2021/22: 1 of 380 finished matches lacks xG (fill 0.997) — acceptable.

- Match counts line up with football-data.co.uk almost exactly (La Liga current
  season: 88 vs 89 — one match played per fd but without xG yet or postponed;
  phase 2 should log such mismatches rather than drop them silently).
- Speed: ~2.6–3.2 s per league for 5 seasons on first fetch (30 season pages
  total ≈ 15 s); season pages are cached to JSON files in the reader's
  `data_dir`. The current season is refetched on every call (incomplete seasons
  bypass cache by default) — 5 requests per refresh, cheap.
- Understat is an unofficial source behind TLS fingerprinting: plain `requests`
  may get blocked (soccerdata uses a TLS-impersonating session). Keep the
  library (pinned) rather than hand-rolling the scraper; cache aggressively;
  refresh the current season at most a few times per day.
- Team names differ from football-data.co.uk spelling (and soccerdata applies
  its own `TEAMNAME_REPLACEMENTS`) — the `team_aliases` table (phase 1) must map
  both spellings.

**Recommendation: use soccerdata (pinned) with a file cache**; fetch older seasons
via the getLeagueData JSON endpoint.

## 3. API-Football — ⏸ not verified

Waiting for `API_FOOTBALL_KEY`. To check when the key arrives: real daily request
limit, current-season availability on the free plan (some plans restrict seasons
to past years — must be tested factually), endpoints fixtures / lineups /
injuries / results for the top-5 leagues and the Champions League.

## 4. Odds API (totals odds) — ⏸ not verified

Waiting for `ODDS_API_KEY` (The Odds API) and `API_FOOTBALL_KEY` (odds endpoints
as candidate #2). To check: which football markets the free tiers actually return
(totals and which lines, exact score, BTTS), which bookmakers, monthly quota,
and whether the quota covers polling top-5 + UCL matches 72 h ahead every 6 h.

## 5. Search API (team news) — ⏸ not verified

Waiting for `TAVILY_API_KEY` (and optionally `BRAVE_SEARCH_API_KEY`). To check:
free quota limits and result quality for `<team> injury news team news` on 3 real
upcoming matches.

## 6. Gemini — ⏸ not verified

Waiting for `GEMINI_API_KEY`. To check: which models the key can access and
structured output (JSON schema) support.

## Selection summary & quota budget — pending

The final source-selection table and the recalculated request budget will be
written here after sections 3–6 are verified.

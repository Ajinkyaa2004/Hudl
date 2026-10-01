# Writing a source adapter (RTT Finder)

Project: /Users/ajinkya/Documents/My Files/HUDL/rtt-finder. Python 3.9 in `.venv` (always run `.venv/bin/python`,
use `from __future__ import annotations` as the FIRST line after the module docstring). No new pip installs without need
(httpx, playwright, openpyxl are installed).

## What the tool does
Input: a HokReg header, e.g. `[2254375] Lehigh Valley Phantoms 16U AA 0 - 0 Jersey Colts 16U AA | AHF 16U | 2026-09-20T02:00:00 | RL:8`.
`lookup.parse_header(text)` -> dict(t1, t2, comp, date 'YYYY-MM-DD', time 'HH:MM' or None, ...).
**Header time is Moscow time (UTC+3)**, so North American evening games carry the NEXT day's date.
Output: the official match report (game sheet / protocol) URL for that exact game.

## Adapter contract (see backend/adapters/nhl.py for a tiny complete example)
```python
from .base import Adapter, score_candidate, local_to_utc
class MySource(Adapter):
    name = "mysource"; site = "example.com"
    # optional: budget_s = 45 ; ambiguous_dates = True (site can't tell back-to-back days apart)
    def applies(self, parsed, tm, kb) -> bool: ...   # cheap: competition words, team country/sites in tm["t1n"]["best"] (a Club Data dict with keys name, country, site, schedule, roster, mhr, ep) 
    def preferred_date(self, parsed): ...            # optional (NA leagues: day before when header time < 12:00)
    async def find(self, parsed, tm, kb) -> list[dict]:
        # fetch games around parsed["date"] (that day and the day before), build candidates:
        cand = dict(source=self.name, site=self.site, league="League + season name", home=..., away=..., date="YYYY-MM-DD" (local game date),
                    score="3-2" or None, status=None, kind="protocol", adapter=self.name, url="<report url>",
                    start_utc="YYYY-MM-DDTHH:MM" or None)   # use local_to_utc(date, "19:30", "America/New_York")
        score_candidate(cand, parsed, cand["league"])
        keep if min(cand["team_scores"]) >= 0.6
```
Fetch with `from ..fetch import get_text, get_json, browser_page` (cached; ttl seconds; browser_page = headless Chromium,
for Cloudflare/JS sites, returns HTML or the result of a js function string). Be polite: no bursts, cache schedules (ttl 1800+).

## Rules
- Accuracy first: never return a game that is not the header's game as a strong match. Team names must keep age (U16/16U/2011),
  level (AA/AAA) and colour/tier words (Black, Gold, Tier 1) so the shared scorer can reject wrong teams.
- Only CREATE new files: `backend/adapters/<name>.py`, `data/<name>_*.json`, `tests/probe_<name>.py`. Do NOT edit shared files
  (`__init__.py`, `base.py`, `lookup.py`, `fetch.py`, `gamesheet.py`, `hockeytech.py`, `data/knowledge.json`). The lead integrates.
- Test on real games: `tests/replay_20260925_0017.csv` columns header, analyst (the link the human analyst found), expected_domain,
  auto_result. `data/progress.xlsx` sheet "Progress": col C header, col H/I report link (read with openpyxl read_only).
  Write `tests/probe_<name>.py` that runs your adapter on those headers and prints found/correct counts
  (e.g. `adapter.find(parsed, tm, kb)` with `tm = lookup.search(header, kb)["teams"]`, `kb = lookup.load_kb()`; score with
  `base.strong(c)`). Season 2026-27 only (games from 2026-08-01).
- Report back: files created, the registration line for ADAPTERS, applies() rule, measured results (x of y correct, wrong count),
  and anything blocked, with the exact error.

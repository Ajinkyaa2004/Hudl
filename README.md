# PuckTrace

PuckTrace (formerly RTT Finder) finds ice hockey match reports for RTT. Paste a match header, get the match report link or the roster links for both teams, in the RTT search order. Nothing is written to any sheet. You enter results into the Progress sheet yourself.

## Run

```bash
cd "/Users/ajinkya/Documents/My Files/HUDL/rtt-finder"
.venv/bin/uvicorn backend.app:app --port 8765
```

Open http://localhost:8765 in Chrome, where the VPN extension lives, so VPN-tagged links open there.

## Screens

- **Find.** One header in: `Team 1 vs Team 2 | Competition | YYYY-MM-DD`, or the full HokReg header. Result shows the report link if the game was saved before, otherwise the places to look in order, then roster links. The search trail below shows every step. "Save what you found" is optional and makes the next search for those teams faster.
- **Sources.** The knowledge base of teams and tournaments with their schedule, site, roster, EliteProspects and MyHockeyRankings links. Search, edit, add, delete by hand.
- **Saved.** Games you saved. Click one to search it again.
- **VPN sites.** Domains that need the Chrome VPN, one per line.
- **Status.** Games known, what the tool has learned, and a daily check of every source.

Keyboard: `/` focuses the search box, `Enter` finds, `S` saves.

## Knowledge base

Everything lives in `data/knowledge.json`. It was seeded once from the Club Data and Progress sheet exports with:

```bash
.venv/bin/python -m backend.seed
```

Re-running the seed overwrites hand edits, so only do it if you want to start over. The Excel files are not read at runtime.

## How a search works

1. **Remembered.** A game you saved before is answered at once.
2. **Game list.** `data/games.db` holds every game of the season from the bulk sources below (about 185,000 by 30 Sept), refreshed once a day while the server runs. When one game there matches both teams and either the header's start time or the expected date, that is the answer, in well under a second.
3. **Live sources.** Otherwise every source that fits the teams and competition is asked live (table below). Games they return are added to the game list.
4. **Web search.** If nothing at all is found, a web search looks for pages naming both teams. These show as possible games only.
5. **Rosters.** When there is no report, both teams' current rosters are looked up (see Rosters).

A result is Found only when one game matches both teams with age group, level and colour or tier consistent, on the same or the neighbouring day. Two extra rules use the header time, which is Moscow time (UTC+3):
- Among several games of the same teams, the one starting at the header time wins, and a game hours away loses to one at the header time.
- If one team clearly matches (age and level included) and a game of that team starts within 20 minutes of the header time, that is the game even if the opponent's name is written differently. A team plays one game at a time.

A header time of 00:00 is treated as unknown. Names are compared with abbreviations spelled out (PTL, CT, NYC), common word forms (Indy/Indiana, Herford/Herforder), words in any order, sponsor words, company suffixes and generic words (Ishockeyklubb, Eishockey) ignored, and birth-year teams matched to age groups (2016 = 10U/U11 this season).

## Sources

| Source | How | Covers |
| --- | --- | --- |
| GameSheet | public JSON API: every game of all ~870 current seasons daily; live day feed for games not in the list yet | USA Hockey and Hockey Canada youth, showcases (CCM, Chicago Mission, MAHA, Beast, USHL Fall Classic, ...), PJHL and more |
| TimeToScore | public schedule tables of every league and tournament on the blackbear, usphl, caha, asec and cna hosts; USPHL exhibitions read from scoresheets | AHF, Tier 1 (THF), NGHL, AGHF, USPHL/NCDC, CAHA, SCAHA, NorCal, Tinseltown and other tournaments |
| HockeyTech | public JSON schedule, 72 leagues (`data/hockeytech_clients.json`) | CHL leagues, AHL, USHL, PWHL, Junior A leagues, Alberta/Manitoba AAA and female leagues, CSSHL, OUA, UT1HL, ... |
| hockeydata (deb-online) | division schedules, ~780 divisions found by number | DEB (Oberliga, DNL, youth, friendlies), German regional leagues (BEV, NRW), Austrian regional leagues, Netherlands |
| stats.swehockey.se | day pages plus cup/tournament games read by game number | Sweden |
| KHL family | day lists on online.khl.ru | KHL, MHL, VHL, cups and friendlies |
| hockey.by, polskihokej.eu, lhf.lv | calendars | Belarus, Poland, Latvia |
| Finland, Switzerland, Norway, Denmark, Czechia | federation JSON / pages | leijonat + Liiga, sihf, hockey.no, sportsadmin + Metal Ligaen, ceskyhokej |
| ICE Hockey League, Alps League, DEL, DEL2, CHL, France, Italy, UK (EIHL, NIHL), onlajny (Slovakia, friendlies) | league sites and feeds | as named |
| RAMP, 200x85, Blue Line/Kreezee, AYHL, HockeyShift, tournamentsgurus | league and tournament platforms | OWHL, Ontario/US tournaments, AYHL, HS Elite, EAK |
| NHL | api-web.nhle.com | NHL |

Blocked by a security check, so shown as links to open in Chrome: hockeyslovakia.sk (a results page link is given; senior Slovak games are found on onlajny), scgha.com (Stoney Creek) and other MBSportsWeb association sites.

## Learning

- Saving a game makes it answer at once next time, and teaches the tool that team's other spelling when the saved report is a game in the list (`data/aliases_learned.json`).
- Every report found, saved or automatic, records which site serves those teams and that competition, so that source is always asked for them.
- Every game any source returns joins the game list.

## Rosters

`backend/roster_sources.py` looks up each team's current roster: EliteProspects (its public data server), GameSheet, HockeyTech and TimeToScore team rosters, The Hockey Observer, then the Club Data roster pages (MyHockeyRankings, team sites). Each roster is marked verified (this season, 10+ players, ages fit), few players, old season, wrong age group, probable or check. On the 200-team test, 67% get a verified current roster (was 11%).

## Status screen and daily jobs

The server refreshes the game list once a day (`backend/harvest.py`: GameSheet, TimeToScore, HockeyTech, hockeydata, Sweden, Poland, Denmark) and then checks one known game per source (`backend/health.py`). The Status screen shows the game count, what has been learned and each source's check. To run by hand:

```bash
.venv/bin/python -m backend.harvest            # all bulk sources (about 30 minutes)
.venv/bin/python -m backend.health             # source checks
```

## Outcomes

- Found: a report was found automatically, or this exact game was saved before with a report link.
- Check: both teams known, sources are listed in RTT order.
- Not found: at least one team is not in the knowledge base. Candidates with a different age group or level are shown as rejected with the reason.

## Layout

- `backend/lookup.py`: parser, team and tournament matching, search order, save.
- `backend/adapters/`: one file per automatic source, `base.py` holds the scoring rules.
- `backend/fetch.py`: HTTP and headless-browser fetching with a cache.
- `backend/app.py`: API and static serving.
- `backend/seed.py`: one-time seed from the sheet exports.
- `backend/rosters.py`, `backend/roster_sources.py`: rosters.
- `backend/gameindex.py`, `backend/harvest.py`, `backend/gamesheet_api.py`: the game list and its daily refresh.
- `backend/learning.py`: remembered results, learned sites and spellings.
- `backend/health.py`: source checks.
- `backend/replay.py`, `backend/replay_rosters.py`: tests against the Progress sheet.
- `frontend/index.html`: the whole UI, no build step.

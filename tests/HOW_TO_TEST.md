# How RTT Finder is tested

Two kinds of test: an automatic replay against the Progress sheet, and a live spot-check by an analyst.

## 1. Replay against the Progress sheet (automatic)

The Progress sheet export in `data/progress.xlsx` is the answer key. For every game it holds the link an analyst actually found. The replay runs each game through the tool and compares.

```bash
cd "/Users/ajinkya/Documents/My Files/HUDL/rtt-finder"
.venv/bin/python -m backend.replay --end 2026-09-21                                   # phase 1 only, all games, about 1 minute
.venv/bin/python -m backend.replay --end 2026-09-21 --auto --all --no-gamesheet       # every game, all sources except GameSheet, about 1 hour
.venv/bin/python -m backend.replay --end 2026-09-21 --auto --gamesheet-only --sample 40  # GameSheet sample, about 1 hour
.venv/bin/python -m backend.replay_rosters --sample 120                               # roster checks, about 15 minutes
```

The replay compares games by identity, not by site: a HockeyTech game linked from a league site (bchl.ca, gohl.ca and so on) equals the same game on lscluster.hockeytech.com, and a mestis.fi or liiga.fi link equals the Finnish federation's game with the same number.

Result labels: correct (same game), WRONG (same source, different game), found elsewhere (another source than the analyst's), found (unverified) (the analyst's link is a screenshot, blank or an unmapped site), candidates, missed, none. The headline is the share of all games resolved automatically.

Each run writes `tests/replay_<timestamp>.csv`, one row per game, and prints a summary.

What is measured:

| Measure | Meaning | Target from the plan |
| --- | --- | --- |
| both teams matched | both header names resolved to a team on file, age and level exact | rises as Sources grows |
| competition matched | competition hint resolved, or recognised as a friendly | |
| analyst's site in links (no history) | the site the analyst used appears in the tool's links, using only Club Data links, not hints learned from this same sheet | stage 1 milestone: 85 percent |
| phase 2 correct | adapter returned Found and the game id equals the analyst's link | |
| phase 2 WRONG | adapter returned Found on the analyst's site but a different game | must stay under 1 percent |
| phase 2 missed | analyst's site is one the adapters cover, but no Found | |
| found elsewhere | adapter found a game on a site the analyst did not use; needs a human look, may be correct | |

The "with history" number is inflated because the source hints were seeded from this sheet. Use the "no history" number to judge Club Data coverage, and the "with history" number to see what a returning competition or team gets.

## 2. Live spot-check (manual, each day for the first two weeks)

Pick 20 games from the day's queue, mixed leagues, at least 5 friendlies and 5 North American youth games.

For each game record in a sheet:

| Column | Value |
| --- | --- |
| header | as pasted |
| tool outcome | Found, Check, Not found |
| tool link | the report link shown, if any |
| correct | yes / no / partly (right site, wrong game, or old roster) |
| seconds saved | rough estimate versus searching by hand |
| note | what was wrong, if anything |

Rules of thumb:

- A Found that points at the wrong game is the only serious failure. Save the header and link so it can be added to the replay set.
- A Check that lists the right site first is a pass for phase 1.
- A Not found for a team that is genuinely new is expected. Add the team under Sources once found and it becomes automatic next time.

## 3. Adding a known game to the replay set

Any game you verify by hand can be added to `data/progress.xlsx` in the same columns, or kept in a separate export passed with the same script. The replay only needs the header, the protocol link, and the date.

## 4. Season-start maintenance (needed for the automatic sources)

Run these once at the start of each season, and again whenever a replay shows a site missing games it used to find.

```bash
cd "/Users/ajinkya/Documents/My Files/HUDL/rtt-finder"
.venv/bin/python -m backend.harvest_gamesheet --from-progress --refresh   # GameSheet seasons and their team names, about 15 minutes
```

German divisions and Czech competitions are learned automatically whenever a report link from those sites is saved in the tool. The seed files `data/deb_divisions.json` and `data/ceskyhokej_competitions.json` were built from the Progress sheet export on 22 Sept 2026.

GameSheet challenges rapid automation, so the tool loads at most one GameSheet page every 6 seconds. Do not run the harvest and a replay with a GameSheet sample at the same time.

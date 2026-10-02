"""Daily harvest: load whole seasons from the bulk sources into the local game index, so most
headers resolve from the index in under a second.

    .venv/bin/python -m backend.harvest             # everything
    .venv/bin/python -m backend.harvest gamesheet   # one source (gamesheet, timetoscore)
    .venv/bin/python -m backend.harvest --from-file # GameSheet from data/gamesheet_games.json

The server also runs it once a day in the background (backend/app.py)."""
from __future__ import annotations
import asyncio, json, re, sys, time
from pathlib import Path
from . import gameindex, lookup

DATA = Path(__file__).resolve().parent.parent / "data"


def gamesheet_rows(games: list[dict]) -> list[dict]:
    from .adapters.gamesheet import clean
    out = []
    for g in games:
        if not g.get("date") or g["date"] < "2026-07-01":
            continue
        league = " ".join(x for x in (g.get("season_name"), g.get("division")) if x)
        out.append(dict(url=g["url"], source="GameSheet", site="gamesheetstats.com", gid=g["game_id"], date=g["date"], time=g.get("time"),
                        start_utc=g.get("start_utc"), home=clean(g["home"]), away=clean(g["away"]), league=league,
                        status=g.get("status"), score=g.get("score")))
    return out


async def harvest_gamesheet(log=print) -> dict:
    from . import gamesheet_api as ga
    kb = lookup.load_kb()
    known = list((kb.get("gamesheet_seasons") or {}).keys())
    t = time.time()
    try:
        seasons = await ga.current_seasons(known)
        log(f"GameSheet: {len(seasons)} current seasons ({time.time() - t:.0f}s)")
        n, failed = 0, 0
        sem = asyncio.Semaphore(4)

        async def one(s):
            nonlocal n, failed
            async with sem:
                gs = await ga.season_games(s["sid"], s.get("name") or "")
            if gs is None:
                failed += 1
                return
            n += gameindex.add_games(gamesheet_rows(gs))
        await asyncio.gather(*[one(s) for s in seasons])
        # remember the seasons (names and dates) for the live adapter and the Sources screen
        kb = lookup.load_kb()
        gsk = kb.setdefault("gamesheet_seasons", {})
        for s in seasons:
            e = gsk.setdefault(str(s["sid"]), {})
            e.update(name=s.get("name") or e.get("name"), start=s.get("start"), end=s.get("end"), harvested=time.strftime("%Y-%m-%d"))
        lookup.save_kb(kb)
    finally:
        await ga.close()
    log(f"GameSheet: {n} games indexed, {failed} seasons failed, {time.time() - t:.0f}s")
    return dict(seasons=len(seasons), games=n, failed=failed)


def gamesheet_from_file(path: Path = DATA / "gamesheet_games.json") -> int:
    d = json.loads(path.read_text())
    return gameindex.add_games(gamesheet_rows(d["games"]))


async def harvest_timetoscore(log=print) -> dict:
    from .adapters.timetoscore import harvest
    r = await harvest(verbose=False)
    log(f"TimeToScore: {r}")
    return r


async def harvest_hockeytech(log=print) -> dict:
    """Every game of the 2026-27 seasons (pre-season, regular, playoffs) of all HockeyTech leagues."""
    import re as _re
    from .adapters.hockeytech import HockeyTech, LEAGUES, REPORT, _utc
    ht = HockeyTech()
    n, leagues = 0, 0
    for code, cfg in LEAGUES.items():
        seen = set()
        for year in (2026, 2027):
            try:
                _, _, games = await ht._league_games(code, cfg, year)
            except Exception:
                continue
            rows = []
            for g in games:
                if g.get("game_id") in seen or (g.get("date_played") or "") < "2026-07-01":
                    continue
                seen.add(g.get("game_id"))
                league = _re.sub(r"(U\d{1,2})(?=[A-Za-z])", r"\1 ", f"{cfg['name']} {g.get('_season', '')}".strip())
                final = g.get("final") == "1" or g.get("game_status") == "Final"
                rows.append(dict(url=REPORT.format(code=cfg.get("client", code), gid=g["game_id"]), source=ht.name, site=ht.site, gid=str(g["game_id"]),
                                 date=g.get("date_played"), time=None, start_utc=_utc(g.get("GameDateISO8601")), home=g.get("home_team_name", ""),
                                 away=g.get("visiting_team_name", ""), league=league, status=g.get("game_status"),
                                 score=f"{g.get('home_goal_count')}-{g.get('visiting_goal_count')}" if final else None))
            n += gameindex.add_games(rows)
        leagues += 1
    log(f"HockeyTech: {n} games from {leagues} leagues")
    return dict(leagues=leagues, games=n)


SWE_GAME = re.compile(r"\|([^|]+?) - ([^|]+?)\|(\d{4}-\d{2}-\d{2})\s(\d{2}:\d{2})\|([^|]+)\|")


def swe_game_page(page: str, gid: str) -> dict | None:
    """Home, away, date, time and series from a stats.swehockey.se game page."""
    import html as _h
    s = re.sub(r"<script.*?</script>|<style.*?</style>", "", page or "", flags=re.S)
    s = re.sub(r"\s*\|[\s|]*", "|", _h.unescape(re.sub(r"<[^>]+>", "|", s)))
    m = SWE_GAME.search(s)
    if not m or not ("2026-07-01" <= m.group(3) <= "2027-06-30"):
        return None
    from .adapters.base import local_to_utc
    return dict(url=f"https://stats.swehockey.se/Game/Events/{gid}", source="swehockey", site="stats.swehockey.se", gid=gid,
                date=m.group(3), time=m.group(4), start_utc=local_to_utc(m.group(3), m.group(4), "Europe/Stockholm"),
                home=m.group(1).strip(), away=m.group(2).strip(), league=m.group(5).strip(), status=None, score=None)


async def harvest_swehockey(log=print, days_back: int | None = None, max_numbers: int = 40000) -> dict:
    """Every game on the all-games day pages from 1 Aug to a week ahead, then the game numbers
    in between that those pages leave out (cups, friendly tournaments), read one by one."""
    import datetime as _dt
    from .fetch import get_text
    from .adapters.swehockey import BY_DATE, parse_rows
    from .adapters.base import local_to_utc
    today = _dt.date.today()
    start = _dt.date(2026, 8, 1) if days_back is None else today - _dt.timedelta(days=days_back)
    days = [(start + _dt.timedelta(days=k)).isoformat() for k in range((today - start).days + 8)]
    n = 0
    sem = asyncio.Semaphore(3)

    async def day(d):
        nonlocal n
        async with sem:
            page = await get_text(BY_DATE.format(date=d), ttl=6 * 3600 if d >= (today - _dt.timedelta(days=3)).isoformat() else 30 * 86400)
        rows = []
        for g in parse_rows(page or "", d, by_date_page=True):
            rows.append(dict(url=f"https://stats.swehockey.se/Game/Events/{g['gid']}", source="swehockey", site="stats.swehockey.se", gid=g["gid"],
                             date=d, time=g.get("time"), start_utc=local_to_utc(d, g.get("time"), "Europe/Stockholm"),
                             home=g["home"], away=g["away"], league=g.get("league") or "", status=None, score=g.get("score")))
        n += gameindex.add_games(rows)
    await asyncio.gather(*[day(d) for d in days])
    # gap fill: cups and friendly tournaments are not on the day pages, but their games are
    # numbered among the others. Each number is read once (pages are cached for a month); the
    # scan remembers how far it got, so later days only read new numbers.
    state_f = DATA / "swehockey_scan.json"
    try:
        state = json.loads(state_f.read_text())
    except Exception:
        state = {}
    with gameindex._lock:
        ids = sorted(int(r[0]) for r in gameindex.conn().execute("select gid from games where source='swehockey' and date>='2026-07-01' and gid is not null"))
    if not ids:
        return dict(games=n)
    lo = state.get("scanned_to") or ids[len(ids) // 100]
    hi = ids[min(len(ids) - 1, int(len(ids) * 0.97))] + 1500
    have = set(ids)
    missing = [i for i in range(lo, hi) if i not in have][:max_numbers]
    found = []

    async def one(gid):
        async with sem:
            page = await get_text(f"https://stats.swehockey.se/Game/Events/{gid}", ttl=30 * 86400, timeout=20)
        g = swe_game_page(page or "", str(gid))
        if g:
            found.append(g)
    for k in range(0, len(missing), 500):          # in chunks, saving progress
        await asyncio.gather(*[one(i) for i in missing[k:k + 500]])
        gameindex.add_games(found)
        state["scanned_to"] = missing[min(k + 500, len(missing)) - 1] + 1
        state_f.write_text(json.dumps(state))
    m = len(found)
    log(f"swehockey: {n} games from day pages, {m} more from {len(missing)} unlisted game numbers (scanned to {state.get('scanned_to')})")
    return dict(games=n + m, unlisted=m)


async def harvest_deb(log=print) -> dict:
    """German divisions (deb-online / hockeydata): refresh every 2026-27 division's dates and teams,
    look for new division numbers above the ones known, and put every game in the index."""
    import datetime as _dt
    from .fetch import get_json
    from .adapters import deb as D
    f = D.load()
    key = f.get("apiKey")
    if not key:
        return dict(error="no api key")
    state_f = DATA / "deb_scan.json"
    try:
        state = json.loads(state_f.read_text())
    except Exception:
        state = {}
    cur = [int(k) for k, v in f["divisions"].items() if "2026" in str(v.get("season") or "") or (v.get("last") or "") >= "2026-07-01"]
    lo = state.get("scanned_to") or (min(cur) if cur else 21000)
    ids = sorted(set(cur) | set(range(lo, (max(cur) if cur else lo) + 300)))
    sem = asyncio.Semaphore(4)
    n, new = 0, 0

    async def one(did):
        nonlocal n, new
        async with sem:
            data = await get_json(D.API.format(key=key, div=did), ttl=6 * 3600, timeout=40)
        rows = ((data or {}).get("data") or {}).get("rows", [])
        if not rows:
            return
        dates = sorted(x.get("scheduledGameStart", "")[:10] for x in rows if x.get("scheduledGameStart"))
        if not dates or dates[-1] < "2026-07-01":
            return
        teams = sorted({x.get("homeTeamLongName") for x in rows if x.get("homeTeamLongName")} | {x.get("awayTeamLongName") for x in rows if x.get("awayTeamLongName")})
        if str(did) not in f["divisions"]:
            new += 1
        f["divisions"][str(did)] = dict(league=rows[0].get("leagueName"), division=rows[0].get("divisionName"), season=rows[0].get("currentSeason"),
                                        first=dates[0], last=dates[-1], games=len(rows), teams=teams)
        out = []
        for r in rows:
            date = (r.get("scheduledGameStart") or "")[:10]
            if date < "2026-07-01":
                continue
            played = r.get("gameHasEnded") or r.get("gameStatus") in (2, 3)
            out.append(dict(url=D.REPORT.format(gid=r["id"], div=did), source="deb-online", site="deb-online.live", gid=r["id"], date=date,
                            time=(r.get("scheduledGameStart") or "")[11:16] or None,
                            start_utc=(_dt.datetime.utcfromtimestamp(r["gameUtcTimestamp"] / 1000).strftime("%Y-%m-%dT%H:%M") if r.get("gameUtcTimestamp") else None),
                            home=r.get("homeTeamLongName", ""), away=r.get("awayTeamLongName", ""),
                            league=f"{r.get('leagueName') or ''} {r.get('divisionName') or ''}".strip(), status=None,
                            score=f"{r.get('homeTeamScore')}-{r.get('awayTeamScore')}" if played else None))
        n += gameindex.add_games(out)
    await asyncio.gather(*[one(d) for d in ids])
    D.DATA.write_text(json.dumps(f, ensure_ascii=False, indent=1))
    state["scanned_to"] = max(ids) - 250
    state_f.write_text(json.dumps(state))
    log(f"deb-online: {n} games, {new} new divisions, {len(ids)} division numbers read")
    return dict(games=n, new_divisions=new)


async def harvest_sportsadmin(log=print) -> dict:
    from .adapters.sportsadmin import harvest
    r = await harvest()
    log(f"sportsadmin: {r}")
    return dict(result=str(r)[:200])


async def harvest_poland(log=print) -> dict:
    """polskihokej.eu (PZHL): every competition's 2026-27 schedule, from the league list on the
    schedule page (Tauron Hokej Liga, 1 Liga, juniors, women, friendlies, cups)."""
    import html as _h
    from .fetch import get_text
    from .adapters.base import local_to_utc
    base = "https://polskihokej.eu/terminarz?league_id={lid}"
    first = await get_text(base.format(lid=1), ttl=6 * 3600, timeout=40)
    lids = sorted({int(v) for v, t in re.findall(r'<option[^>]*value="(\d+)"[^>]*>([^<]*)', first or "") if int(v) < 1000} | {1})
    names = {int(v): _h.unescape(t).strip() for v, t in re.findall(r'<option[^>]*value="(\d+)"[^>]*>([^<]*)', first or "") if int(v) < 1000}
    sem = asyncio.Semaphore(3)
    n = 0

    async def one(lid):
        nonlocal n
        async with sem:
            page = first if lid == 1 else await get_text(base.format(lid=lid), ttl=6 * 3600, timeout=40)
        out = []
        for row in re.findall(r'<tr class="transition hover:bg-gray-50">(.*?)</tr>', page or "", re.S):
            gid = re.search(r"polskihokej\.eu/game/(\d+)", row)
            d = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", row)
            teams = re.findall(r'alt="([^"]+)"', row)
            if not (gid and d and len(teams) >= 2):
                continue
            date = f"{d.group(3)}-{d.group(2)}-{d.group(1)}"
            if date < "2026-07-01":
                continue
            tm = re.search(r">\s*(\d{1,2}:\d{2})\s*<", row)
            sc = re.search(r">\s*(\d+)\s*:\s*(\d+)\s*<", row)
            out.append(dict(url=f"https://polskihokej.eu/game/{gid.group(1)}", source="polskihokej", site="polskihokej.eu", gid=gid.group(1),
                            date=date, time=tm.group(1) if tm else None, start_utc=local_to_utc(date, tm.group(1) if tm else None, "Europe/Warsaw"),
                            home=_h.unescape(teams[0]).title(), away=_h.unescape(teams[1]).title(), league=names.get(lid, "Poland"),
                            status=None, score=f"{sc.group(1)}-{sc.group(2)}" if sc else None))
        n += gameindex.add_games(out)
    await asyncio.gather(*[one(l) for l in lids])
    # played games leave the schedule pages: read the game numbers below the schedule's from
    # the game pages (each once; cached a month)
    with gameindex._lock:
        ids = sorted(int(r[0]) for r in gameindex.conn().execute("select gid from games where source='polskihokej'"))
    found = []
    if ids:
        have = set(ids)
        state_f = DATA / "poland_scan.json"
        try:
            lo = json.loads(state_f.read_text()).get("from") or ids[0] - 700
        except Exception:
            lo = ids[0] - 700
        todo = [i for i in range(lo, ids[-1] + 50) if i not in have]

        async def game(gid):
            async with sem:
                page = await get_text(f"https://polskihokej.eu/game/{gid}", ttl=30 * 86400, timeout=30)
            t = re.sub(r"<script.*?</script>|<style.*?</style>", "", page or "", flags=re.S)
            t = re.sub(r"\s*\|[\s|]*", "|", _h.unescape(re.sub(r"<[^>]+>", "|", t)))
            m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})\|([^|]+)\|([^|]+)\|(\d+:\d+|VS)\|([^|]+)\|", t)
            if not m:
                return
            date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
            if date < "2026-07-01":
                return
            sc = m.group(6).split(":") if ":" in m.group(6) else None
            found.append(dict(url=f"https://polskihokej.eu/game/{gid}", source="polskihokej", site="polskihokej.eu", gid=str(gid), date=date,
                              time=None, start_utc=None, home=m.group(5).strip().title(), away=m.group(7).strip().title(),
                              league=m.group(4).strip(), status=None, score=f"{sc[0]}-{sc[1]}" if sc else None))
        await asyncio.gather(*[game(i) for i in todo])
        gameindex.add_games(found)
        dated = sorted(int(g["gid"]) for g in found)
        state_f.write_text(json.dumps({"from": (dated[0] if dated else lo)}))
    log(f"polskihokej: {n} scheduled games from {len(lids)} competitions, {len(found)} played games from game pages")
    return dict(games=n + len(found), competitions=len(lids))


SOURCES = {"gamesheet": harvest_gamesheet, "poland": harvest_poland, "deb": harvest_deb, "sportsadmin": harvest_sportsadmin, "swehockey": harvest_swehockey, "timetoscore": harvest_timetoscore, "hockeytech": harvest_hockeytech}


async def run(names=None, log=print) -> dict:
    out = {}
    for name in names or SOURCES:
        try:
            out[name] = await SOURCES[name](log)
        except Exception as e:
            out[name] = dict(error=str(e)[:200])
            log(f"{name}: failed {e}")
    (DATA / "harvest_status.json").write_text(json.dumps(dict(at=time.strftime("%Y-%m-%dT%H:%M:%S"), results=out, index=gameindex.stats()), indent=1))
    return out


async def run_in_thread(names=None, log=print, delay_s: float = 0) -> dict:
    """The refresh in its own thread and event loop, so its CPU-heavy parsing doesn't hold up
    the web server's requests (a small server has a fraction of one CPU)."""
    if delay_s:
        await asyncio.sleep(delay_s)
    return await asyncio.to_thread(lambda: asyncio.run(run(names, log)))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--from-file" in sys.argv:
        print(gamesheet_from_file(), "GameSheet games loaded from file")
    else:
        print(asyncio.run(run(args or None)))
    print(gameindex.stats())

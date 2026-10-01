"""GameSheet JSON API: whole seasons of games in one plain HTTP call, no browser.

The gamesheetstats.com web app (Next.js) reads its data from its own JSON routes on the
same host. They answer plain httpx GETs with a browser User-Agent; no cookie, token or
Cloudflare clearance is needed (the HTML pages are challenged, the /api/ routes are not).

    GET https://gamesheetstats.com/api/unified-games/{sid}      every game of a season, all statuses
        optional: limit, offset, order=asc|desc, division=<id>, gameType=regular_season|playoffs|exhibition,
                  gameSearch=<text>, dateStart=YYYY-MM-DD, dateEnd=YYYY-MM-DD, team=<id>
        -> {"data": [game...], "meta": {"total": n, "filtered": n}}   (no limit = all games)
    GET https://gamesheetstats.com/api/season-info/{sid}        name, start/end, league, association
    GET https://gamesheetstats.com/api/season-divisions/{sid}   divisions with team counts
    GET https://gamesheetstats.com/api/leagues/{lid}            league name and its active season
    GET https://gamesheetstats.com/api/leagues/{lid}/seasons    every season of a league

Across seasons, by date: the web app's Firestore database (project gamesheet-production,
public web API key) holds one document per game in the top-level `games` collection, with
season, teams, divisions and times. A structured query on ScheduledStartTime.Time lists every
GameSheet game of a day across all seasons (about 1,000 to 3,000 per day). Caution: that
indexed field is true UTC for new-schema documents but LOCAL time written as "Z" for older
ones (source game-summary-service), so the query is widened by 14 h each side and filtered
on scheduledTimeGmt, which is true UTC in both schemas. scheduledStartTime is local time.

Report link: https://gamesheetstats.com/seasons/{sid}/games/{game_id}

    .venv/bin/python -m backend.gamesheet_api --index            # every game of the current seasons -> data/gamesheet_games.json
    .venv/bin/python -m backend.gamesheet_api --season 15554     # print one season
    .venv/bin/python -m backend.gamesheet_api --day 2026-09-20   # every game of one UTC day, all seasons (Firestore)
"""
from __future__ import annotations
import asyncio, datetime, json, re, time
from pathlib import Path
import httpx

BASE = "https://gamesheetstats.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json", "Accept-Language": "en-US,en;q=0.8",
           "Referer": "https://gamesheetstats.com/"}
FS_KEY = None   # the public web key the site ships in its JS bundle; read from the site at first use


async def fs_key() -> str | None:
    """GameSheet's public Firebase web key, from GAMESHEET_FS_KEY, a local cache file, or the
    site's own JavaScript (it is the key every visitor's browser uses)."""
    global FS_KEY
    if FS_KEY:
        return FS_KEY
    import os
    cache = DATA / "gamesheet_fs_key.txt"
    FS_KEY = os.environ.get("GAMESHEET_FS_KEY") or (cache.read_text().strip() if cache.exists() else None)
    if FS_KEY:
        return FS_KEY
    _setup()
    try:
        page = (await _client.get(BASE + "/")).text
        for src in re.findall(r'src="(/_next/static/[^"]+\.js)"', page)[:40]:
            js = (await _client.get(BASE + src)).text
            m = re.search(r"AIza[0-9A-Za-z_\-]{35}", js)
            if m:
                FS_KEY = m.group(0)
                cache.write_text(FS_KEY)
                break
    except Exception:
        pass
    return FS_KEY
FS_DOCS = "https://firestore.googleapis.com/v1/projects/gamesheet-production/databases/(default)/documents"
DATA = Path(__file__).resolve().parent.parent / "data"
INDEX_FILE = DATA / "gamesheet_games.json"
PAGE = 1000              # games per unified-games request (the route has no cap; paging keeps replies small)
CONCURRENCY = 3          # parallel requests to gamesheetstats.com
MIN_GAP = 0.15           # seconds between request starts

_client: httpx.AsyncClient | None = None
_sem: asyncio.Semaphore | None = None
_last = 0.0
_pace = None
stats = dict(requests=0, retries=0, rate_limited=0, errors=0)


_loop = None


def _setup():
    global _client, _sem, _pace, _loop
    if _client is not None and _loop is not asyncio.get_running_loop():
        _client = None                  # an earlier asyncio.run owned it
    if _client is None:
        _loop = asyncio.get_running_loop()
        _client = httpx.AsyncClient(headers=HEADERS, timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True,
                                    limits=httpx.Limits(max_connections=CONCURRENCY + 2, max_keepalive_connections=CONCURRENCY + 2))
        _sem = asyncio.Semaphore(CONCURRENCY)
        _pace = asyncio.Lock()


async def close():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _request(method: str, url: str, params: dict | None = None, body: dict | None = None, tries: int = 5):
    """GET/POST returning parsed JSON, or None on a 404 or after repeated failures.
    Paced (MIN_GAP between starts, CONCURRENCY in flight); 429/5xx/403 back off exponentially,
    honouring Retry-After."""
    global _last
    _setup()
    delay = 2.0
    for attempt in range(tries):
        async with _sem:
            async with _pace:
                wait = MIN_GAP - (time.monotonic() - _last)
                if wait > 0:
                    await asyncio.sleep(wait)
                _last = time.monotonic()
            stats["requests"] += 1
            try:
                r = await (_client.post(url, params=params, json=body) if method == "POST" else _client.get(url, params=params))
            except (httpx.TimeoutException, httpx.TransportError):
                r = None
        if r is not None and r.status_code == 200:
            ctype = r.headers.get("content-type", "")
            if "json" in ctype:
                try:
                    return r.json()
                except ValueError:
                    pass
            # an HTML reply to an API URL is a Cloudflare challenge page: back off and retry
        if r is not None and r.status_code == 404:
            return None
        if r is not None and r.status_code == 429:
            stats["rate_limited"] += 1
        stats["retries"] += 1
        ra = r.headers.get("retry-after") if r is not None else None
        await asyncio.sleep(float(ra) if ra and ra.isdigit() else delay)
        delay = min(delay * 2, 60)
    stats["errors"] += 1
    return None


async def api(path: str, **params):
    return await _request("GET", BASE + path, params={k: v for k, v in params.items() if v is not None} or None)


# ---------------------------------------------------------------- seasons

def _season_row(s: dict) -> dict:
    assoc = (s.get("association") or {}).get("title") or ""
    return dict(sid=str(s.get("id")), title=s.get("title") or "", name=(s.get("title") or "") + (f" » {assoc.strip()}" if assoc else ""),
                start=s.get("start"), end=s.get("end"), stats_year=s.get("stats_year"), archived=bool(s.get("archived")),
                active=bool(s.get("is_active")), public=s.get("isPublic", True), sport=s.get("sport"),
                league_id=str((s.get("league") or {}).get("id") or s.get("leagueId") or ""), league=(s.get("league") or {}).get("title"),
                association_id=str((s.get("association") or {}).get("id") or ""), association=assoc.strip())


async def season_info(sid) -> dict | None:
    """Season name, dates, league and association. None when the id does not exist."""
    d = await api(f"/api/season-info/{sid}")
    rows = (d or {}).get("data") or []
    return _season_row(rows[0]) if rows and rows[0].get("id") else None


async def league_seasons(lid) -> list[dict]:
    d = await api(f"/api/leagues/{lid}/seasons")
    return [_season_row(s) for s in (d or {}).get("data") or [] if s.get("id")]


async def season_divisions(sid) -> list[dict]:
    d = await api(f"/api/season-divisions/{sid}")
    return [dict(id=str(x.get("id")), title=x.get("title"), teams=x.get("teams")) for x in (d or {}).get("data") or []]


def is_current(s: dict, today: str | None = None, before_days: int = 60, after_days: int = 30) -> bool:
    """A season whose date range touches [today - before_days, today + after_days]."""
    t = datetime.date.fromisoformat(today) if today else datetime.date.today()
    lo, hi = (t - datetime.timedelta(days=before_days)).isoformat(), (t + datetime.timedelta(days=after_days)).isoformat()
    return bool(s and s.get("start") and s.get("end") and s["start"] <= hi and s["end"] >= lo and (s.get("sport") or "hockey") == "hockey")


async def scan_seasons(ids, today: str | None = None, only_current: bool = True, stop_after_empty: int | None = None) -> list[dict]:
    """season-info for each id (ids in ascending order). With stop_after_empty, stop after that many
    ids in a row that do not exist (the top of the id range)."""
    ids = [str(i) for i in ids]
    out, empty = [], 0
    for i in range(0, len(ids), CONCURRENCY * 4):
        chunk = ids[i:i + CONCURRENCY * 4]
        infos = await asyncio.gather(*(season_info(s) for s in chunk))
        for s in infos:
            if s is None:
                empty += 1
                continue
            empty = 0
            if not only_current or is_current(s, today):
                out.append(s)
        if stop_after_empty and empty >= stop_after_empty:
            break
    return out


async def current_seasons(known_ids=None, today: str | None = None, scan_above: int = 400, via_firestore_days: int = 0) -> list[dict]:
    """Every current hockey season. Sources: the known season ids (e.g. kb["gamesheet_seasons"]),
    new ids above the highest known one (GameSheet numbers seasons in sequence; stops after 60 missing
    ids in a row), and optionally the seasons seen in Firestore games of the last `via_firestore_days` days.
    Filters to seasons whose start..end touches today -60 .. +30 days."""
    known = sorted({int(i) for i in (known_ids or [])})
    seen: dict = {}
    for s in await scan_seasons(known, today):
        seen[s["sid"]] = s
    top = known[-1] if known else 15000
    for s in await scan_seasons(range(top + 1, top + 1 + scan_above), today, stop_after_empty=60):
        seen[s["sid"]] = s
    if via_firestore_days:
        t = datetime.date.fromisoformat(today) if today else datetime.date.today()
        extra = set()
        for k in range(via_firestore_days):
            day = (t - datetime.timedelta(days=k + 1)).isoformat()
            extra |= {g["sid"] for g in await games_by_date(day, day, fields="season")}
        for s in await scan_seasons(sorted(extra - set(seen), key=int), today):
            seen[s["sid"]] = s
    return sorted(seen.values(), key=lambda s: int(s["sid"]))


# ---------------------------------------------------------------- games

def _hhmm(t: str | None) -> str | None:
    m = re.match(r"\s*(\d{1,2}):(\d{2})\s*([AP]M)?", t or "", re.I)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        h = h % 12 + (12 if m.group(3).upper() == "PM" else 0)
    return f"{h:02d}:{mi:02d}"


def _iso(d: str | None) -> str | None:
    try:
        return datetime.datetime.strptime(d.strip(), "%b %d, %Y").date().isoformat()
    except Exception:
        return None


def game_url(sid, game_id) -> str:
    return f"{BASE}/seasons/{sid}/games/{game_id}"


def _game_row(g: dict, sid: str, season_name: str) -> dict:
    h, v = g.get("home") or {}, g.get("visitor") or {}
    gid = g.get("gameId") or g.get("id")
    st = g.get("status")
    score = f"{h.get('goals')}-{v.get('goals')}" if st in ("final", "in_progress", "unofficial") and h.get("goals") is not None else None
    zulu = (g.get("timeStampZulu") or "").rstrip("Z")[:16] or None
    return dict(game_id=str(gid), sid=str(sid), date=_iso(g.get("date")), time=_hhmm(g.get("time")), tz=g.get("timeZoneName"),
                start_utc=zulu, home=(h.get("title") or "").strip(), away=(v.get("title") or "").strip(),
                home_id=str(h.get("id") or ""), away_id=str(v.get("id") or ""),
                division=((h.get("division") or {}).get("title") or (v.get("division") or {}).get("title") or "").strip(),
                away_division=((v.get("division") or {}).get("title") or "").strip(),
                season_name=season_name, status=st, game_type=g.get("gameType"), number=g.get("number"),
                location=g.get("location"), score=score, url=game_url(sid, gid))


async def season_games(sid, season_name: str | None = None) -> list[dict]:
    """Every game of a season (scheduled, in progress and final), oldest first:
    dict(game_id, sid, date 'YYYY-MM-DD' local, time 'HH:MM' local, tz, start_utc 'YYYY-MM-DDTHH:MM',
         home, away, division, away_division, season_name, status, game_type, number, location, score, url).
    Returns [] for an empty season and None when the season could not be read."""
    sid = str(sid)
    info_task = None if season_name is not None else asyncio.ensure_future(season_info(sid))
    raw, offset, total = [], 0, None
    while True:
        d = await api(f"/api/unified-games/{sid}", order="asc", limit=PAGE, offset=offset)
        if d is None:
            if info_task:
                info_task.cancel()
            return None
        page = d.get("data") or []
        raw += page
        total = (d.get("meta") or {}).get("total", total)
        offset += len(page)
        if not page or len(page) < PAGE or (total is not None and offset >= total):
            break
    if info_task:
        info = await info_task
        season_name = (info or {}).get("name") or ""
    seen, out = set(), []
    for g in raw:
        row = _game_row(g, sid, season_name or "")
        if row["game_id"] not in seen:
            seen.add(row["game_id"])
            out.append(row)
    return out


async def all_games(seasons, progress=None) -> dict:
    """season_games for many seasons (list of season dicts from current_seasons, or ids).
    Returns {sid: [games] or None}."""
    items = [(s["sid"], s.get("name")) if isinstance(s, dict) else (str(s), None) for s in seasons]
    out = {}
    done = 0

    async def one(sid, name):
        nonlocal done
        out[sid] = await season_games(sid, name)
        done += 1
        if progress:
            progress(done, len(items), sid, out[sid])
    # the semaphore inside _request limits concurrency; batches keep memory and task count small
    for i in range(0, len(items), CONCURRENCY * 4):
        await asyncio.gather(*(one(sid, name) for sid, name in items[i:i + CONCURRENCY * 4]))
    return out


# ---------------------------------------------------------------- across seasons (Firestore)

def _fs_val(v):
    if v is None:
        return None
    if "mapValue" in v:
        return {k: _fs_val(x) for k, x in (v["mapValue"].get("fields") or {}).items()}
    if "arrayValue" in v:
        return [_fs_val(x) for x in v["arrayValue"].get("values", [])]
    return next(iter(v.values()))


FS_FIELDS = ["id", "season", "home.title", "home.division", "visitor.title", "visitor.division", "scheduledStartTime",
             "scheduledTimeGmt", "timeZoneName", "status", "gameType", "number", "ScheduledStartTime", "league", "association"]


async def games_by_date(d0: str, d1: str | None = None, fields: str = "all", page: int = 1000) -> list[dict]:
    """Every GameSheet game whose UTC start is on days d0..d1 ('YYYY-MM-DD'), across all seasons,
    from the web app's Firestore `games` collection. Rows as season_games (date/time local)
    plus league and association; season_name is the season title without the association."""
    d1 = d1 or d0
    lo = datetime.datetime.fromisoformat(d0)
    hi = datetime.datetime.fromisoformat(d1) + datetime.timedelta(days=1)
    margin = datetime.timedelta(hours=14)   # the indexed time is local time for older documents
    t0, t1 = (lo - margin).strftime("%Y-%m-%dT%H:%M:%SZ"), (hi + margin).strftime("%Y-%m-%dT%H:%M:%SZ")
    lo_s, hi_s = lo.strftime("%Y-%m-%dT%H:%M"), hi.strftime("%Y-%m-%dT%H:%M")
    mask = ["season", "ScheduledStartTime", "scheduledTimeGmt"] if fields == "season" else FS_FIELDS
    out, cursor = [], None
    while True:
        q = {"structuredQuery": {
            "from": [{"collectionId": "games"}],
            "select": {"fields": [{"fieldPath": f} for f in mask]},
            "where": {"compositeFilter": {"op": "AND", "filters": [
                {"fieldFilter": {"field": {"fieldPath": "ScheduledStartTime.Time"}, "op": "GREATER_THAN_OR_EQUAL", "value": {"timestampValue": t0}}},
                {"fieldFilter": {"field": {"fieldPath": "ScheduledStartTime.Time"}, "op": "LESS_THAN", "value": {"timestampValue": t1}}}]}},
            "orderBy": [{"field": {"fieldPath": "ScheduledStartTime.Time"}}, {"field": {"fieldPath": "__name__"}}],
            "limit": page}}
        if cursor:
            q["structuredQuery"]["startAt"] = {"values": cursor, "before": False}
        res = await _request("POST", f"{FS_DOCS}:runQuery", params={"key": await fs_key()}, body=q)
        docs = [x["document"] for x in res or [] if "document" in x]
        for doc in docs:
            f = {k: _fs_val(v) for k, v in (doc.get("fields") or {}).items()}
            season = f.get("season") or {}
            sid = str(season.get("id") or "")
            utc = (f.get("scheduledTimeGmt") or ((f.get("ScheduledStartTime") or {}).get("Time")) or "")[:16]
            if utc and not (lo_s <= utc < hi_s):
                continue   # inside the widened query window only
            if fields == "season":
                out.append(dict(sid=sid, season_name=season.get("title")))
                continue
            local = f.get("scheduledStartTime") or ""
            home, vis = f.get("home") or {}, f.get("visitor") or {}
            gid = str(f.get("id") or doc["name"].rsplit("/", 1)[-1])
            out.append(dict(game_id=gid, sid=sid, date=local[:10] or None, time=local[11:16] or None, tz=f.get("timeZoneName"),
                            start_utc=utc or None,
                            home=(home.get("title") or "").strip(), away=(vis.get("title") or "").strip(),
                            division=((home.get("division") or {}).get("title") or "").strip(),
                            away_division=((vis.get("division") or {}).get("title") or "").strip(),
                            season_name=season.get("title") or "", status=f.get("status"), game_type=f.get("gameType"),
                            number=f.get("number"), league=(f.get("league") or {}).get("title"),
                            association=(f.get("association") or {}).get("title"), url=game_url(sid, gid)))
        if len(docs) < page:
            break
        last = docs[-1]
        ts = ((last.get("fields") or {}).get("ScheduledStartTime") or {}).get("mapValue", {}).get("fields", {}).get("Time")
        if not ts:
            break
        cursor = [ts, {"referenceValue": last["name"]}]
    return out


# ---------------------------------------------------------------- local index

async def build_index(known_ids=None, today: str | None = None, path: Path = INDEX_FILE, log=print) -> dict:
    """Fetch every game of every current season and write {built, seasons: {sid: season}, games: [...]}."""
    t = time.time()
    if known_ids is None:
        try:
            known_ids = list((json.loads((DATA / "knowledge.json").read_text()).get("gamesheet_seasons") or {}).keys())
        except Exception:
            known_ids = []
    seasons = await current_seasons(known_ids, today)
    log(f"{len(seasons)} current seasons in {time.time() - t:.0f}s ({stats['requests']} requests)")
    t2 = time.time()

    def prog(n, total, sid, games):
        if n % 50 == 0 or n == total:
            log(f"  {n}/{total} seasons, {time.time() - t2:.0f}s, {stats['requests']} requests, {stats['rate_limited']} x 429")
    per = await all_games(seasons, prog)
    games = [g for gs in per.values() if gs for g in gs]
    idx = dict(built=datetime.datetime.now().isoformat(timespec="seconds"),
               seconds=dict(seasons=round(t2 - t, 1), games=round(time.time() - t2, 1)),
               failed=[sid for sid, gs in per.items() if gs is None],
               seasons={s["sid"]: s for s in seasons}, games=games)
    path.write_text(json.dumps(idx, separators=(",", ":")))
    log(f"{len(games)} games from {len(seasons)} seasons in {time.time() - t:.0f}s -> {path}")
    return idx


async def _main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", action="store_true")
    ap.add_argument("--season")
    ap.add_argument("--day")
    ap.add_argument("--current", action="store_true")
    a = ap.parse_args()
    try:
        if a.season:
            gs = await season_games(a.season)
            for g in gs or []:
                print(g["date"], g["time"], g["tz"], g["status"], g["division"], "|", g["away"], "@", g["home"], g["url"])
            print(len(gs or []), "games")
        if a.day:
            gs = await games_by_date(a.day)
            print(len(gs), "games,", len({g["sid"] for g in gs}), "seasons")
        if a.current:
            kb = json.loads((DATA / "knowledge.json").read_text())
            ss = await current_seasons(list((kb.get("gamesheet_seasons") or {}).keys()))
            print(len(ss), "current seasons")
        if a.index:
            await build_index()
    finally:
        await close()
    print(stats)


if __name__ == "__main__":
    asyncio.run(_main())

"""PuckTrace API. Run: .venv/bin/uvicorn backend.app:app --port 8765"""
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import lookup

ROOT = Path(__file__).resolve().parent.parent
FRONT = ROOT / "frontend"

app = FastAPI(title="PuckTrace")
KB = lookup.load_kb()

# ---------------------------------------------------------------- access
# Set APP_PASSWORD on a deployed server: every /api call then needs the session cookie that
# /api/login gives for the right password. Locally (no APP_PASSWORD) everything is open.
import hashlib as _hl, hmac as _hm, os as _os, time as _time
from fastapi import Request
from fastapi.responses import JSONResponse

PASSWORD = _os.environ.get("APP_PASSWORD") or ""
_SECRET = (_os.environ.get("APP_SECRET") or PASSWORD or "local").encode()
OPEN = {"/api/login", "/api/session", "/api/healthz"}


def _token() -> str:
    return _hm.new(_SECRET, b"pucktrace-session:" + PASSWORD.encode(), _hl.sha256).hexdigest()


def _signed_in(request: Request) -> bool:
    """The session cookie (pages on the same site), or the same value in the x-pt-session header
    (the page calling the NAS straight from the browser, where cookies of another site don't go)."""
    if not PASSWORD:
        return True
    given = request.cookies.get("pt_session", "") or request.headers.get("x-pt-session", "")
    return _hm.compare_digest(given, _token())


# the page may call this server straight from the browser (not only through Vercel), so Vercel's
# own name lookup of the NAS, which fails for hours at a time, is not in the way
import re as _re_cors
CORS_ORIGIN = _re_cors.compile(r"https://pucktrace\.actorix\.in|https://[a-z0-9-]+\.vercel\.app|http://(localhost|127\.0\.0\.1)(:\d+)?")
CORS_HEADERS = {"access-control-allow-methods": "GET, POST, DELETE, OPTIONS",
                "access-control-allow-headers": "content-type, x-pt-session",
                "access-control-max-age": "7200", "access-control-allow-private-network": "true"}


# which server answered, shown on the page: the home NAS (the Docker image sets APP_VERSION) or the Render backup
SERVER = "render" if _os.environ.get("RENDER") else ("nas" if _os.environ.get("APP_VERSION") else "local")


@app.middleware("http")
async def _guard(request: Request, call_next):
    origin = request.headers.get("origin", "")
    allowed = bool(origin) and bool(CORS_ORIGIN.fullmatch(origin))
    if request.method == "OPTIONS" and allowed:                  # the browser asking before a cross-site call
        from fastapi.responses import Response as _R
        r = _R(status_code=204)
    elif PASSWORD and request.url.path.startswith("/api/") and request.url.path not in OPEN and not _signed_in(request):
        r = JSONResponse({"detail": "Sign in required"}, status_code=401)
    else:
        r = await call_next(request)
    r.headers["x-pt-server"] = SERVER
    r.headers["access-control-expose-headers"] = "x-pt-server"
    if allowed:
        r.headers["access-control-allow-origin"] = origin
        r.headers["vary"] = "origin"
        r.headers.update(CORS_HEADERS)
    return r


class LoginIn(BaseModel):
    password: str
    name: str = ""           # who is signing in (the team shares one password)


# who is using the site right now: name -> last time their page said hello (kept in memory only)
_PRESENT: dict = {}
PRESENCE_S = 150             # a page says hello every minute; silence this long means gone


class PresenceIn(BaseModel):
    name: str


def _clean_name(n: str) -> str:
    import re as _re
    return _re.sub(r"\s+", " ", (n or "").strip())[:40]


def _online() -> list:
    now = _time.time()
    for k in [k for k, t in _PRESENT.items() if now - t > PRESENCE_S]:
        _PRESENT.pop(k, None)
    return sorted(_PRESENT, key=str.lower)


@app.post("/api/presence")
def presence(inp: PresenceIn):
    """The page calls this every minute with the user's name; the answer is everyone online."""
    n = _clean_name(inp.name)
    if n:
        _PRESENT[n] = _time.time()
    return dict(online=_online())


@app.post("/api/login")
def login(inp: LoginIn, request: Request):
    if not PASSWORD:
        return dict(ok=True)
    if not _hm.compare_digest(inp.password.encode(), PASSWORD.encode()):
        raise HTTPException(401, "Wrong password")
    r = JSONResponse(dict(ok=True, token=_token()))
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    r.set_cookie("pt_session", _token(), max_age=30 * 86400, httponly=True, samesite="lax", secure=secure)
    return r


@app.post("/api/logout")
def logout():
    r = JSONResponse(dict(ok=True))
    r.delete_cookie("pt_session")
    return r


@app.get("/api/session")
def session(request: Request):
    ok = _signed_in(request)
    # a signed-in page gets the token too, to call the NAS directly with it (pages signed in before this)
    return dict(required=bool(PASSWORD), ok=ok, **({"token": _token()} if ok and PASSWORD else {}))


@app.post("/api/admin/upload")
async def admin_upload(name: str, request: Request):
    """Put a local data file on a deployed server (signed in): the knowledge base built from
    Club Data and learned spellings are not in the public repo."""
    global KB
    allowed = {"knowledge.json", "aliases_learned.json"}
    if name not in allowed:
        raise HTTPException(400, f"Allowed: {sorted(allowed)}")
    body = await request.body()
    import json as _j
    try:
        _j.loads(body)
    except Exception:
        raise HTTPException(400, "Not valid JSON")
    (ROOT / "data" / name).write_bytes(body)
    if name == "knowledge.json":
        KB = lookup.load_kb()
    return dict(ok=True, bytes=len(body))


@app.get("/api/admin/data")
def admin_data(name: str, since: float = 0):
    """The private data files, for the other server to copy (signed in: both servers share
    APP_PASSWORD and APP_SECRET, so each can send the other a valid session token). With `since`,
    answers 204 unless this copy is newer, so the regular check costs nothing."""
    if name not in {"knowledge.json", "aliases_learned.json"}:
        raise HTTPException(400, "Not a data file")
    f = ROOT / "data" / name
    if not f.exists():
        raise HTTPException(404, "No such file here")
    if name == "knowledge.json" and not KB.get("teams"):
        raise HTTPException(409, "This server's knowledge base is empty; nothing worth copying")
    t = f.stat().st_mtime
    if since and t <= since + 1:
        return Response(status_code=204)
    return Response(f.read_bytes(), media_type="application/json", headers={"x-data-time": str(t), "x-kb-teams": str(len(KB.get("teams") or {}))})


SYNC = dict(at=None, ok=None, error=None)     # the last data sync, shown by /api/healthz


def _kb_size(d: dict) -> int:
    return len(d.get("teams") or {}) + len(d.get("tournaments") or {}) + len(d.get("team_sources") or {})


# the two servers keep the same knowledge base and learned spellings: each checks the other every
# 5 minutes and takes its copy when it is newer (a save on the NAS reaches Render; an update loaded
# on Render while the NAS is off reaches the NAS when it starts)
NAS_URL, RENDER_URL = "https://home-nas.tailfff30a.ts.net", "https://pucktrace-api-8u5i.onrender.com"
DATA_FROM = _os.environ.get("DATA_FROM") or (NAS_URL if SERVER == "render" else RENDER_URL if SERVER == "nas" else "")


async def _get_retry(c, url, tries=3, **kw):
    """The Tailscale hostname now and then fails to resolve from a cloud host: try again before giving up."""
    import asyncio
    last = None
    for i in range(tries):
        try:
            r = await c.get(url, **kw)
            if r.status_code < 500:
                return r
            last = RuntimeError(f"HTTP {r.status_code}")
        except Exception as e:
            last = e
        await asyncio.sleep(1 + i)
    raise last


async def _data_sync_loop():
    import asyncio, httpx, json as _j, time as _t
    from .adapters import base as _base
    global KB
    await asyncio.sleep(20)
    while True:
        try:
            async with httpx.AsyncClient(timeout=90, cookies={"pt_session": _token()}) as c:
                for name in ("knowledge.json", "aliases_learned.json"):
                    f = ROOT / "data" / name
                    mine = f.stat().st_mtime if f.exists() else 0
                    r = await _get_retry(c, f"{DATA_FROM}/api/admin/data", params={"name": name, "since": mine})
                    if r.status_code != 200:
                        continue
                    theirs = float(r.headers.get("x-data-time") or 0)
                    if theirs <= mine + 1:
                        continue                              # an older server that doesn't know `since`
                    data = _j.loads(r.content)                # only valid JSON replaces what is here
                    if name == "knowledge.json" and _kb_size(data) < 0.9 * _kb_size(KB):
                        # never let a thinner knowledge base (a server that restarted empty) replace a full one
                        print(f"[data sync] refused {name} from {DATA_FROM}: {_kb_size(data)} entries vs {_kb_size(KB)} here", flush=True)
                        continue
                    if not (f.exists() and f.read_bytes() == r.content):
                        f.write_bytes(r.content)
                        if name == "knowledge.json":
                            KB = lookup.load_kb()
                        else:
                            _base._ALIASES = None
                            _base.team_similarity.cache_clear()
                        print(f"[data sync] {name} updated from {DATA_FROM}", flush=True)
                    _os.utime(f, (theirs, theirs))            # same time on both: no copying back and forth
            SYNC.update(at=_t.strftime("%Y-%m-%dT%H:%M:%S"), ok=True, error=None)
        except Exception as e:
            SYNC.update(at=_t.strftime("%Y-%m-%dT%H:%M:%S"), ok=False, error=str(e)[:160])
            print("[data sync] failed:", e, flush=True)
        # without a knowledge base yet (a fresh restart), try again every minute
        await asyncio.sleep(int(_os.environ.get("SYNC_EVERY", "60" if not KB.get("teams") else "300")))


_STARTED = _time.time()


def _rss_mb() -> int:
    """Memory in use by this process, from /proc on Linux."""
    try:
        for line in open("/proc/self/status"):
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


@app.api_route("/api/healthz", methods=["GET", "HEAD"])   # uptime monitors check with HEAD
def healthz():
    # open (no sign-in): only whether the published game list is loaded, no names or headers
    gl = globals().get("GAME_LIST") or {}
    return dict(ok=True, server=SERVER, game_list=gl.get("games", 0), game_list_at=gl.get("at"), game_list_from=gl.get("source"), knowledge_base=len(KB.get("teams") or {}) > 0,
                version=(_os.environ.get("APP_VERSION") or _os.environ.get("RENDER_GIT_COMMIT", ""))[:7] or None,
                kb_teams=len(KB.get("teams") or {}), uptime_s=int(_time.time() - _STARTED), rss_mb=_rss_mb(),
                searches=sum(1 for v in _JOBS.values() if v["status"] == "running"), max_searches=_max_searches(),
                game_list_error=gl.get("error"), sync=SYNC)


GAME_LIST_URL = _os.environ.get("GAME_LIST_URL", "https://github.com/Ajinkyaa2004/Hudl/releases/download/game-list/game-list.db.gz")
GAME_LIST = dict(at=None, games=0, error=None)


GAME_LIST_TOKEN = _os.environ.get("GAME_LIST_TOKEN", "")     # a GitHub token with read access, for a private repo
GAME_LIST_MIN = 1000                                           # fewer games than this is not a game list


async def _fetch_game_list(c) -> tuple[bytes, str]:
    """The gzipped game list, from the first source that has it:
    1. the public release link (a public repo),
    2. the GitHub API with GAME_LIST_TOKEN (a private repo),
    3. the other server's copy (NAS <-> Render), which needs no GitHub at all."""
    import re as _re
    errors = []
    # the release keeps a spare copy, uploaded just before the main one: one of the two is always whole
    for url, label in ((GAME_LIST_URL, "github"), (GAME_LIST_URL.replace("game-list.db.gz", "game-list-spare.db.gz"), "github spare")):
        if label == "github spare" and url == GAME_LIST_URL:
            continue
        try:
            r = await c.get(url)
            if r.status_code == 200:
                return r.content, label
            errors.append(f"{label}: HTTP {r.status_code}")
        except Exception as e:
            errors.append(f"{label}: {e}")
    m = _re.search(r"github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/([^/?]+)", GAME_LIST_URL)
    if GAME_LIST_TOKEN and m:
        try:
            owner, repo, tag, name = m.groups()
            h = {"Authorization": f"Bearer {GAME_LIST_TOKEN}", "Accept": "application/vnd.github+json"}
            rel = await c.get(f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}", headers=h)
            aid = next((a["id"] for a in (rel.json().get("assets") or []) if a.get("name") == name), None) if rel.status_code == 200 else None
            if aid:
                r = await c.get(f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{aid}",
                                headers={"Authorization": f"Bearer {GAME_LIST_TOKEN}", "Accept": "application/octet-stream"})
                if r.status_code == 200:
                    return r.content, "github api"
            errors.append(f"GitHub API: HTTP {rel.status_code}" + ("" if aid else ", asset not listed"))
        except Exception as e:
            errors.append(f"GitHub API: {e}")
    if DATA_FROM:
        try:
            r = await _get_retry(c, f"{DATA_FROM}/api/admin/game-list", cookies={"pt_session": _token()})
            if r.status_code == 200:
                return r.content, "the other server"
            errors.append(f"other server: HTTP {r.status_code}")
        except Exception as e:
            errors.append(f"other server: {e}")
    raise RuntimeError("game list download: " + "; ".join(errors))


async def load_game_list() -> int:
    """Download the game list (built on GitHub every 3 hours) and merge it into the local index.
    Only the games table is touched: what the server learned (results, sources) stays."""
    import asyncio, gzip, time
    import httpx
    from . import gameindex
    async with httpx.AsyncClient(follow_redirects=True, timeout=180) as c:
        content, source = await _fetch_game_list(c)
    path = ROOT / "data" / "game-list.db"

    def merge() -> int:
        import io, shutil
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as g, open(path, "wb") as out:
            shutil.copyfileobj(g, out, 1 << 20)       # streamed: never the whole list in memory at once
        con = gameindex.conn()
        with gameindex._lock:
            con.execute("attach database ? as gl", (str(path),))
            try:
                n = con.execute("select count(*) from gl.games").fetchone()[0]
                if n < GAME_LIST_MIN:
                    raise RuntimeError(f"game list from {source} holds only {n} games")
                # games found live on this server since the list was built are kept when newer
                con.execute("""insert or replace into games select g.* from gl.games g left join games l on l.url = g.url
                               where l.url is null or ifnull(l.updated, 0) <= ifnull(g.updated, 0)""")
                con.commit()
            finally:
                con.execute("detach database gl")
        path.unlink(missing_ok=True)
        return n
    n = await asyncio.to_thread(merge)
    GAME_LIST.update(at=time.strftime("%Y-%m-%dT%H:%M:%S"), games=n, error=None, source=source)
    print(f"[game list] {n} games merged from {source}", flush=True)
    return n


_EXPORT = ROOT / "data" / "game-list-export.db.gz"


@app.get("/api/admin/game-list")
async def admin_game_list():
    """This server's game list (the games table only, gzipped) for the other server, rebuilt at most
    once an hour. Refused while this server has too few games to be worth copying."""
    import asyncio, time
    from . import gameindex, export_games
    with gameindex._lock:
        n = gameindex.conn().execute("select count(*) from games").fetchone()[0]
    if n < GAME_LIST_MIN:
        raise HTTPException(503, f"only {n} games here")
    if not _EXPORT.exists() or time.time() - _EXPORT.stat().st_mtime > 3600:
        await asyncio.to_thread(export_games.export, ROOT / "data" / "games.db", _EXPORT)
    return Response(_EXPORT.read_bytes(), media_type="application/gzip")


async def _game_list_loop():
    import asyncio
    await asyncio.sleep(5)
    while True:
        try:
            await load_game_list()
        except Exception as e:
            GAME_LIST["error"] = str(e)[:200]
            print("[game list] failed:", e, flush=True)
        await asyncio.sleep(900 if GAME_LIST["error"] else 3600 if GAME_LIST.get("source") == "the other server" else 3 * 3600)


async def _daily_harvest():
    """Refresh the game index from the bulk sources once a day while the server runs."""
    import asyncio, json, time
    from . import harvest
    status = ROOT / "data" / "harvest_status.json"
    await asyncio.sleep(90)            # the published game list loads first; a harvest only runs without it
    while True:
        try:
            last = json.loads(status.read_text()).get("at") if status.exists() else None
            age = time.time() - time.mktime(time.strptime(last, "%Y-%m-%dT%H:%M:%S")) if last else 1e9
            # with the published game list loaded, the small server needs no harvest of its own;
            # a server without a disk (Render) never harvests: the games would vanish at its next restart
            if SERVER == "render" or (GAME_LIST["at"] and _os.environ.get("HARVEST_WITH_LIST", "0") != "1"):
                await asyncio.sleep(3600)
                continue
            if age > 20 * 3600:
                # HARVEST_SOURCES picks the bulk sources (a small free server can't take them all);
                # HEALTH_DAILY=0 skips the daily source check
                names = [x.strip() for x in _os.environ.get("HARVEST_SOURCES", "").split(",") if x.strip()] or None
                if names is None and _mem_limit_mb() and _mem_limit_mb() <= 1600:
                    # a small box (the 2 GB NAS) harvests only the light sources; GameSheet's hundreds of
                    # seasons and the rest come with the published game list
                    names = ["timetoscore", "hockeytech", "poland"]
                await harvest.run_in_thread(names, log=lambda m: print("[harvest]", m, flush=True), delay_s=180)
                if _os.environ.get("HEALTH_DAILY", "1") != "0":
                    from . import health
                    await asyncio.to_thread(lambda: asyncio.run(health.run()))
        except Exception as e:
            print("[harvest] failed:", e, flush=True)
        await asyncio.sleep(3600)


# ---------------------------------------------------------------- switches to the backup
# When a page moves to the backup it notes why (each way to the NAS: no answer, HTTP error...) and
# reports it to the NAS once it is back. The NAS compares the time with its own record of when it
# was running (runs.json, updated every minute), so the Status page can tell "the NAS was off" from
# "the NAS was running, the way to it failed".
RUNS = ROOT / "data" / "runs.json"
SWITCHES = ROOT / "data" / "switches.jsonl"


def _note_run(new: bool = False):
    import json as _j
    try:
        runs = _j.loads(RUNS.read_text())
    except Exception:
        runs = []
    now = int(_time.time())
    if new or not runs:
        runs.append([int(_STARTED), now])
    else:
        runs[-1][1] = now
    try:
        RUNS.write_text(_j.dumps(runs[-500:]))
    except Exception:
        pass


def _running_at(t: float):
    """True when this server was running at epoch t (to a minute and a half), False when not,
    None when its record does not reach back that far."""
    import json as _j
    try:
        runs = _j.loads(RUNS.read_text())
    except Exception:
        return None
    if not runs or t < runs[0][0] - 60:
        return None
    return any(a - 30 <= t <= b + 90 for a, b in runs)


class SwitchesIn(BaseModel):
    events: list


@app.post("/api/switches")
def switches_add(inp: SwitchesIn):
    import json as _j
    clean = lambda v, n=200: str(v or "")[:n]
    lines = []
    for e in inp.events[:50]:
        if not isinstance(e, dict):
            continue
        reasons = e.get("reasons") if isinstance(e.get("reasons"), dict) else {}
        lines.append(_j.dumps(dict(at=clean(e.get("at"), 40), back_at=clean(e.get("back_at"), 40), name=_clean_name(e.get("name") or ""),
                                   trigger=clean(e.get("trigger"), 80), reasons={clean(k, 20): clean(v) for k, v in list(reasons.items())[:4]},
                                   received=_time.strftime("%Y-%m-%dT%H:%M:%S"), server=SERVER), ensure_ascii=False))
    if lines:
        try:
            old = SWITCHES.read_text().splitlines() if SWITCHES.exists() else []
            SWITCHES.write_text("\n".join((old + lines)[-1000:]) + "\n")
        except Exception:
            pass
    return dict(ok=True, saved=len(lines))


@app.get("/api/switches")
def switches_list(limit: int = 50):
    """The latest moves to the backup, newest first, each marked with whether this server was
    running at that moment."""
    import json as _j, datetime as _dt
    try:
        rows = [_j.loads(l) for l in SWITCHES.read_text().splitlines() if l.strip()]
    except Exception:
        rows = []
    out = []
    for e in reversed(rows[-max(1, min(limit, 200)):]):
        try:
            t = _dt.datetime.fromisoformat(e["at"].replace("Z", "+00:00")).timestamp()
            e["nas_running"] = _running_at(t)
            if e.get("back_at"):
                e["away_s"] = int(_dt.datetime.fromisoformat(e["back_at"].replace("Z", "+00:00")).timestamp() - t)
        except Exception:
            e["nas_running"] = None
        out.append(e)
    return dict(server=SERVER, switches=out)


async def _heartbeat_loop():
    """One line a minute in the container log (memory, searches), so that when the container dies
    the last lines before it show what it was doing; and a note at start of how long the previous
    run lived, read from a small file on disk."""
    import asyncio, json as _j
    f = ROOT / "data" / "heartbeat.json"
    try:
        prev = _j.loads(f.read_text())
        print(f"[alive] started; previous run last seen {prev.get('at')} after {prev.get('uptime_s')} s, rss {prev.get('rss_mb')} MB, {prev.get('searches')} searches running", flush=True)
    except Exception:
        print("[alive] started; no record of a previous run", flush=True)
    first = True
    while True:
        _note_run(new=first)
        first = False
        beat = dict(at=_time.strftime("%Y-%m-%dT%H:%M:%S"), uptime_s=int(_time.time() - _STARTED), rss_mb=_rss_mb(),
                    searches=sum(1 for v in _JOBS.values() if v["status"] == "running"), limit_mb=_mem_limit_mb())
        print(f"[alive] up {beat['uptime_s']} s, rss {beat['rss_mb']} MB of {beat['limit_mb'] or '-'}, {beat['searches']} searches", flush=True)
        try:
            f.write_text(_j.dumps(beat))
        except Exception:
            pass
        await asyncio.sleep(60)


async def _cache_cleanup_loop():
    """Old cached pages are removed a minute after start and then once a day."""
    import asyncio
    from . import fetch
    await asyncio.sleep(60)
    while True:
        try:
            print("[cache] cleaned:", await asyncio.to_thread(fetch.prune_disk_cache), flush=True)
        except Exception as e:
            print("[cache] cleanup failed:", e, flush=True)
        await asyncio.sleep(24 * 3600)


@app.on_event("startup")
async def _start_harvest():
    import asyncio
    if _os.environ.get("GAME_LIST", "1") != "0":
        asyncio.create_task(_game_list_loop())
    asyncio.create_task(_daily_harvest())
    asyncio.create_task(_cache_cleanup_loop())
    asyncio.create_task(_heartbeat_loop())
    if DATA_FROM:
        asyncio.create_task(_data_sync_loop())


class SearchIn(BaseModel):
    header: str


class SaveIn(BaseModel):
    header: str
    protocol: str = ""
    t1link: str = ""
    t2link: str = ""
    comp_site: str = ""


class TeamIn(BaseModel):
    name: str
    site: Optional[str] = None
    schedule: Optional[str] = None
    roster: Optional[str] = None
    ep: Optional[str] = None
    mhr: Optional[str] = None
    country: Optional[str] = None


class TournamentIn(BaseModel):
    name: str
    site: Optional[str] = None
    schedule: Optional[str] = None
    ep: Optional[str] = None
    mhr: Optional[str] = None


class VpnIn(BaseModel):
    sites: List[str]


@app.get("/")
def index():
    return FileResponse(FRONT / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/config")
def config():
    from .roster_pages import LIGHT
    return dict(teams=len(KB["teams"]), tournaments=len(KB["tournaments"]), saved=len(KB.get("history", [])),
                vpn_sites=lookup.load_vpn(), upload_sessions=lookup.UPLOAD_SESSIONS, previews=not LIGHT)


@app.post("/api/search")
async def search(inp: SearchIn, auto: bool = True, rosters: bool = True):
    r = await lookup.search_full(inp.header, KB, rosters=rosters) if auto else lookup.search(inp.header, KB)
    if not r["ok"]:
        raise HTTPException(400, r["error"])
    return r


class RostersIn(BaseModel):
    header: str
    game: Optional[dict] = None     # the report found for this header: rosters are then read from that game


@app.post("/api/rosters")
async def rosters(inp: RostersIn):
    """Roster check for both teams, loaded separately so the report shows first."""
    from .rosters import check_rosters
    r = lookup.search(inp.header, KB)
    if not r["ok"]:
        raise HTTPException(400, r["error"])
    g = {k: inp.game.get(k) for k in ("url", "league", "home", "away", "date") if inp.game.get(k)} if inp.game else None
    # with the report found, the game's own lineups are enough: no page checks or site searches
    return await check_rosters(r["parsed"], r["teams"], games=dict(t1=g, t2=g) if g else None, quick=bool(g))


# ---------------------------------------------------------------- background jobs
# A search can outlast the hosting proxy (Vercel -> Render) on a slow or just-woken server: the
# browser then saw an error while the server finished and remembered the answer, so the same
# header worked in a new tab. Searches now run as jobs: /start returns at once and the page polls
# /api/job/{id}. The same header asked twice shares one job.
import asyncio as _aio, secrets as _secrets, time as _time
_JOBS: dict = {}
_JOB_KEYS: dict = {}
_JOB_SEM = None          # searches at once, see _max_searches()


from . import box as _box
_mem_limit_mb = _box.mem_limit_mb


def _max_searches() -> int:
    """MAX_SEARCHES is a ceiling; a small box gets fewer: a search can take 100 MB, and the box
    must keep room for the game list, the caches and the system (a 2 GB NAS, Render's 512 MB)."""
    ceiling = int(_os.environ.get("MAX_SEARCHES", "3"))
    lim = _mem_limit_mb()
    by_memory = 2 if lim and lim <= 600 else 4 if lim and lim <= 1600 else ceiling
    return max(1, min(ceiling, by_memory))


class _SearchWorkers:
    """Searches run on a few worker threads, each with its own event loop for the server's life.
    Their CPU work (reading league pages, scoring games, rosters) then never holds up the main loop,
    which keeps answering health checks, job polls and presence at once: with 20 people searching,
    a main-loop answer took up to 25 s before, and the page counts a NAS that slow as down.
    SEARCH_WORKERS=0 runs searches on the main loop as before."""
    def __init__(self):
        self.loops = None
        self.free = None

    def _start(self, n: int):
        import threading
        self.loops, self.free = [], _aio.Queue()
        for i in range(n):
            loop = _aio.new_event_loop()
            threading.Thread(target=loop.run_forever, name=f"search-{i}", daemon=True).start()
            self.loops.append(loop)
            self.free.put_nowait(loop)

    async def run(self, factory):
        if _os.environ.get("SEARCH_WORKERS", "1") == "0":
            return await factory()
        if self.loops is None:
            self._start(_max_searches())
        loop = await self.free.get()
        try:
            return await _aio.wrap_future(_aio.run_coroutine_threadsafe(factory(), loop))
        finally:
            self.free.put_nowait(loop)


_WORKERS = _SearchWorkers()


def app_busy() -> bool:
    """True while a search or roster job runs: background work (the daily refresh) can wait."""
    return any(v["status"] == "running" for v in _JOBS.values())


def _job_start(key: str, factory) -> dict:
    now = _time.time()
    for jid in [j for j, v in _JOBS.items() if v["status"] != "running" and now - v["started"] > 600]:
        _JOB_KEYS.pop(_JOBS[jid]["key"], None)
        _JOBS.pop(jid, None)
    jid = _JOB_KEYS.get(key)
    if jid in _JOBS and (_JOBS[jid]["status"] == "running" or now - _JOBS[jid]["started"] < 20):
        return dict(job=jid)
    jid = _secrets.token_urlsafe(9)
    _JOBS[jid] = dict(status="running", key=key, started=now)
    _JOB_KEYS[key] = jid

    async def run():
        global _JOB_SEM
        _JOB_SEM = _JOB_SEM or _aio.Semaphore(_max_searches())
        j = _JOBS[jid]
        try:
            async with _JOB_SEM:
                j["result"] = await _WORKERS.run(factory)
            j["status"] = "done"
        except HTTPException as e:
            j.update(status="error", error=str(e.detail), code=e.status_code)
        except Exception as e:
            j.update(status="error", error=f"{type(e).__name__}: {e}"[:300], code=500)
        j["seconds"] = round(_time.time() - j["started"], 1)
    _aio.create_task(run())
    return dict(job=jid)


async def _search_with_retry(header: str, rosters: bool) -> dict:
    """Sources that time out on a slow server are usually ready a moment later (their pages are
    cached by then), so a search with timed-out sources and no report runs once more."""
    t0 = _time.time()
    r = await lookup.search_full(header, KB, rosters=rosters)
    if not r["ok"]:
        raise HTTPException(400, r["error"])
    a = r.get("auto") or {}
    timed_out = [x for x in a.get("adapters", []) if not x.get("ok") and "timed out" in str(x.get("error") or "")]
    # a second pass helps when a site was slow or the server busy (many searches at once): pages the
    # first pass fetched are cached by then, so it is quick
    if a.get("verdict") != "found" and timed_out and _time.time() - t0 < 60:
        r2 = await lookup.search_full(header, KB, rosters=rosters)
        a2 = r2.get("auto") or {}
        order = {"found": 2, "check": 1}
        if r2.get("ok") and order.get(a2.get("verdict"), 0) >= order.get(a.get("verdict"), 0):
            a2["retried"] = [x["site"] for x in timed_out]
            r = r2
    return r


@app.post("/api/search/start")
async def search_start(inp: SearchIn, rosters: bool = False):
    if not lookup.parse_header(inp.header):
        raise HTTPException(400, "Could not read this header. Use: Team 1 vs Team 2 | Competition | YYYY-MM-DD")
    h = inp.header.strip()
    return _job_start(f"search|{rosters}|{h}", lambda: _search_with_retry(h, rosters))


@app.post("/api/rosters/start")
async def rosters_start(inp: RostersIn):
    h = inp.header.strip()
    return _job_start(f"rosters|{(inp.game or {}).get('url', '')}|{h}", lambda: rosters(RostersIn(header=h, game=inp.game)))


@app.get("/api/job/{jid}")
async def job(jid: str):
    j = _JOBS.get(jid)
    if not j:
        raise HTTPException(404, "Job not found (the server restarted)")
    out = dict(status=j["status"], seconds=j.get("seconds", round(_time.time() - j["started"], 1)))
    if j["status"] == "done":
        out["result"] = j["result"]
    elif j["status"] == "error":
        out.update(error=j["error"], code=j.get("code", 500))
    return out


@app.post("/api/save")
async def save(inp: SaveIn):
    p = lookup.parse_header(inp.header)
    if not p:
        raise HTTPException(400, "Could not parse header")
    if not (inp.protocol or inp.t1link or inp.t2link):
        raise HTTPException(400, "Nothing to save: add a protocol link or roster links")
    lookup.save_result(KB, p, inp.protocol.strip(), inp.t1link.strip(), inp.t2link.strip(), inp.comp_site.strip())
    try:
        await lookup.learn_from_link(inp.protocol.strip(), p)
    except Exception:
        pass
    learned = []
    if inp.protocol.strip():
        from . import learning
        learning.remember(p, inp.header, inp.protocol.strip(), "saved", confirmed=True)
        try:
            learned = learning.learn_aliases(p, inp.protocol.strip())
        except Exception:
            pass
    return dict(ok=True, saved=len(KB["history"]), learned=learned)


@app.get("/api/health")
def health_status():
    import json as _j
    try:
        return _j.loads((ROOT / "data" / "health.json").read_text())
    except Exception:
        return dict(at=None, checks=[])


_HEALTH_RUNNING = False


@app.post("/api/health/run")
async def health_run():
    """Check every source once with a known game. Heavy (about 30 live sites): refused on the Render
    backup, whose 512 MB it can exhaust while it carries everyone's searches, and run one at a time."""
    global _HEALTH_RUNNING
    from . import health
    if SERVER == "render":
        raise HTTPException(409, "The source check runs on the home NAS only. Try again when the footer says Running on Home NAS.")
    if _HEALTH_RUNNING:
        raise HTTPException(409, "A source check is already running. The results appear in about two minutes.")
    _HEALTH_RUNNING = True
    try:
        # on its own thread and event loop: its page parsing never holds up searches and the
        # health pings the page uses to decide whether the NAS is up
        return await _aio.to_thread(lambda: _aio.run(health.run(gentle=True)))
    finally:
        _HEALTH_RUNNING = False


@app.get("/api/learning")
def learning_stats():
    """How much the tool knows: games in the index by source, remembered results, learned names."""
    from . import gameindex, learning
    import json as _j
    try:
        harvest = _j.loads((ROOT / "data" / "harvest_status.json").read_text())
    except Exception:
        harvest = None
    if GAME_LIST["at"] and (not harvest or GAME_LIST["at"] > str(harvest.get("at") or "")):
        harvest = dict(harvest or {}, at=GAME_LIST["at"], source="published game list", games=GAME_LIST["games"])
    return dict(index=gameindex.stats(), learning=learning.stats(), harvest=harvest, game_list=GAME_LIST)


@app.get("/api/history")
def history(limit: int = 30):
    return list(reversed(KB.get("history", [])))[:limit]


@app.get("/api/sources")
def sources(q: str = "", kind: str = "teams", limit: int = 50):
    qn = lookup.norm(q)
    table = KB["teams"] if kind == "teams" else KB["tournaments"]
    rows = [dict(key=k, **v) for k, v in table.items() if not qn or qn in k]
    rows.sort(key=lambda r: r["name"].lower())
    return dict(rows=rows[:limit], total=len(rows))


@app.post("/api/sources/team")
def put_team(inp: TeamIn):
    if not inp.name.strip():
        raise HTTPException(400, "Name required")
    k = lookup.norm(inp.name)
    cur = KB["teams"].get(k, {})
    KB["teams"][k] = dict(name=inp.name.strip(), site=inp.site or None, schedule=inp.schedule or None, roster=inp.roster or None,
                          ep=inp.ep or None, mhr=inp.mhr or None, country=inp.country or cur.get("country"))
    lookup.save_kb(KB)
    return dict(key=k, **KB["teams"][k])


@app.post("/api/sources/tournament")
def put_tournament(inp: TournamentIn):
    if not inp.name.strip():
        raise HTTPException(400, "Name required")
    k = lookup.norm(inp.name)
    KB["tournaments"][k] = dict(name=inp.name.strip(), site=inp.site or None, schedule=inp.schedule or None, ep=inp.ep or None, mhr=inp.mhr or None)
    lookup.save_kb(KB)
    return dict(key=k, **KB["tournaments"][k])


@app.delete("/api/sources/{kind}/{key}")
def delete_source(kind: str, key: str):
    table = KB["teams"] if kind == "team" else KB["tournaments"]
    table.pop(key, None)
    lookup.save_kb(KB)
    return dict(ok=True)


NO_PREVIEW = ("eliteprospects.com", "google.com", "hudl.com", "khl.ru", "chl.ca", "hockeyslovakia.sk", "iihf.com", "hockeydb.com")


_SHOT_SEM = None


@app.get("/api/shot")
async def shot(url: str):
    """Screenshot of a page for the preview panel (cached)."""
    from .fetch import screenshot, cached_screenshot
    d = lookup.domain(url)
    if not url.startswith("http") or any(d.endswith(b) for b in NO_PREVIEW):
        raise HTTPException(404, "This site blocks previews, open it in Chrome")
    from .roster_pages import LIGHT
    if LIGHT:
        # no browser on a small server: a preview made earlier is served, nothing else is tried.
        # (Trying used to start a browser helper per request that was never cleaned up, until the
        # container ran out of memory and was killed.)
        img = cached_screenshot(url)
        if not img:
            raise HTTPException(404, "No preview on this server, open it in Chrome")
        return Response(content=img, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=21600"})
    global _SHOT_SEM
    _SHOT_SEM = _SHOT_SEM or _aio.Semaphore(3)
    async with _SHOT_SEM:
        img = await screenshot(url)
    if not img:
        raise HTTPException(404, "Preview did not load, open it in Chrome")
    return Response(content=img, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=21600"})


@app.get("/api/vpn")
def get_vpn():
    return dict(sites=lookup.load_vpn())


@app.post("/api/vpn")
def set_vpn(inp: VpnIn):
    lookup.save_vpn(inp.sites)
    return dict(sites=lookup.load_vpn())


app.mount("/static", StaticFiles(directory=FRONT), name="static")

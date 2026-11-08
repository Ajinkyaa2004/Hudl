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
# Set APP_PASSWORD on a deployed server: every /api call then needs a session. People sign in with
# their own name and password (backend/users.py, data/users.json); APP_PASSWORD with no name gives
# the service session the servers and the backup job use. Locally (no APP_PASSWORD) everything is open.
import hashlib as _hl, hmac as _hm, os as _os, time as _time
from fastapi import Request
from fastapi.responses import JSONResponse

PASSWORD = _os.environ.get("APP_PASSWORD") or ""
_SECRET = (_os.environ.get("APP_SECRET") or PASSWORD or "local").encode()
OPEN = {"/api/login", "/api/session", "/api/healthz"}


def _token() -> str:
    """The service session: server to server, the backup job and uploads (never given to a page)."""
    return _hm.new(_SECRET, b"pucktrace-service:" + PASSWORD.encode(), _hl.sha256).hexdigest()


def _given(request: Request) -> str:
    return request.cookies.get("pt_session", "") or request.headers.get("x-pt-session", "")


def _user(request: Request) -> "dict | None":
    """The person signed in: their session comes only in the x-pt-session header, from the page's
    own tab storage, so closing the tab signs them out (no cookie keeps them signed in)."""
    from . import users as _users
    return _users.user_of(_SECRET, request.headers.get("x-pt-session", ""))


def _signed_in(request: Request) -> bool:
    """A person's session (header), or the service session (header or cookie: the servers' sync)."""
    if not PASSWORD:
        return True
    return _hm.compare_digest(_given(request), _token()) or _user(request) is not None


# the page may call this server straight from the browser (not only through Vercel), so Vercel's
# own name lookup of the NAS, which fails for hours at a time, is not in the way
import re as _re_cors
CORS_ORIGIN = _re_cors.compile(r"https://(admin\.)?pucktrace\.actorix\.in|https://[a-z0-9-]+\.vercel\.app|http://(localhost|127\.0\.0\.1)(:\d+)?")
CORS_HEADERS = {"access-control-allow-methods": "GET, POST, DELETE, OPTIONS",
                "access-control-allow-headers": "content-type, x-pt-session",
                "access-control-max-age": "7200", "access-control-allow-private-network": "true"}


# which server answered, shown on the page: the home NAS (the Docker image sets APP_VERSION) or the Render backup
SERVER = "render" if _os.environ.get("RENDER") else ("nas" if _os.environ.get("APP_VERSION") else "local")
# the name the page shows. The office PC runs the same code as the NAS (APP_VERSION is set), so it
# is named by SERVER_LABEL when given, else by the OS: a Windows host is the office PC.
SERVER_LABEL = _os.environ.get("SERVER_LABEL") or ("pc" if _os.name == "nt" else SERVER)


@app.middleware("http")
async def _guard(request: Request, call_next):
    origin = request.headers.get("origin", "")
    allowed = bool(origin) and bool(CORS_ORIGIN.fullmatch(origin))
    if request.method == "OPTIONS" and allowed:                  # the browser asking before a cross-site call
        from fastapi.responses import Response as _R
        r = _R(status_code=204)
    elif PASSWORD and request.url.path.startswith("/api/") and request.url.path not in OPEN and not _signed_in(request):
        r = JSONResponse({"detail": "Sign in required"}, status_code=401)
    elif PASSWORD and request.url.path.startswith("/api/admin/") and not (
            _hm.compare_digest(_given(request), _token()) or (_user(request) or {}).get("admin")):
        r = JSONResponse({"detail": "Only an admin can do this"}, status_code=403)
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
def presence(inp: PresenceIn, request: Request):
    """The page calls this every minute; the answer is everyone online. The name is the signed-in
    account's (the typed one only on a server without accounts)."""
    u = _user(request) if PASSWORD else None
    n = u["name"] if u else _clean_name(inp.name) if not PASSWORD else ""
    if n:
        _PRESENT[n] = _time.time()
    return dict(online=_online(), announce=_announce())


@app.post("/api/login")
def login(inp: LoginIn, request: Request):
    if not PASSWORD:
        return dict(ok=True)
    from . import users as _users
    if not inp.name.strip():
        # no name: the service sign-in (servers, the backup job, uploads); the page always sends a name
        if not _hm.compare_digest(inp.password.encode(), PASSWORD.encode()):
            raise HTTPException(401, "Wrong password")
        return dict(ok=True, token=_token())
    u = _users.verify(inp.name, inp.password)
    if not u:
        raise HTTPException(401, "Wrong username or password")
    _users.note_login(u["name"])
    tok = _users.token_for(_SECRET, u)
    r = JSONResponse(dict(ok=True, token=tok, name=u["name"], admin=bool(u.get("admin"))))
    r.delete_cookie("pt_session")         # a cookie from before (30 days) no longer signs anyone in
    return r


# ---------------------------------------------------------------- the Admin screen (admin accounts only:
# every /api/admin/ route is refused to anyone else by _guard)

class UserIn(BaseModel):
    name: str = ""
    password: str = ""        # empty: PuckTrace makes one


class BlockIn(BaseModel):
    blocked: bool


def _push_file(name: str):
    """Send a data file to the other server at once (not at the next 5-minute sync)."""
    if not DATA_FROM or not PASSWORD:
        return

    def push():
        import httpx
        try:
            httpx.post(f"{DATA_FROM}/api/admin/upload", params={"name": name}, content=(ROOT / "data" / name).read_bytes(),
                       headers={"content-type": "application/json", "x-pt-session": _token()}, timeout=300)
        except Exception as e:
            print(f"[push] {name} could not reach the other server:", e, flush=True)
    import threading
    threading.Thread(target=push, daemon=True).start()


def _users_changed(kicked: str = ""):
    """After a change: the person kicked out leaves the online list, and the other server gets the
    accounts at once (not at the next 5-minute sync), so nobody carries on there."""
    from . import users as _users
    if kicked:
        for k in [k for k in _PRESENT if _users._key(k) == _users._key(kicked)]:
            _PRESENT.pop(k, None)
    _push_file("users.json")


def _admin_call(fn, *a, kicked: str = "", **kw):
    try:
        out = fn(*a, **kw)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    _users_changed(kicked)
    return out


@app.get("/api/admin/users")
def admin_users(request: Request):
    from . import users as _users
    online = {_users._key(n) for n in _online()}
    me = (_user(request) or {}).get("name", "")
    rows = [dict(_users.public(u), online=_users._key(u["name"]) in online, me=_users._key(u["name"]) == _users._key(me))
            for u in _users.users().values()]
    return dict(users=sorted(rows, key=lambda r: (not r["admin"], not r["online"], r["name"].lower())))


@app.post("/api/admin/users")
def admin_user_add(inp: UserIn):
    from . import users as _users
    pw = _admin_call(_users.add, inp.name, inp.password or None)
    return dict(ok=True, name=_users.clean_name(inp.name), password=pw)


@app.post("/api/admin/users/{name}/password")
def admin_user_password(name: str, inp: UserIn):
    from . import users as _users
    pw = _admin_call(_users.new_password, name, inp.password or None, kicked=name)
    return dict(ok=True, name=name, password=pw)


@app.post("/api/admin/users/{name}/signout")
def admin_user_signout(name: str):
    from . import users as _users
    _admin_call(_users.sign_out, name, kicked=name)
    return dict(ok=True)


@app.post("/api/admin/users/{name}/block")
def admin_user_block(name: str, inp: BlockIn, request: Request):
    from . import users as _users
    _admin_call(_users.set_blocked, name, inp.blocked, me=(_user(request) or {}).get("name", ""), kicked=name if inp.blocked else "")
    return dict(ok=True)


@app.delete("/api/admin/users/{name}")
def admin_user_delete(name: str, request: Request):
    from . import users as _users
    _admin_call(_users.delete, name, me=(_user(request) or {}).get("name", ""), kicked=name)
    return dict(ok=True)


# ---------------------------------------------------------------- announcement banner

ANNOUNCE = ROOT / "data" / "announce.json"


def _announce() -> "dict | None":
    """The banner every page shows, until it is cleared or its end time passes."""
    import json as _j, datetime as _dt
    try:
        a = _j.loads(ANNOUNCE.read_text())
    except Exception:
        return None
    if not a.get("text"):
        return None
    if a.get("until") and a["until"] < _dt.datetime.now(_dt.timezone.utc).isoformat():
        return None
    return {k: a.get(k) for k in ("id", "text", "level", "until", "at")}


class AnnounceIn(BaseModel):
    text: str = ""
    level: str = "info"       # info | warn
    hours: float = 0          # 0: until cleared


@app.get("/api/announce")
def announce_get():
    return dict(announce=_announce())


@app.post("/api/admin/announce")
def announce_set(inp: AnnounceIn, request: Request):
    import json as _j, datetime as _dt
    text = (inp.text or "").strip()[:500]
    now = _dt.datetime.now(_dt.timezone.utc)
    a = dict(id=_secrets.token_hex(4), text=text, level="warn" if inp.level == "warn" else "info",
             until=(now + _dt.timedelta(hours=inp.hours)).isoformat() if text and inp.hours > 0 else None,
             at=now.isoformat(), by=(_user(request) or {}).get("name", ""))
    ANNOUNCE.write_text(_j.dumps(a if text else dict(text="", at=now.isoformat())))
    _push_file("announce.json")
    return dict(ok=True, announce=_announce())


# ---------------------------------------------------------------- usage and live activity

_LOG_CACHE: dict = {"at": 0, "rows": []}


def _log_rows() -> list:
    """The search log (the Google Sheet: every search on both servers, with names), cached 2 minutes."""
    if _time.time() - _LOG_CACHE["at"] > 120:
        from . import sheet_report
        _LOG_CACHE.update(rows=sheet_report.log_rows(), at=_time.time())
    return _LOG_CACHE["rows"]


@app.get("/api/admin/usage")
async def admin_usage(days: int = 7):
    """Per person: searches, found / pick one / not found, average time, today and over `days`."""
    import asyncio, collections, datetime as _dt
    try:
        rows = await asyncio.to_thread(_log_rows)
    except Exception as e:
        raise HTTPException(502, f"Could not read the search log: {e}"[:200])
    today = (_dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)).date()
    since = (today - _dt.timedelta(days=max(1, days) - 1)).isoformat()
    per = collections.defaultdict(lambda: dict(today=0, n=0, found=0, pick=0, none=0, secs=[], last=""))
    daily = collections.Counter()
    for r in rows:
        t = _re_cors.sub(r" (\d):", r" 0\1:", (r.get("Time (IST)") or "").strip())   # the log writes "6:55:32"
        if t[:10] < since:
            continue
        name = (r.get("Name") or "-").strip() or "-"
        res = (r.get("Result") or "").strip() or ("Found" if (r.get("Found") or "").strip() == "Yes" else "Nothing")
        p = per[name]
        p["n"] += 1
        p["today"] += t[:10] == today.isoformat()
        p["found" if res == "Found" else "pick" if res == "Shortlist" else "none"] += 1
        try:
            p["secs"].append(float(r.get("Search time (s)") or ""))
        except ValueError:
            pass
        p["last"] = max(p["last"], t)
        daily[t[:10]] += 1
    out = []
    for name, p in per.items():
        sec = sorted(p.pop("secs"))
        out.append(dict(name=name, **p, found_pct=round(100 * p["found"] / p["n"]) if p["n"] else 0,
                        median_s=sec[len(sec) // 2] if sec else None))
    out.sort(key=lambda x: (-x["n"], x["name"].lower()))
    return dict(days=days, since=since, people=out, total=sum(x["n"] for x in out),
                daily=[dict(date=d, n=daily[d]) for d in sorted(daily)])


@app.get("/api/admin/activity")
async def admin_activity(local: int = 0):
    """Searches running now and the last finished ones, on this server and (unless local) the other."""
    now = _time.time()
    running = [dict(who=j.get("who") or "-", header=j.get("header", ""), secs=round(now - j["started"]), server=SERVER)
               for j in list(_JOBS.values()) if j["status"] == "running" and j.get("header")]
    recent = [dict(r, ago=round(now - r["at"])) for r in _RECENT]
    other = None
    if not local and DATA_FROM and PASSWORD:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=8) as c:
                o = (await c.get(f"{DATA_FROM}/api/admin/activity", params={"local": 1}, headers={"x-pt-session": _token()})).json()
            running += o.get("running", [])
            recent += o.get("recent", [])
            other = "ok"
        except Exception:
            other = "unreachable"
    recent.sort(key=lambda r: r["ago"])
    return dict(running=sorted(running, key=lambda r: -r["secs"]), recent=recent[:30], online=_online(), other=other)


# ---------------------------------------------------------------- RTT sheet upload (learning)

_LEARN: dict = dict(status="idle", log=[], plan=None, result=None, at=None)


def _learn_log(line: str):
    _LEARN["log"].append(str(line)[:300])
    del _LEARN["log"][:-300]
    print("[sheet]", line, flush=True)


@app.get("/api/admin/sheet")
def admin_sheet_state():
    pl = _LEARN.get("plan") or {}
    return dict(status=_LEARN["status"], log=_LEARN["log"][-80:], at=_LEARN["at"], result=_LEARN["result"],
                rows=len(pl.get("rows") or []), why=pl.get("why"), pairs=[list(x) for x in pl.get("pairs") or []])


@app.post("/api/admin/sheet")
async def admin_sheet_upload(request: Request):
    """The RTT Progress sheet (.xlsx) from the Admin screen: saved as data/progress.xlsx, then read
    for what it would teach. Only on the main server: Render has too little memory for it."""
    import asyncio, datetime as _dt
    if SERVER == "render":
        raise HTTPException(409, "The backup server can't learn from the sheet: switch the main server on and try again")
    if _LEARN["status"] in ("planning", "applying"):
        raise HTTPException(409, "A sheet is being read already")
    body = await request.body()
    if body[:2] != b"PK":
        raise HTTPException(400, "That is not an .xlsx file")
    (ROOT / "data" / "progress.xlsx").write_bytes(body)
    _LEARN.update(status="planning", log=[], plan=None, result=None, at=_dt.datetime.now().isoformat(timespec="seconds"))
    _learn_log(f"sheet received ({len(body) // 1024} KB), reading it")

    async def run():
        from . import learn_sheet
        try:
            _LEARN["plan"] = await asyncio.to_thread(learn_sheet.plan, _learn_log)
            _LEARN["status"] = "ready"
        except Exception as e:
            _LEARN.update(status="error")
            _learn_log(f"error: {type(e).__name__}: {e}")
    asyncio.create_task(run())
    return dict(ok=True)


class SheetApplyIn(BaseModel):
    block: list = []          # [[site spelling, header spelling], ...] the admin unticked
    accuracy: bool = True     # then refresh the dashboard's Accuracy tab


@app.post("/api/admin/sheet/apply")
async def admin_sheet_apply(inp: SheetApplyIn):
    import asyncio
    if _LEARN["status"] != "ready" or not _LEARN.get("plan"):
        raise HTTPException(409, "Upload a sheet first")
    _LEARN["status"] = "applying"
    block = [tuple(x[:2]) for x in inp.block if isinstance(x, (list, tuple)) and len(x) >= 2]

    async def run():
        global KB
        from . import learn_sheet, sheet_report
        from .adapters import base as _base
        try:
            r = await asyncio.to_thread(learn_sheet.apply, _LEARN["plan"], block, _learn_log)
            KB = lookup.load_kb()
            _base._ALIASES = None
            _base.team_similarity.cache_clear()
            for f in ("knowledge.json", "aliases_learned.json"):
                _push_file(f)
            _learn_log("knowledge and spellings in use here, and sent to the other server")
            if inp.accuracy:
                rows = await asyncio.to_thread(lambda: sheet_report.verdicts(sheet_report.log_rows()))
                import collections
                c = collections.Counter(x[5] for x in rows)
                ok, wrong = c[sheet_report.V_OK], c[sheet_report.V_WRONG]
                sent = await asyncio.to_thread(sheet_report.send, rows)
                r["accuracy"] = f"{100 * ok / (ok + wrong):.1f}% ({ok} of {ok + wrong})" if ok + wrong else None
                _learn_log(f"Accuracy tab refreshed: {r['accuracy']} ({sent[:60]})")
            _LEARN.update(status="done", result=r)
        except Exception as e:
            _LEARN.update(status="error")
            _learn_log(f"error: {type(e).__name__}: {e}")
    asyncio.create_task(run())
    return dict(ok=True)


@app.post("/api/logout")
def logout():
    r = JSONResponse(dict(ok=True))
    r.delete_cookie("pt_session")
    return r


@app.get("/api/session")
def session(request: Request):
    u = _user(request) if PASSWORD else None
    # a signed-in page gets its token too, to call the NAS directly with it; only a person's session counts here
    return dict(required=bool(PASSWORD), ok=bool(u) or not PASSWORD,
                **({"token": request.headers.get("x-pt-session", ""), "name": u["name"], "admin": bool(u.get("admin"))} if u else {}))


# private data files the servers receive by upload and keep in step with each other; the GameSheet
# team list lets the roster lookup find GameSheet teams whose game wasn't found
DATA_FILES = ("knowledge.json", "aliases_learned.json", "sheet_log.json", "gamesheet_teams.json", "users.json", "announce.json")


@app.post("/api/admin/upload")
async def admin_upload(name: str, request: Request):
    """Put a local data file on a deployed server (signed in): the knowledge base built from
    Club Data and learned spellings are not in the public repo."""
    global KB
    allowed = set(DATA_FILES)
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
    if name not in DATA_FILES:
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
NAS_URL, RENDER_URL = "https://nas.actorix.in", "https://pucktrace-api-8u5i.onrender.com"   # the NAS by its Cloudflare Tunnel: Render often fails to look up the Tailscale name
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
                for name in DATA_FILES:
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
        if not KB.get("teams") and _os.environ.get("KB_KEY"):
            # still empty (a restart while the NAS is off): open the locked copy kept on GitHub
            try:
                from . import kbvault
                done = await asyncio.to_thread(kbvault.restore, ROOT / "data", _os.environ["KB_KEY"])
                if done:
                    KB = lookup.load_kb()
                    _base._ALIASES = None
                    _base.team_similarity.cache_clear()
                    SYNC.update(vault=_t.strftime("%Y-%m-%dT%H:%M:%S"))
                    print(f"[data sync] restored {', '.join(done)} from the locked backup", flush=True)
            except Exception as e:
                print("[data sync] locked backup failed:", e, flush=True)
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
    return dict(ok=True, server=SERVER, server_label=SERVER_LABEL, game_list=gl.get("games", 0), game_list_at=gl.get("at"), game_list_from=gl.get("source"), knowledge_base=len(KB.get("teams") or {}) > 0,
                version=(_os.environ.get("APP_VERSION") or _os.environ.get("RENDER_GIT_COMMIT", ""))[:7] or None,
                kb_teams=len(KB.get("teams") or {}), uptime_s=int(_time.time() - _STARTED), rss_mb=_rss_mb(),
                searches=sum(1 for v in _JOBS.values() if v["status"] == "running"), max_searches=_max_searches(),
                game_list_error=gl.get("error"), sync=SYNC, backup_key=bool(_os.environ.get("KB_KEY")))


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
    path = ROOT / "data" / "game-list-download.db"     # never the export's scratch file

    def merge() -> int:
        import io, shutil
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as g, open(path, "wb") as out:
            shutil.copyfileobj(g, out, 1 << 20)       # streamed: never the whole list in memory at once
        con = gameindex.conn()
        with gameindex._lock:
            # a failed merge must never leave the list attached: an open transaction kept 'gl' attached
            # and every later merge failed ('database gl is already in use') until a restart
            try:
                con.rollback()
                con.execute("detach database gl")
            except Exception:
                pass
            con.execute("attach database ? as gl", (str(path),))
            try:
                n = con.execute("select count(*) from gl.games").fetchone()[0]
                if n < GAME_LIST_MIN:
                    raise RuntimeError(f"game list from {source} holds only {n} games")
                # games found live on this server since the list was built are kept when newer
                con.execute("""insert or replace into games select g.* from gl.games g left join games l on l.url = g.url
                               where l.url is null or ifnull(l.updated, 0) <= ifnull(g.updated, 0)""")
                try:                                   # games their source deleted (lists from before have no record)
                    con.execute("delete from games where url in (select url from gl.removed)")
                    con.execute("insert or replace into removed select url, at from gl.removed")
                except Exception:
                    pass
                con.commit()
            except Exception:
                con.rollback()
                raise
            finally:
                try:
                    con.execute("detach database gl")
                except Exception:
                    pass
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


# ---------------------------------------------------------------- tunnel and machine diagnostics
# The Status page says "the PC was up, the way in was not" but cannot say *why*. This reports what
# the machine itself can see: the cloudflared service and its command line, the tail of its log, the
# network adapter and its power saving, memory and disk, and the app's health down two paths. It is
# read-only and admin-only, and it never reads env.cmd or prints a secret.
_DIAG_FILES = ("C:\\PuckTrace\\logs", "C:\\ProgramData\\cloudflared",
               "C:\\Windows\\System32\\config\\systemprofile\\.cloudflared", "C:\\cloudflared")
import re as _re
_REDACT = _re.compile(r"(?i)^\s*(tunnelsecret|secret|token|password|api[-_]?token)\b")


def _redact(text: str) -> str:
    out = []
    for line in (text or "").splitlines():
        out.append("***" if _REDACT.search(line) else line)
    return "\n".join(out)


@app.get("/api/admin/diag")
def admin_diag(deep: int = 0):
    """Admin only. Read-only look at the machine that serves PuckTrace: cloudflared, its log, the
    adapter, power saving, memory and disk, plus the app reached locally and through the tunnel."""
    import shutil, subprocess, time as _t, urllib.request
    from concurrent.futures import ThreadPoolExecutor

    def run(args, timeout=25):
        try:
            p = subprocess.run(args, capture_output=True, timeout=timeout,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            text = (p.stdout or b"") + (b"\n" + p.stderr if p.stderr else b"")
            return text.decode("utf-8", "replace").strip()[:20000]
        except FileNotFoundError:
            return f"not found: {args[0]}"
        except Exception as e:                        # a blocked command must not fail the report
            return f"{type(e).__name__}: {e}"

    def ps(script, timeout=25):
        return run([shutil.which("powershell") or "powershell", "-NoProfile", "-NonInteractive",
                    "-Command", script], timeout)

    def probe(url, tries=3):
        # a browser name matters: Cloudflare's managed rules answer 403 to Python's default agent,
        # which would look like an outage
        browser = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                 "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}
        got = []
        for _ in range(tries):
            t0 = _t.time()
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=browser), timeout=8) as r:
                    r.read()
                    got.append(dict(status=r.status, ms=round((_t.time() - t0) * 1000)))
            except Exception as e:
                got.append(dict(status=getattr(e, "code", None), ms=round((_t.time() - t0) * 1000),
                                error=f"{type(e).__name__}: {e}"))
            _t.sleep(0.4)
        return dict(url=url, tries=got,
                    fails=sum(1 for g in got if g.get("error") or (g.get("status") or 500) >= 500))

    if _os.name != "nt":
        return dict(server=SERVER, label=SERVER_LABEL, at=_time.strftime("%Y-%m-%dT%H:%M:%S"),
                    note="this is not the office PC; nothing to read here", probes=[])

    jobs = {
        "machine": ("Get-CimInstance Win32_ComputerSystem | Select Manufacturer,Model,TotalPhysicalMemory | ConvertTo-Json -Compress;"
                    "Get-CimInstance Win32_OperatingSystem | Select TotalVisibleMemorySize,FreePhysicalMemory,LastBootUpTime | ConvertTo-Json -Compress;"
                    "Get-PSDrive C | Select Used,Free | ConvertTo-Json -Compress", 25),
        "adapters": ("Get-NetAdapter | Select Name,Status,LinkSpeed,MediaType,InterfaceDescription | ConvertTo-Json -Compress", 25),
        "routes": ("Find-NetRoute -RemoteIPAddress 1.1.1.1 | Select-Object InterfaceAlias,IPAddress,NextHop,RouteMetric,InterfaceMetric | ConvertTo-Json -Compress;"
                   "Get-NetIPConfiguration | ForEach-Object { [pscustomobject]@{ If=$_.InterfaceAlias; IPv4=(@($_.IPv4Address.IPAddress) -join ',');"
                   " Gateway=(@($_.IPv4DefaultGateway.NextHop) -join ',') } } | ConvertTo-Json -Compress", 25),
        "adapter_power": ("Get-NetAdapter | Where-Object Status -eq 'Up' | ForEach-Object { $n = $_.Name;"
                          " $p = Get-NetAdapterPowerManagement -Name $n -ErrorAction SilentlyContinue;"
                          " '{0} | turn-off-device={1} | sleep-on-disconnect={2}' -f $n, $p.AllowComputerToTurnOffDevice, $p.DeviceSleepOnDisconnect }", 25),
        "wifi": ("netsh wlan show interfaces", 15),
        "power": ("powercfg /a;"
                  "powercfg /q SCHEME_CURRENT 19cbb8fa-5279-450e-9fac-8a3d5fedd0c1 | Select-String -Pattern 'Power Saving Mode|Current AC|Current DC|Index'", 20),
        "ports": ("Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in 80,443,8000,7844 } |"
                  " Select-Object LocalAddress,LocalPort,OwningProcess | Sort-Object LocalPort | Format-Table -AutoSize | Out-String", 20),
        "cloudflared_service": ("Get-Service *cloudflared* | Select Name,Status,StartType | ConvertTo-Json -Compress;"
                                "Get-CimInstance Win32_Process -Filter \"Name='cloudflared.exe'\" | Select ProcessId,CommandLine | ConvertTo-Json -Compress", 25),
        "cloudflared_version": ("$c = (Get-Command cloudflared.exe -ErrorAction SilentlyContinue).Source;"
                                "if (-not $c) { $c = (Get-ChildItem 'C:\\Program Files*\\cloudflared\\cloudflared.exe','C:\\PuckTrace\\cloudflared.exe' -ErrorAction SilentlyContinue | Select-Object -First 1).FullName };"
                                "if ($c) { \"exe: $c\"; & $c --version 2>&1 } else { 'cloudflared.exe not found' }", 20),
        "cloudflared_logs": ("$d = " + ",".join(f"'{x}'" for x in _DIAG_FILES) + ";"
                             "Get-ChildItem 'C:\\ProgramData\\cloudflared' -ErrorAction SilentlyContinue | Select-Object Name,Length,LastWriteTime | Format-Table -AutoSize | Out-String;"
                             "$f = Get-ChildItem $d -Filter 'cloudflared*.log' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime;"
                             "if (-not $f) { 'no cloudflared log file: the service logs to the Windows Event Log' }"
                             "foreach ($x in $f) { \"`n== $($x.FullName) ==\"; Get-Content $x.FullName -Tail 1500 |"
                             " Select-String -Pattern 'ERR|error|warn|retry|reconnect|timeout|502|503|504|Unable|failed|Registered tunnel' |"
                             " Select-Object -Last 50 | ForEach-Object { $_.Line } }", 30),
        "cloudflared_events": ("$e = Get-WinEvent -ProviderName Cloudflared -MaxEvents 150 -ErrorAction SilentlyContinue;"
                               "if (-not $e) { 'no events from the Cloudflared provider' } else { $e |"
                               " ForEach-Object { '{0} | {1} | {2} | {3}' -f $_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss'), $_.Id, $_.LevelDisplayName, ($_.Message -replace '\\s+',' ') } }", 40),
        "events": ("Get-WinEvent -FilterHashtable @{LogName='Application';StartTime=(Get-Date).AddDays(-3)} -ErrorAction SilentlyContinue |"
                   " Where-Object { $_.ProviderName -match 'cloudflared' -or $_.Message -match 'cloudflared' } |"
                   " Select-Object -First 15 TimeCreated,Id,LevelDisplayName,Message | Format-List | Out-String", 40),
    }
    if deep:
        jobs["uplink"] = ("Test-Connection 1.1.1.1 -Count 20 -ErrorAction SilentlyContinue |"
                          " Measure-Object -Property ResponseTime -Average -Minimum -Maximum | Format-List | Out-String", 40)

    out = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {k: pool.submit(ps, script, timeout) for k, (script, timeout) in jobs.items()}
        for k, f in futures.items():
            out[k] = _redact(f.result())

    # which link actually carries traffic: connect to the internet from each adapter's own address.
    # Windows picks one default route, so this is the only way to know whether the wired link works.
    def via(ip):
        import socket
        s = socket.socket()
        s.settimeout(6)
        t0 = _t.time()
        try:
            s.bind((ip, 0))
            s.connect(("1.1.1.1", 443))
            return dict(ip=ip, ok=True, ms=round((_t.time() - t0) * 1000))
        except Exception as e:
            return dict(ip=ip, ok=False, ms=round((_t.time() - t0) * 1000),
                        error=f"{type(e).__name__}: {e}")
        finally:
            s.close()

    ifs = []
    for line in (_win_ps("Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } | ForEach-Object {"
                         " '{0}|{1}' -f $_.InterfaceAlias, (@($_.IPv4Address.IPAddress) -join ',') }") or "").splitlines():
        if "|" in line:
            alias, addrs = line.split("|", 1)
            for ip in addrs.split(","):
                if ip.strip():
                    ifs.append(dict(alias=alias.strip(), **via(ip.strip())))
    out["interfaces"] = ifs

    out["probes"] = [probe("http://127.0.0.1:8000/api/healthz", tries=2),
                     probe("https://nas.actorix.in/api/healthz", tries=5),
                     probe("https://pucktrace-api-8u5i.onrender.com/api/healthz", tries=1)]
    return dict(server=SERVER, label=SERVER_LABEL, at=_time.strftime("%Y-%m-%dT%H:%M:%S"),
                version=_os.environ.get("APP_VERSION"), **out)


# ---------------------------------------------------------------- reversible fixes (admin only)
# One action, one change, then check that the site still answers through the tunnel; undo it if not.
# What was changed is kept in data/net_revert.json so `revert_network` works even after a restart.
_NET_STATE = ROOT / "data" / "net_revert.json"
_CF_KEY = "HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Cloudflared"
_BROWSER_UA = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}


def _win_ps(script, timeout=45):
    import shutil, subprocess
    try:
        p = subprocess.run([shutil.which("powershell") or "powershell", "-NoProfile", "-NonInteractive",
                            "-Command", script], capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return ((p.stdout or b"") + (b"\n" + p.stderr if p.stderr else b"")).decode("utf-8", "replace").strip()[:4000]
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def _site_ok(tries=5):
    """Does the public name answer through the tunnel right now? Every change is judged by this."""
    import urllib.request
    ok = fails = 0
    last = None
    for _ in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    "https://nas.actorix.in/api/healthz", headers=_BROWSER_UA), timeout=8) as r:
                r.read()
                ok += 1
        except Exception as e:
            fails += 1
            last = f"{type(e).__name__}: {e}"
        _time.sleep(0.3)
    return dict(ok=ok, fails=fails, last=last)


def _net_state():
    import json as _j
    try:
        return _j.loads(_NET_STATE.read_text())
    except Exception:
        return {}


def _net_save(state):
    import json as _j
    _NET_STATE.parent.mkdir(parents=True, exist_ok=True)
    _NET_STATE.write_text(_j.dumps(state, indent=1))


def _links():
    """Every adapter that has a default gateway, plus whether the internet is reachable from its own
    address. Binding the socket is the only way to test a link Windows is not routing through."""
    import socket
    out = []
    for line in (_win_ps("Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } | ForEach-Object {"
                         " '{0}|{1}' -f $_.InterfaceAlias, (@($_.IPv4Address.IPAddress) -join ',') }") or "").splitlines():
        if "|" not in line:
            continue
        alias, addrs = line.split("|", 1)
        for ip in [a.strip() for a in addrs.split(",") if a.strip()]:
            s = socket.socket()
            s.settimeout(6)
            try:
                s.bind((ip, 0))
                s.connect(("1.1.1.1", 443))
                out.append(dict(alias=alias.strip(), ip=ip, ok=True))
            except Exception as e:
                out.append(dict(alias=alias.strip(), ip=ip, ok=False, error=f"{type(e).__name__}: {e}"))
            finally:
                s.close()
    return out


def _revert(state):
    """Put the previous settings back. Safe to call when nothing was changed."""
    notes = []
    if state.get("cloudflared_imagepath"):
        old = state["cloudflared_imagepath"]
        notes.append(_win_ps(f"Set-ItemProperty -Path '{_CF_KEY}' -Name ImagePath -Value '{old}';"
                             "Restart-Service Cloudflared -Force; Start-Sleep -Seconds 8; (Get-Service Cloudflared).Status"))
        state.pop("cloudflared_imagepath", None)
    if state.get("metrics"):
        parts = [f"Set-NetIPInterface -InterfaceAlias '{a}' -AddressFamily IPv4 -InterfaceMetric {int(v)}"
                 for a, v in state["metrics"].items() if str(v).strip().isdigit()]
        if parts:
            notes.append(_win_ps(";".join(parts)))
        state.pop("metrics", None)
    _net_save(state)
    return notes


class FixIn(BaseModel):
    action: str


@app.post("/api/admin/diag/fix")
def admin_diag_fix(inp: FixIn):
    """Admin only. One small, reversible change to the office PC, verified through the tunnel."""
    if _os.name != "nt":
        raise HTTPException(400, "only the office PC can apply these")
    action = (inp.action or "").strip().lower()
    state = _net_state()
    out = dict(action=action)

    if action == "state":
        out["links"] = _links()
        out["metrics"] = _win_ps("Get-NetIPInterface -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -in 'Ethernet','Wi-Fi' } |"
                                 " ForEach-Object { $_.InterfaceAlias + '=' + $_.InterfaceMetric }")
        out["cloudflared_image_path"] = _win_ps(f"(Get-ItemProperty '{_CF_KEY}').ImagePath").strip().strip('"')
        out["saved"] = state
        return out

    if action == "revert_network":
        out["reverted"] = _revert(state)
        out["after"] = _site_ok(3)
        return out

    out["before"] = _site_ok(3)

    if action == "prefer_ethernet":
        out["links"] = _links()
        if not [l for l in out["links"] if l["alias"].lower() == "ethernet" and l["ok"]]:
            out["result"] = "Ethernet has no internet from its own address; nothing changed"
            return out
        if "metrics" not in state:
            saved = {}
            for alias in ("Ethernet", "Wi-Fi"):
                v = _win_ps(f"(Get-NetIPInterface -InterfaceAlias '{alias}' -AddressFamily IPv4).InterfaceMetric").strip()
                if v.isdigit():
                    saved[alias] = int(v)
            state["metrics"] = saved
            _net_save(state)
        out["changed"] = _win_ps("Set-NetIPInterface -InterfaceAlias Ethernet -AddressFamily IPv4 -InterfaceMetric 10;"
                                 "Set-NetIPInterface -InterfaceAlias Wi-Fi -AddressFamily IPv4 -InterfaceMetric 60;"
                                 "(Get-NetIPInterface -InterfaceAlias Ethernet -AddressFamily IPv4).InterfaceMetric;"
                                 "(Get-NetIPInterface -InterfaceAlias Wi-Fi -AddressFamily IPv4).InterfaceMetric")
        _time.sleep(3)

    elif action == "wifi_performance":
        out["changed"] = _win_ps(
            "Set-NetAdapterPowerManagement -Name 'Wi-Fi' -AllowComputerToTurnOffDevice Disabled -ErrorAction SilentlyContinue;"
            "Set-NetAdapterPowerManagement -Name 'Ethernet' -AllowComputerToTurnOffDevice Disabled -ErrorAction SilentlyContinue;"
            "powercfg /setacvalueindex SCHEME_CURRENT 19cbb8fa-5279-450e-9fac-8a3d5fedd0c1 12bbebe6-58d6-4636-95bb-3217ef867c1a 0;"
            "powercfg /setdcvalueindex SCHEME_CURRENT 19cbb8fa-5279-450e-9fac-8a3d5fedd0c1 12bbebe6-58d6-4636-95bb-3217ef867c1a 0;"
            "powercfg /setactive SCHEME_CURRENT;"
            "powercfg /change standby-timeout-ac 0; powercfg /change hibernate-timeout-ac 0;"
            "Get-NetAdapterPowerManagement -Name 'Wi-Fi','Ethernet' | ForEach-Object { $_.Name + ' turn-off=' + $_.AllowComputerToTurnOffDevice }")
        state["wifi_performance"] = True
        _net_save(state)

    elif action in ("cloudflared_log", "cloudflared_http2"):
        image = _win_ps(f"(Get-ItemProperty '{_CF_KEY}').ImagePath").strip().strip('"')
        out["image_path_before"] = image
        if "cloudflared_imagepath" not in state:
            state["cloudflared_imagepath"] = image
            _net_save(state)
        log = "C:\\ProgramData\\cloudflared\\cloudflared.log"
        new = image
        if "--logfile" not in new:
            new += f' --logfile "{log}" --loglevel info'
        if action == "cloudflared_http2" and "--protocol" not in new:
            new += " --protocol http2"
        if new == image:
            out["changed"] = "nothing to change; already set"
        else:
            _win_ps("New-Item -ItemType Directory -Force 'C:\\ProgramData\\cloudflared' | Out-Null")
            out["changed"] = _win_ps(f"Set-ItemProperty -Path '{_CF_KEY}' -Name ImagePath -Value '{new}';"
                                     "Restart-Service Cloudflared -Force; Start-Sleep -Seconds 10; (Get-Service Cloudflared).Status")
        # a service restart needs longer than a route change before the tunnel answers again
        _time.sleep(4)

    else:
        raise HTTPException(400, f"unknown action {action!r}")

    after = _site_ok(5)
    out["after"] = after
    if after["fails"] and action in ("prefer_ethernet", "cloudflared_log", "cloudflared_http2"):
        out["reverted"] = _revert(state)
        out["after_revert"] = _site_ok(3)
    return out




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
                                   kind=clean(e.get("kind"), 10) or None,
                                   backup_ok=e.get("backup_ok") if isinstance(e.get("backup_ok"), bool) else None,
                                   online=e.get("online") if isinstance(e.get("online"), bool) else None,
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
    if SERVER != "render":
        asyncio.create_task(_recheck_loop())
    if DATA_FROM:
        asyncio.create_task(_data_sync_loop())


class SearchIn(BaseModel):
    header: str
    name: str = ""           # who searched (the name given at sign-in), for the owner's search log


class SaveIn(BaseModel):
    header: str
    protocol: str = ""
    t1link: str = ""
    t2link: str = ""
    comp_site: str = ""
    ch_id: str = ""         # a hokej.cz option picked: its game on ceskyhokej.cz ...
    comp_id: str = ""       # ... and competition, so the pick teaches the hokej.cz numbering


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


# ---------------------------------------------------------------- the owner's search log
# Every finished search adds a row to the owner's Google Sheet through its Apps Script web app:
# time (IST), name, header, found Yes/No, report link, server. The address and its key are in
# data/sheet_log.json ({"url": ..., "key": ...}), uploaded like the knowledge base and never in the
# public repository. Sent in the background; nothing changes for the person searching.
# ---------------------------------------------------------------- re-checking not-found searches
# Reports often go online hours after a game. Every search that ended without a report is kept in
# data/recheck.json and searched again later in the day while the server is quiet; a report found
# then is stored (the game list and the remembered answers), so the next analyst gets it at once.
RECHECK = ROOT / "data" / "recheck.json"
_RECHECK_LOCK = None


def _recheck_note(header: str, result: dict):
    import json as _j
    a = (result or {}).get("auto") or {}
    if a.get("verdict") == "found" or SERVER == "render":
        return
    try:
        items = _j.loads(RECHECK.read_text())
    except Exception:
        items = {}
    now = _time.time()
    e = items.get(header) or dict(first=now, tries=0, last=0)
    items[header] = e
    items = {h: v for h, v in items.items() if now - v["first"] < 3 * 86400}
    try:
        RECHECK.write_text(_j.dumps(items))
    except Exception:
        pass


async def _recheck_loop():
    """Every 20 minutes, while no search runs, search again up to 5 headers that ended without a
    report at least 30 minutes ago (each at most once an hour, for 3 days)."""
    import asyncio, json as _j
    await asyncio.sleep(300)
    while True:
        try:
            items = _j.loads(RECHECK.read_text()) if RECHECK.exists() else {}
            now = _time.time()
            due = [h for h, v in items.items() if now - v["first"] > 1800 and now - v.get("last", 0) > 3600]
            done = 0
            for h in sorted(due, key=lambda h: items[h].get("last", 0))[:5]:
                if app_busy():
                    break
                items[h]["last"] = _time.time()
                items[h]["tries"] = items[h].get("tries", 0) + 1
                try:
                    r = await _WORKERS.run(lambda h=h: _search_with_retry(h, False))
                    if ((r.get("auto") or {}).get("verdict")) == "found":
                        items.pop(h, None)
                        done += 1
                        print(f"[recheck] found later: {h[:90]}", flush=True)
                except Exception:
                    pass
                await asyncio.sleep(20)
            items = {h: v for h, v in items.items() if _time.time() - v["first"] < 3 * 86400}
            RECHECK.write_text(_j.dumps(items))
            if due:
                print(f"[recheck] {len(due)} waiting, {done} found now, {len(items)} kept", flush=True)
        except Exception as e:
            print(f"[recheck] error: {str(e)[:120]}", flush=True)
        await asyncio.sleep(1200)


async def _sheet_log(name: str, header: str, result: dict, seconds: float = None):
    import json as _j, datetime as _dt
    try:
        cfg = _j.loads((ROOT / "data" / "sheet_log.json").read_text())
        if not cfg.get("url"):
            return
        a = (result or {}).get("auto") or {}
        found = a.get("verdict") == "found"
        try:
            from zoneinfo import ZoneInfo
            now = _dt.datetime.now(ZoneInfo("Asia/Kolkata"))
        except Exception:
            now = _dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)
        link = ((a.get("best") or {}).get("url") or "") if found else ""
        p = lookup.parse_header(header) or {}
        verdict = a.get("verdict")
        row = dict(key=cfg.get("key", ""), time=now.strftime("%Y-%m-%d %H:%M:%S"), name=_clean_name(name) or "-",
                   header=header[:500], found="Yes" if found else "No", link=link, server=SERVER,
                   seconds=seconds,
                   result="Found" if found else ("Shortlist" if verdict == "check" and a.get("candidates") else "Nothing"),
                   found_on=lookup.domain(link) if link else "",
                   competition=(p.get("comp") or "")[:120],
                   match_id=p.get("match_id") or "")
        import httpx
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            await c.post(cfg["url"], content=_j.dumps(row), headers={"content-type": "text/plain"})
    except Exception as e:
        print(f"[sheet log] not sent: {str(e)[:120]}", flush=True)


def _job_start(key: str, factory, on_done=None, who: str = "", header: str = "") -> dict:
    now = _time.time()
    for jid in [j for j, v in _JOBS.items() if v["status"] != "running" and now - v["started"] > 600]:
        _JOB_KEYS.pop(_JOBS[jid]["key"], None)
        _JOBS.pop(jid, None)
    jid = _JOB_KEYS.get(key)
    if jid in _JOBS and (_JOBS[jid]["status"] == "running" or now - _JOBS[jid]["started"] < 20):
        return dict(job=jid)
    jid = _secrets.token_urlsafe(9)
    _JOBS[jid] = dict(status="running", key=key, started=now, who=who, header=header)
    _JOB_KEYS[key] = jid

    async def run():
        global _JOB_SEM
        _JOB_SEM = _JOB_SEM or _aio.Semaphore(_max_searches())
        j = _JOBS[jid]
        try:
            async with _JOB_SEM:
                j["result"] = await _WORKERS.run(factory)
            j["status"] = "done"
            if on_done:
                _aio.create_task(on_done(j["result"], round(_time.time() - j["started"], 1)))
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
async def search_start(inp: SearchIn, request: Request, rosters: bool = False):
    if not lookup.parse_header(inp.header):
        raise HTTPException(400, "Could not read this header. Use: Team 1 vs Team 2 | Competition | YYYY-MM-DD")
    h = inp.header.strip()
    u = _user(request) if PASSWORD else None
    who = u["name"] if u else inp.name          # the search log names the signed-in account
    return _job_start(f"search|{rosters}|{h}", lambda: _search_with_retry(h, rosters),
                      on_done=lambda res, secs: _after_search(who, h, res, secs), who=who, header=h)


_RECENT: list = []      # the last searches finished here, newest first (Admin > Live activity)


async def _after_search(who: str, h: str, res: dict, secs: float):
    a = (res or {}).get("auto") or {}
    _RECENT.insert(0, dict(at=_time.time(), who=_clean_name(who) or "-", header=h[:300], secs=secs, server=SERVER,
                           result="Found" if a.get("verdict") == "found" else "Pick one" if a.get("verdict") == "check" and a.get("candidates") else "Not found",
                           link=((a.get("best") or {}).get("url") or "") if a.get("verdict") == "found" else ""))
    del _RECENT[40:]
    _recheck_note(h, res)
    await _sheet_log(who, h, res, secs)


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
    if inp.ch_id and inp.comp_id:
        from . import hokejcz
        hokejcz.learn_pick(KB, inp.protocol.strip(), inp.ch_id, inp.comp_id)
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

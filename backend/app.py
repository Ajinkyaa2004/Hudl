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


async def _daily_harvest():
    """Refresh the game index from the bulk sources once a day while the server runs."""
    import asyncio, json, time
    from . import harvest
    status = ROOT / "data" / "harvest_status.json"
    while True:
        try:
            last = json.loads(status.read_text()).get("at") if status.exists() else None
            age = time.time() - time.mktime(time.strptime(last, "%Y-%m-%dT%H:%M:%S")) if last else 1e9
            if age > 20 * 3600:
                await harvest.run(log=lambda m: print("[harvest]", m, flush=True))
                from . import health
                await health.run()
        except Exception as e:
            print("[harvest] failed:", e, flush=True)
        await asyncio.sleep(3600)


@app.on_event("startup")
async def _start_harvest():
    import asyncio
    asyncio.create_task(_daily_harvest())


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
    return FileResponse(FRONT / "index.html")


@app.get("/api/config")
def config():
    return dict(teams=len(KB["teams"]), tournaments=len(KB["tournaments"]), saved=len(KB.get("history", [])),
                vpn_sites=lookup.load_vpn(), upload_sessions=lookup.UPLOAD_SESSIONS)


@app.post("/api/search")
async def search(inp: SearchIn, auto: bool = True, rosters: bool = True):
    r = await lookup.search_full(inp.header, KB, rosters=rosters) if auto else lookup.search(inp.header, KB)
    if not r["ok"]:
        raise HTTPException(400, r["error"])
    return r


@app.post("/api/rosters")
async def rosters(inp: SearchIn):
    """Roster check for both teams, loaded separately so the report shows first."""
    from .rosters import check_rosters
    r = lookup.search(inp.header, KB)
    if not r["ok"]:
        raise HTTPException(400, r["error"])
    return await check_rosters(r["parsed"], r["teams"])


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


@app.post("/api/health/run")
async def health_run():
    from . import health
    return await health.run()


@app.get("/api/learning")
def learning_stats():
    """How much the tool knows: games in the index by source, remembered results, learned names."""
    from . import gameindex, learning
    import json as _j
    try:
        harvest = _j.loads((ROOT / "data" / "harvest_status.json").read_text())
    except Exception:
        harvest = None
    return dict(index=gameindex.stats(), learning=learning.stats(), harvest=harvest)


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


@app.get("/api/shot")
async def shot(url: str):
    """Screenshot of a page for the preview panel (cached)."""
    from .fetch import screenshot
    d = lookup.domain(url)
    if not url.startswith("http") or any(d.endswith(b) for b in NO_PREVIEW):
        raise HTTPException(404, "This site blocks previews, open it in Chrome")
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

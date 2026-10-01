"""What the tool learns as it is used, kept in data/games.db next to the game index.

- results:  every header the tool resolved or you saved -> the report link. The same header
            again is answered at once, and a saved (confirmed) answer always wins.
- sources:  team or competition -> report site that served it, so that source is tried
            first next time even if nothing else points to it.
- aliases:  when you save a link for a game the index knows under different team names
            ("PTL Black 14U" for "Princeton Tiger Lilies 14U"), both spellings are linked so
            the next game of that team matches automatically.
"""
from __future__ import annotations
import json, time
from pathlib import Path
from . import gameindex
from .lookup import domain

ALIAS_FILE = Path(__file__).resolve().parent.parent / "data" / "aliases_learned.json"


def _db():
    c = gameindex.conn()
    c.execute("""create table if not exists results(key text primary key, header text, url text, source text, confirmed int,
                 t1 text, t2 text, date text, at real)""")
    c.execute("create table if not exists sources(key text, site text, n int, primary key(key, site))")
    return c


def result_key(parsed: dict) -> str:
    return parsed.get("match_id") or f"{parsed['t1n']}|{parsed['t2n']}|{parsed['date']}"


def remembered(parsed: dict) -> dict | None:
    with gameindex._lock:
        r = _db().execute("select url, source, confirmed, at from results where key=?", (result_key(parsed),)).fetchone()
    return dict(url=r[0], source=r[1], confirmed=bool(r[2]), at=r[3]) if r and r[0] else None


def remember(parsed: dict, header: str, url: str, source: str, confirmed: bool) -> None:
    if not url:
        return
    key = result_key(parsed)
    with gameindex._lock:
        c = _db()
        old = c.execute("select confirmed from results where key=?", (key,)).fetchone()
        if old and old[0] and not confirmed:
            return                       # never overwrite what the analyst confirmed
        c.execute("insert or replace into results values (?,?,?,?,?,?,?,?,?)",
                  (key, header, url, source, int(confirmed), parsed["t1"], parsed["t2"], parsed["date"], time.time()))
        d = domain(url)
        for k in (parsed["t1n"], parsed["t2n"], "comp:" + parsed["compn"]):
            c.execute("insert into sources values (?,?,1) on conflict(key, site) do update set n = n + 1", (k, d))
        c.commit()


def learned_sites(parsed: dict) -> set:
    with gameindex._lock:
        rows = _db().execute("select site from sources where key in (?,?,?)",
                             (parsed["t1n"], parsed["t2n"], "comp:" + parsed["compn"])).fetchall()
    return {r[0] for r in rows}


def learn_aliases(parsed: dict, url: str) -> list[str]:
    """If the saved report is a game in the index, link the header's team names to the site's."""
    from .adapters.base import team_similarity, aliases
    with gameindex._lock:
        g = gameindex.conn().execute("select home, away from games where url=?", (url,)).fetchone()
    if not g:
        return []
    home, away = g
    pairs = [(parsed["t1"], home), (parsed["t2"], away)]
    if team_similarity(parsed["t1"], away)[0] + team_similarity(parsed["t2"], home)[0] > \
       team_similarity(parsed["t1"], home)[0] + team_similarity(parsed["t2"], away)[0]:
        pairs = [(parsed["t1"], away), (parsed["t2"], home)]
    try:
        data = json.loads(ALIAS_FILE.read_text())
    except Exception:
        data = {}
    added = []
    for header_name, site_name in pairs:
        if team_similarity(header_name, site_name)[0] >= 0.8:
            continue
        lst = data.setdefault(site_name, [])
        if header_name not in lst:
            lst.append(header_name)
            added.append(f"{site_name} = {header_name}")
    if added:
        ALIAS_FILE.write_text(json.dumps(data, indent=1, ensure_ascii=False))
        import backend.adapters.base as b
        b._ALIASES = None                 # reload with the new names
        team_similarity.cache_clear()
    return added


def stats() -> dict:
    with gameindex._lock:
        c = _db()
        res = c.execute("select count(*), sum(confirmed) from results").fetchone()
        src = c.execute("select count(*) from sources").fetchone()[0]
    try:
        al = sum(len(v) for v in json.loads(ALIAS_FILE.read_text()).values())
    except Exception:
        al = 0
    return dict(results=res[0] or 0, confirmed=res[1] or 0, source_links=src, aliases=al)

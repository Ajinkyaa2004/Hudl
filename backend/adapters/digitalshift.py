"""HockeyShift / DigitalShift stats sites: Upper Midwest High School Elite League
(hselitehockey.com), Midwest Girls HS Elite League (girlshselitehockey.com), Dallas Stars
Tournaments. Public web API web.api.digitalshift.ca: POST /login {client_service_id} gives a
ticket; partials/stats/schedule/table returns the season (or tournament) schedule, 'all=true'
from the first game, paged with start_id/offset/limit. Report: {site}/stats#/{league}/game/{id}.
Leagues: data/digitalshift_leagues.json."""
from __future__ import annotations
import asyncio, datetime, html, json, re, time
from pathlib import Path
import httpx
from .base import Adapter
from .na_util import local_date, swap_words, score_local, age_level

DATA = Path(__file__).resolve().parent.parent.parent / "data" / "digitalshift_leagues.json"
API = "https://web.api.digitalshift.ca"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0.0.0 Safari/537.36")
_tickets: dict = {}          # client_service_id -> (time, ticket)
_cache: dict = {}            # (url, params) -> (time, json)


def _cfg() -> list[dict]:
    try:
        return json.loads(DATA.read_text())["leagues"]
    except Exception:
        return []


async def _ticket(client: httpx.AsyncClient, cid: str) -> str | None:
    hit = _tickets.get(cid)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    r = await client.post(f"{API}/login", json={"client_service_id": cid})    # no Referer: the WAF refuses it here
    if r.status_code != 200:
        return None
    t = r.json().get("ticket", {}).get("hash")
    _tickets[cid] = (time.time(), t)
    return t


async def _get(client, lg, path, params, ttl=1800):
    key = (lg["site"], path, tuple(sorted(params.items())))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    t = await _ticket(client, lg["client_service_id"])
    if not t:
        return None
    r = await client.get(f"{API}/{path}", params=params, headers={
        "Authorization": f'ticket="{t}"', "Accept": "application/json", "Origin": lg["site"],
        "Referer": lg["site"] + "/", "User-Agent": UA})
    if r.status_code != 200:
        return None
    data = r.json()
    _cache[key] = (time.time(), data)
    return data


def _items(data) -> list[dict]:
    """The schedule list from either the first page (HTML with ng-init JSON) or a next page."""
    if not data:
        return []
    if isinstance(data.get("schedule"), list):
        return data["schedule"]
    m = re.search(r'ctrl\.schedule=(\[.*?\])"', data.get("content") or "", re.S)
    try:
        return json.loads(html.unescape(m.group(1))) if m else []
    except Exception:
        return []


async def schedule(client, lg, scope: dict, until: str) -> list[dict]:
    """Every game of a season or tournament from its first game up to date `until`."""
    params = dict(scope, all="true", order="datetime")
    items = _items(await _get(client, lg, "partials/stats/schedule/table", params))
    out = list(items)
    for _ in range(30):
        if not items or (items[-1].get("date") or "") > until:
            break
        items = _items(await _get(client, lg, "partials/stats/schedule/table",
                                  dict(params, start_id=items[-1]["id"], offset=1, limit=100)))
        out += items
    return out


class DigitalShift(Adapter):
    name = "digitalshift"
    site = "hockeyshift.com"
    budget_s = 40

    def _leagues(self, parsed, tm):
        c = parsed["comp"].lower()
        hits = []
        for lg in _cfg():
            host = lg["site"].split("//", 1)[-1].replace("www.", "")
            linked = any(host in ((tm[k]["best"] or {}).get(f) or "") for k in ("t1n", "t2n") for f in ("site", "schedule", "roster"))
            if linked or any(w in c for w in lg.get("comp", [])):
                hits.append(lg)
        return hits

    def applies(self, parsed, tm, kb):
        return bool(self._leagues(parsed, tm))

    def preferred_date(self, parsed):
        return local_date(parsed, "America/Chicago")

    async def _league_games(self, client, lg, parsed):
        until = (datetime.date.fromisoformat(parsed["date"]) + datetime.timedelta(days=1)).isoformat()
        f = await _get(client, lg, "partials/stats/filters", {"type": "league", "id": lg["league"]}, ttl=6 * 3600) or {}
        season = (f.get("season") or {}).get("selected_id")
        tours = [o["id"] for o in ((f.get("tournament") or {}).get("options") or []) if isinstance(o, dict)]
        scopes = [{"tournament_id": t} for t in tours] or ([{"league_id": lg["league"], "season_id": season}] if season else [])
        out = []
        for sc in scopes:
            out += [(lg, g) for g in await schedule(client, lg, sc, until)]
        return out

    async def find(self, parsed, tm, kb):
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            res = await asyncio.gather(*[self._league_games(client, lg, parsed) for lg in self._leagues(parsed, tm)], return_exceptions=True)
        d = datetime.date.fromisoformat(parsed["date"])
        out = []
        for r in res:
            if isinstance(r, Exception):
                continue
            for lg, g in r:
                if g.get("type") not in (None, "game") or not g.get("game_id") or not g.get("date"):
                    continue
                if abs((datetime.date.fromisoformat(g["date"]) - d).days) > 1:
                    continue
                names = lg.get("names") or {}
                strip = re.compile(lg["strip"]) if lg.get("strip") else None
                fix = lambda n: age_level(swap_words(strip.sub("", n) if strip else n, names))
                league = f"{lg['name']} {g.get('home_division') or ''} {lg.get('context') or ''}".strip()
                final = str(g.get("status") or "").lower().startswith("final")
                tz = lg.get("tz") or "America/Chicago"
                cand = dict(source=self.name, site=lg["site"].split("//", 1)[-1], league=league,
                            home=fix(g.get("home_team") or ""), away=fix(g.get("away_team") or ""),
                            date=g["date"], score=f"{g.get('home_score')}-{g.get('away_score')}" if final else None,
                            status=g.get("status"), kind="protocol", adapter=self.name,
                            url=f"{lg['site']}/stats#/{lg['league']}/game/{g['game_id']}",
                            start_utc=(g.get("datetime_tz") or "")[:16] or None)
                score_local(cand, parsed, league, tz)
                if min(cand["team_scores"]) >= 0.6:
                    out.append(cand)
        return out

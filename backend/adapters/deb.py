"""deb-online.live (Germany): schedules per division from the hockeydata.net API.
Divisions with their team lists are harvested into data/deb_divisions.json."""
from __future__ import annotations
import json, asyncio, difflib
from pathlib import Path
from ..fetch import get_json
from ..lookup import tokens
from .base import Adapter, score_candidate, days_apart

DATA = Path(__file__).resolve().parent.parent.parent / "data" / "deb_divisions.json"
API = "https://api.hockeydata.net/data/ebel/Schedule?apiKey={key}&lang=en&referer=deb-online.live&divisionId={div}&widgetOptions=%7B%22gameStatus%22%3A%5B0%2C1%2C2%2C3%2C4%5D%2C%22semantic%22%3Atrue%7D"
REPORT = "https://deb-online.live/spielbericht/?gameId={gid}&divisionId={div}"


def load():
    try:
        return json.loads(DATA.read_text())
    except Exception:
        return dict(apiKey="", divisions={})


async def remember_division(did: str):
    """Fetch a division's schedule once and add it to the file (learning from a saved link)."""
    f = load()
    if did in f["divisions"] or not f.get("apiKey"):
        return
    data = await get_json(API.format(key=f["apiKey"], div=did), ttl=6 * 3600, timeout=40)
    rows = ((data or {}).get("data") or {}).get("rows", [])
    if not rows:
        return
    dates = sorted(x.get("scheduledGameStart", "")[:10] for x in rows if x.get("scheduledGameStart"))
    teams = sorted({x.get("homeTeamLongName") for x in rows if x.get("homeTeamLongName")} | {x.get("awayTeamLongName") for x in rows if x.get("awayTeamLongName")})
    f["divisions"][did] = dict(league=rows[0].get("leagueName"), division=rows[0].get("divisionName"), season=rows[0].get("currentSeason"),
                               first=dates[0] if dates else None, last=dates[-1] if dates else None, games=len(rows), teams=teams)
    DATA.write_text(json.dumps(f, ensure_ascii=False, indent=1))


def _team_in(name: str, team_list: list) -> bool:
    from .base import team_similarity
    if any(team_similarity(name, t)[0] >= 0.8 for t in team_list):
        return True
    base, _, _ = tokens(name)
    for t in team_list:
        tb, _, _ = tokens(t)
        if base and tb and (base in tb or tb in base or difflib.SequenceMatcher(None, base, tb).ratio() >= 0.8):
            return True
    return False


class Deb(Adapter):
    name = "deb-online"
    site = "deb-online.live"
    ambiguous_dates = True   # 5 of 43 back-to-back pairs were on the other day; the header time decides instead

    def _divisions(self, parsed, tm):
        d = load()
        date = parsed["date"]
        picked = []
        for did, v in d["divisions"].items():
            if v.get("first") and v.get("last") and not (v["first"] <= date <= v["last"] or days_apart(date, v["first"]) in (0, 1) or days_apart(date, v["last"]) in (0, 1)):
                continue
            if _team_in(parsed["t1"], v.get("teams", [])) or _team_in(parsed["t2"], v.get("teams", [])):
                picked.append(did)
        return d["apiKey"], picked

    def applies(self, parsed, tm, kb):
        c = parsed["comp"].lower()
        if any(k in c for k in ("del", "dnl", "oberliga", "regionalliga", "german", "deutsch", "bayern", "landesliga")):
            return True
        for key in ("t1n", "t2n"):
            b = tm[key]["best"] or (tm[key]["candidates"][0] if tm[key]["candidates"] else None)
            if b and (str(b.get("country") or "").lower() == "germany" or "deb-online" in (b.get("site") or "") + (b.get("schedule") or "")):
                return True
        _, picked = self._divisions(parsed, tm)
        return bool(picked)

    async def find(self, parsed, tm, kb):
        out = []
        key, picked = self._divisions(parsed, tm)
        if not key or not picked:
            return out
        results = await asyncio.gather(*[get_json(API.format(key=key, div=did), ttl=6 * 3600, timeout=40) for did in picked[:12]], return_exceptions=True)
        for did, data in zip(picked, results):
            if isinstance(data, Exception) or not data:
                continue
            for r in (data.get("data") or {}).get("rows", []):
                date = (r.get("scheduledGameStart") or "")[:10]
                if days_apart(parsed["date"], date) not in (0, 1):
                    continue
                league = f"{r.get('leagueName') or ''} {r.get('divisionName') or ''}".strip()
                played = r.get("gameHasEnded") or r.get("gameStatus") in (2, 3)
                cand = dict(source=self.name, site=self.site, league=league, home=r.get("homeTeamLongName", ""), away=r.get("awayTeamLongName", ""),
                            date=date, score=f"{r.get('homeTeamScore')}-{r.get('awayTeamScore')}" if played else None, status=None,
                            kind="protocol", adapter=self.name, url=REPORT.format(gid=r["id"], div=did),
                            start_utc=(__import__("datetime").datetime.utcfromtimestamp(r["gameUtcTimestamp"] / 1000).strftime("%Y-%m-%dT%H:%M") if r.get("gameUtcTimestamp") else None))
                score_candidate(cand, parsed, league)
                if min(cand["team_scores"]) >= 0.6:
                    out.append(cand)
        return out

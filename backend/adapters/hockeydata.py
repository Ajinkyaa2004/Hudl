"""hockeydata.net leagues: the Alps Hockey League (alps.hockey) and the Austrian federation's
competitions on eishockey.at (U13..U20, OEHV U18i, women's leagues, Eliteliga).
Both sites load hockeydata's public widget API (the key is in alps.hockey's own widget
script). A season's division id is read from eishockey.at's game-center page per league
(links carry ?saison=<divisionId>)."""
from __future__ import annotations
import asyncio, datetime, re
from collections import Counter
from ..fetch import get_text, get_json
from .base import Adapter, score_candidate, days_apart

KEY_JS = "https://www.alps.hockey/templates/g5_hydrogen/custom/js/_los_customer_ahl.js"
KEY_FALLBACK = "175fe3ea6bf375c7c4cba4f747c33d84"
API = "https://api.hockeydata.net/data/ebel/Schedule?apiKey={key}&lang=en&referer=www.alps.hockey&divisionId={div}"
GC = "https://www.eishockey.at/game-center/{league}"
EAT_GAME = "https://www.eishockey.at/game-center/{league}/spiel/{gid}?saison={div}"
ALPS_GAME = "https://www.alps.hockey/en/pages/game/?gameId={gid}&divisionId={div}"

LEAGUE_NAMES = {"alps": "Alps Hockey League", "oeel": "OEHV Eliteliga", "debl": "DEBL (women)", "debl2": "DEBL2 (women)",
                "ewhl": "EWHL (women)", "awhl": "AWHL (women)", "u20": "OEHV U20", "u18": "OEHV U18i", "u17": "OEHV U17",
                "u16": "OEHV U16", "u15": "OEHV U15", "u14": "OEHV U14", "u13": "OEHV U13"}

# site name -> names the headers use
NAMES = {
    "Adler Stadtwerke Kitzbühel": "EC Die Adler Stadtwerke Kitzbuhel",
    "Rittner Buam SkyAlps": "Ritten/Renon Sport Hockey Rittner Buam",
    "HC Meran/o Pircher": "HC Merano Meran",
    "HC Gherdeina valgardena.it": "HC Gherdeina",
    "Hockey Unterland Cavaliers": "Unterland Cavaliers",
    "Red Bull Hockey Juniors": "EC Red Bull Salzburg II Juniors",
    "KHL Sisak": "KHL Sisak",
    "HDD SIJ Acroni Jesenice": "HD Hidria HDD Jesenice",
    "Migross Supermercati Asiago Hockey": "Asiago",
    "EK Die Zeller Eisbären": "EKZ Zeller Eisbaren",
    "EC iDM VSV": "EC Villacher SV VSV",
    "EC iDM Wärmepumpen VSV": "EC Villacher SV VSV",
    "HC TIWAG Innsbruck - Die Haie": "HC TWK TIWAG Innsbruck Die Haie",
    "EC KAC": "EC-KAC",
    "EV Vienna Capitals": "Vienna Capitals",
}


def _name(site_name: str, league: str) -> str:
    """Header-style name; youth leagues get their age group (the site omits it)."""
    n = NAMES.get(site_name, site_name)
    if re.fullmatch(r"u\d{2}", league) and not re.search(r"\bU\d{2}\b", n):
        n += " " + league.upper()
    return n


def _country(tm, key):
    b = tm[key]["best"] or (tm[key]["candidates"][0] if tm[key]["candidates"] else None)
    return str((b or {}).get("country") or "").lower()


def _ages(parsed):
    from ..lookup import tokens
    return tokens(parsed["t1"])[1] | tokens(parsed["t2"])[1] | tokens(parsed["comp"])[1] | set(re.findall(r"\bu(\d{2})", parsed["comp"].lower()))


class HockeyData(Adapter):
    name = "hockeydata"
    site = "alps.hockey / eishockey.at"

    def _leagues(self, parsed, tm):
        from ..lookup import is_friendly
        c = parsed["comp"].lower()
        ages = {str(a).lstrip("u") for a in _ages(parsed)}
        austrian = "oehv" in c or "öehv" in c or "austria" in c or "osterreich" in c or "eliteliga" in c
        countries = {_country(tm, "t1n"), _country(tm, "t2n")}
        if "alps" in c or "ahl" == c.strip():
            return ["alps"]
        if ages:
            if austrian or "austria" in countries:
                return [f"u{a}" for a in sorted(ages) if f"u{a}" in LEAGUE_NAMES]
            return []
        if austrian:
            return ["oeel"] + (["debl", "debl2", "ewhl", "awhl"] if "women" in c or "dam" in c else [])
        if any(w in c for w in ("debl", "ewhl", "awhl")):
            return [w for w in ("debl2", "debl", "ewhl", "awhl") if w in c][:1]
        if is_friendly(parsed["comp"]) and countries & {"austria", "italy", "slovenia", "croatia"}:
            return ["alps"]
        return []

    def applies(self, parsed, tm, kb):
        return bool(self._leagues(parsed, tm))

    async def _key(self):
        js = await get_text(KEY_JS, ttl=7 * 24 * 3600)
        m = re.search(r'"apiKey"\s*:\s*"([0-9a-f]{32})"', js or "")
        return m.group(1) if m else KEY_FALLBACK

    async def _division(self, league):
        page = await get_text(GC.format(league=league), ttl=24 * 3600)
        ids = Counter(re.findall(r"saison=(\d{4,6})\b", page or ""))
        ids.pop("2026", None)
        return ids.most_common(1)[0][0] if ids else None

    async def find(self, parsed, tm, kb):
        leagues = self._leagues(parsed, tm)
        key = await self._key()
        divs = await asyncio.gather(*[self._division(l) for l in leagues])
        pairs = [(l, d) for l, d in zip(leagues, divs) if d]
        datas = await asyncio.gather(*[get_json(API.format(key=key, div=d), ttl=1800, timeout=30) for _, d in pairs])
        out = []
        for (league, div), data in zip(pairs, datas):
            for g in ((data or {}).get("data") or {}).get("rows", []):
                v = (g.get("scheduledDate") or {}).get("value") or ""
                if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", v):
                    continue
                date = f"{v[6:]}-{v[3:5]}-{v[:2]}"
                if days_apart(parsed["date"], date) not in (0, 1):
                    continue
                ts = g.get("gameUtcTimestamp")
                start = datetime.datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%dT%H:%M") if ts else None
                home, away = g.get("homeTeamLongName") or "", g.get("awayTeamLongName") or ""
                score = f"{g.get('homeTeamScore')}-{g.get('awayTeamScore')}" if g.get("gameHasEnded") else None
                url = (ALPS_GAME if league == "alps" else EAT_GAME).format(league=league, gid=g["id"], div=div)
                lname = LEAGUE_NAMES.get(league, league)
                cand = dict(source=self.name, site=url.split("/")[2], league=f"{lname} {g.get('divisionName') or ''}".strip(),
                            home=_name(home, league), away=_name(away, league), date=date, score=score, status=None,
                            kind="protocol", adapter=self.name, url=url, start_utc=start)
                score_candidate(cand, parsed, lname if league.startswith("u") else "")
                if min(cand["team_scores"]) >= 0.6:
                    out.append(cand)
        return out

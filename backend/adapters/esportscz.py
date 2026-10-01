"""eSports.cz league platform (hokejovyzapis.cz): the ICE Hockey League (ice.hockey) and
Denmark's Metal Ligaen (metalligaen.dk) publish their whole season as JSON on S3, one file
per league part (1 = regular season, 2 = pre-season, 3+ = play-offs when they exist).
start_date is the local game time."""
from __future__ import annotations
import asyncio, datetime
from ..fetch import get_json
from .base import Adapter, score_candidate, days_apart, local_to_utc

FEED = "https://s3-eu-west-1.amazonaws.com/{tenant}.hokejovyzapis.cz/league-matches/{season}/{part}.json"

TENANTS = {
    "icehl": dict(league="ICE Hockey League", tz="Europe/Vienna",
                  report="https://www.ice.hockey/en/stats/gamestats?gameId={id}",
                  comp_words=("ice hockey league", "icehl", "ebel", "win2day"),
                  countries={"austria", "hungary", "slovenia", "italy"}),
    "den": dict(league="Metal Ligaen", tz="Europe/Copenhagen",
                report="https://metalligaen.dk/kampe/detalje?gameId={id}",
                comp_words=("metal liga", "metalliga", "denmark1", "denmark 1", "danish"),
                countries={"denmark"}),
}
PARTS = (1, 2, 3)

# site name -> the name the headers use, when the site's name alone would not match
NAMES = {
    "Sønderjyske Ishockey": "SonderjyskE Ishockey",
    "EC iDM Wärmepumpen VSV": "EC Villacher SV VSV",
    "HC Falkensteiner Pustertal": "HC Pustertal Falkensteiner",
    "HCB Südtirol Alperia": "HC Bolzano-Bozen Foxes Sudtirol Alperia",
    "Hydro Fehérvár AV19": "Fehervar AV19",
    "Moser Medical Graz99ers": "EC Moser Medical Graz 99ers",
    "Steinbach Black Wings Linz": "Black Wings Linz Steinbach",
    "HC TIWAG Innsbruck - Die Haie": "HC Innsbruck TIWAG Die Haie",
    "Olimpija Ljubljana": "HK Olimpija Ljubljana",
    "University of Saskatchewan": "Saskatchewan Huskies University",
}


def season_of(date: str) -> str:
    return date[:4] if date[5:7] >= "07" else str(int(date[:4]) - 1)


def _country(tm, key):
    b = tm[key]["best"] or (tm[key]["candidates"][0] if tm[key]["candidates"] else None)
    return str((b or {}).get("country") or "").lower()


class EsportsCz(Adapter):
    name = "esportscz"
    site = "ice.hockey / metalligaen.dk"

    def _tenants(self, parsed, tm):
        c = parsed["comp"].lower()
        friendly = lookup_friendly(parsed["comp"])
        countries = {_country(tm, "t1n"), _country(tm, "t2n")}
        out = []
        for t, cfg in TENANTS.items():
            # pre-season guests come from many countries (Rosenheim, Saskatchewan) and Club
            # Data often lacks the country: every friendly reads both feeds (cached, small)
            if any(w in c for w in cfg["comp_words"]) or friendly or countries & cfg["countries"] and "cup" in c:
                out.append(t)
        return out

    def applies(self, parsed, tm, kb):
        return bool(self._tenants(parsed, tm))

    async def find(self, parsed, tm, kb):
        season = season_of(parsed["date"])
        tenants = self._tenants(parsed, tm)
        jobs = [(t, p) for t in tenants for p in PARTS]
        feeds = await asyncio.gather(*[get_json(FEED.format(tenant=t, season=season, part=p), ttl=1800) for t, p in jobs])
        out = []
        for (t, p), data in zip(jobs, feeds):
            cfg = TENANTS[t]
            for g in (data or {}).get("matches", []):
                sd = (g.get("start_date") or "")
                date, hhmm = sd[:10], sd[11:16]
                if days_apart(parsed["date"], date) not in (0, 1):
                    continue
                home = (g.get("home") or {}).get("name") or ""
                away = (g.get("guest") or {}).get("name") or ""
                fin = ((g.get("results") or {}).get("score") or {}).get("final") or {}
                score = f"{fin.get('score_home')}-{fin.get('score_guest')}" if g.get("status") == "AFTER_MATCH" and fin else None
                league = f"{cfg['league']} {g.get('league_part_name') or ''}".strip() if p == 1 else (g.get("league_part_name") or cfg["league"])
                cand = dict(source=self.name, site=cfg["report"].split("/")[2], league=league,
                            home=NAMES.get(home, home), away=NAMES.get(away, away), date=date, score=score,
                            status=g.get("status"), kind="protocol", adapter=self.name,
                            url=cfg["report"].format(id=g["id"]), start_utc=local_to_utc(date, hhmm, cfg["tz"]))
                score_candidate(cand, parsed, "")
                if min(cand["team_scores"]) >= 0.6:
                    out.append(cand)
        return out


def lookup_friendly(comp: str) -> bool:
    from ..lookup import is_friendly
    c = comp.lower()
    return is_friendly(comp) or "pre-season" in c or "preseason" in c or "test" in c

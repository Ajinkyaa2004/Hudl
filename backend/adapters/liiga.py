"""liiga.fi (Finland, top league): public JSON of every game in a season, including
pre-season friendlies. Game ids are the same as the Finnish federation's."""
from __future__ import annotations
import asyncio, datetime
from ..fetch import get_json
from .base import Adapter, score_candidate, days_apart

API = "https://liiga.fi/api/v2/games?tournament={t}&season={season}"
REPORT = "https://liiga.fi/en/game/{season}/{gid}/events"
TOURNAMENTS = ("valmistavat_ottelut", "runkosarja", "playoffs", "chl")


def season_for(date: str) -> int:
    d = datetime.date.fromisoformat(date)
    return d.year + 1 if d.month >= 5 else d.year


class Liiga(Adapter):
    name = "liiga"
    site = "liiga.fi"

    def applies(self, parsed, tm, kb):
        c = parsed["comp"].lower()
        if "liiga" in c or "finland" in c or "finnish" in c or "friendl" in c or "champions" in c:
            return True
        for key in ("t1n", "t2n"):
            b = tm[key]["best"] or (tm[key]["candidates"][0] if tm[key]["candidates"] else None)
            if b and str(b.get("country") or "").lower() == "finland":
                return True
        return False

    async def find(self, parsed, tm, kb):
        season = season_for(parsed["date"])
        lists = await asyncio.gather(*[get_json(API.format(t=t, season=season), ttl=6 * 3600, timeout=40) for t in TOURNAMENTS], return_exceptions=True)
        out = []
        for t, games in zip(TOURNAMENTS, lists):
            if not isinstance(games, list):
                continue
            for g in games:
                date = (g.get("start") or "")[:10]
                if days_apart(parsed["date"], date) not in (0, 1):
                    continue
                home, away = (g.get("homeTeam") or {}), (g.get("awayTeam") or {})
                played = g.get("ended") or g.get("finishedType")
                cand = dict(source=self.name, site=self.site, league=f"Liiga {t}", home=home.get("teamName", ""), away=away.get("teamName", ""),
                            date=date, score=f"{home.get('goals')}-{away.get('goals')}" if played else None, status=None,
                            kind="protocol", adapter=self.name, url=REPORT.format(season=season, gid=g["id"]),
                            start_utc=(g.get("start") or "")[:16] or None)
                score_candidate(cand, parsed, "Liiga")
                if min(cand["team_scores"]) >= 0.6:
                    out.append(cand)
        return out

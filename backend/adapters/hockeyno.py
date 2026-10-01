"""live.hockey.no (Norway): public by-date JSON of every scheduled match."""
from __future__ import annotations
from ..fetch import get_json
from .base import Adapter, score_candidate, days_apart, local_to_utc
import datetime, re

API = "https://sf34-terminlister-prod-app.azurewebsites.net/ta/ScheduledMatchesBySport/?sportIds=152&date={m}/{d}/{y}"
REPORT = "https://live.hockey.no/match?seasonId={season}&tournamentId={tour}&matchId={mid}&matchDate={date}T00:00:00"


def _clean(name: str) -> str:
    # "Comet Halden Elite - MEN 1" -> "Comet Halden Elite"
    return re.sub(r"\s+-\s+(MEN|WOMEN|U\d+|G\d+|J\d+)\b.*$", "", name or "").strip()


class HockeyNo(Adapter):
    name = "hockey.no"
    site = "live.hockey.no"

    def applies(self, parsed, tm, kb):
        c = parsed["comp"].lower()
        if any(k in c for k in ("norw", "elitehockey", "eliteserien", "1. divisjon", "hockeyliga")):
            return True
        for key in ("t1n", "t2n"):
            b = tm[key]["best"] or (tm[key]["candidates"][0] if tm[key]["candidates"] else None)
            if b and (str(b.get("country") or "").lower() == "norway" or "hockey.no" in (b.get("site") or "") + (b.get("schedule") or "")):
                return True
        return False

    async def find(self, parsed, tm, kb):
        out = []
        d0 = datetime.date.fromisoformat(parsed["date"])
        for delta in (0, -1, 1):
            d = d0 + datetime.timedelta(days=delta)
            data = await get_json(API.format(m=d.month, d=d.day, y=d.year), ttl=1800)
            for m in (data or {}).get("matches", []):
                date = d.isoformat()
                cand = dict(source=self.name, site=self.site, league=m.get("tournamentName", ""), home=_clean(m.get("homeTeam")), away=_clean(m.get("awayTeam")),
                            date=date, score=None, status=None, kind="protocol", adapter=self.name,
                            start_utc=local_to_utc(date, str(m.get("matchStartTime") or "").zfill(4), "Europe/Oslo"),
                            url=REPORT.format(season=m.get("seasonId") or "", tour=m.get("tournamentId"), mid=m.get("matchId"), date=date))
                if "seasonId" not in m:
                    cand["url"] = f"https://live.hockey.no/match?tournamentId={m.get('tournamentId')}&matchId={m.get('matchId')}&matchDate={date}T00:00:00"
                score_candidate(cand, parsed, m.get("tournamentName", ""))
                if min(cand["team_scores"]) >= 0.6:
                    out.append(cand)
        return out

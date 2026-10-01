"""NHL: public schedule API by date (pre-season, regular season, playoffs)."""
from __future__ import annotations
import datetime
from ..fetch import get_json
from .base import Adapter, score_candidate

API = "https://api-web.nhle.com/v1/schedule/{date}"
REPORT = "https://www.nhl.com/gamecenter/{gid}"


def _name(t: dict) -> str:
    place = (t.get("placeName") or {}).get("default", "")
    common = (t.get("commonName") or {}).get("default", "")
    return f"{place} {common}".strip()


class Nhl(Adapter):
    name = "nhl"
    site = "nhl.com"

    def applies(self, parsed, tm, kb):
        c = parsed["comp"].lower()
        return "nhl" in c or "national hockey league" in c or "friendl" in c or "test tag" in c

    def preferred_date(self, parsed):
        d = datetime.date.fromisoformat(parsed["date"])
        t = parsed.get("time")
        return (d - datetime.timedelta(days=1)).isoformat() if (not t or t < "12:00") else parsed["date"]

    async def find(self, parsed, tm, kb):
        data = await get_json(API.format(date=(datetime.date.fromisoformat(parsed["date"]) - datetime.timedelta(days=1)).isoformat()), ttl=1800)
        out = []
        for week in (data or {}).get("gameWeek", []):
            for g in week.get("games", []):
                date = week.get("date")
                cand = dict(source=self.name, site=self.site, league="NHL", home=_name(g.get("homeTeam") or {}), away=_name(g.get("awayTeam") or {}),
                            date=date, score=(f"{g['homeTeam'].get('score')}-{g['awayTeam'].get('score')}" if g.get("gameState") in ("OFF", "FINAL") else None),
                            status=g.get("gameState"), kind="protocol", adapter=self.name, url=REPORT.format(gid=g["id"]),
                            start_utc=(g.get("startTimeUTC") or "")[:16] or None)
                score_candidate(cand, parsed, "NHL")
                if min(cand["team_scores"]) >= 0.6:
                    out.append(cand)
        return out

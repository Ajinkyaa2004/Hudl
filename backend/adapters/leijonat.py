"""tulospalvelu.leijonat.fi (Finland): games-by-date JSON. Teams come as short
club names (Kärpät, TPS, HIFK) and the level name carries the age group."""
from __future__ import annotations
import datetime
from ..fetch import get_json
from .base import Adapter, score_candidate, local_to_utc

API = "https://tulospalvelu.leijonat.fi/helpers/getgames?season={season}&subSerieId=0&teamid=0&districtid=-1&gamedays=-1&dog={date}&levelid={level}"
LEVELS = "https://tulospalvelu.leijonat.fi/helpers/getlevels?season={season}"
REPORT = "https://tulospalvelu.leijonat.fi/game?season={season}&gameid={gid}&lang=en"


def season_for(date: str) -> int:
    d = datetime.date.fromisoformat(date)
    return d.year + 1 if d.month >= 5 else d.year


class Leijonat(Adapter):
    name = "leijonat"
    site = "tulospalvelu.leijonat.fi"

    def applies(self, parsed, tm, kb):
        c = parsed["comp"].lower()
        if any(k in c for k in ("liiga", "mestis", "sm-sarja", "u20 sm", "u18 sm", "u16 sm", "suomi", "finland", "finnish", "auroraliiga")):
            return True
        for key in ("t1n", "t2n"):
            b = tm[key]["best"] or (tm[key]["candidates"][0] if tm[key]["candidates"] else None)
            if b and (str(b.get("country") or "").lower() == "finland" or "leijonat" in (b.get("site") or "") + (b.get("schedule") or "") + (b.get("roster") or "")):
                return True
        return False

    async def find(self, parsed, tm, kb):
        import asyncio
        out = []
        season = season_for(parsed["date"])
        # the default list leaves out friendlies ("Harjoitusottelut"), which have their own level ids
        levels = await get_json(LEVELS.format(season=season), ttl=24 * 3600) or []
        friendly_ids = [str(l["LevelID"]) for l in levels if "arjoitus" in str(l.get("LevelName", ""))]
        # the day before and after too: a header's date is sometimes a day off (the sheet's date is
        # the analysis date), and the time-of-day match then still finds the game
        import datetime
        d0 = datetime.date.fromisoformat(parsed["date"])
        days = [(d0 + datetime.timedelta(days=i)).isoformat() for i in (0, -1, 1)]
        calls = [get_json(API.format(season=season, date=d, level=lv), ttl=1800) for d in days for lv in ["-1"] + friendly_ids]
        data = []
        for res in await asyncio.gather(*calls, return_exceptions=True):
            if isinstance(res, list):
                data += res
        for level in data or []:
            lname = level.get("LevelName", "")
            for g in level.get("Games", []):
                gdate = g.get("GameDateDB") or parsed["date"]
                home = g.get("HomeTeamTempName") or g.get("HomeTeamAbbrv") or ""
                away = g.get("AwayTeamTempName") or g.get("AwayTeamAbbrv") or ""
                score = f"{g.get('HomeGoals')}-{g.get('AwayGoals')}" if g.get("GameStatus") in (2, 3) else None
                cand = dict(source=self.name, site=self.site, league=f"{lname} {g.get('SubSerieName') or ''}".strip(), home=home, away=away,
                            date=gdate, score=score, status=None, kind="protocol", adapter=self.name,
                            url=REPORT.format(season=season, gid=g["GameID"]), note="team names on this site are short forms",
                            start_utc=local_to_utc(gdate, (g.get("GameTime") or "")[:5], "Europe/Helsinki"))
                score_candidate(cand, parsed, lname)
                if min(cand["team_scores"]) >= 0.6:
                    out.append(cand)
        return out

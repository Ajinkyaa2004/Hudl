"""Champions Hockey League (chl.hockey): the schedule page lists one JSON feed per stage
(api/s3?q=schedule-<season>-<stage>.json); each match carries its UTC start and the
/matches/<id>/<slug> page link."""
from __future__ import annotations
import asyncio, re
from ..fetch import get_text, get_json
from .base import Adapter, score_candidate, days_apart

BASE = "https://www.chl.hockey"
SCHEDULE_PAGE = BASE + "/en/schedule"
FEED_RE = re.compile(r"baseFeedsUrl:\s*'(https://www\.chl\.hockey/api/s3\?q=schedule-[0-9a-f]+-[0-9a-f]+\.json)'")

# CHL site name -> names the headers use
NAMES = {
    "HC Pilsen": "HC Skoda Plzen Pilsen",
    "Rögle Ängelholm": "Rogle BK Angelholm",
    "Frölunda Gothenburg": "Frolunda HC Gothenburg",
    "Tappara Tampere": "Tampereen Tappara",
    "KAC Klagenfurt": "EC-KAC Klagenfurt",
    "Red Bull Salzburg": "EC Red Bull Salzburg",
    "Bordeaux Boxers": "Boxers de Bordeaux",
    "Koo Koo Kouvola": "KooKoo Hockey Kouvola",
    "KooKoo Kouvola": "KooKoo Hockey Kouvola",
    "Graz 99ers": "EC Moser Medical Graz 99ers",
    "Storhamar Hamar": "Storhamar Hockey Hamar",
    "Geneve-Servette": "Geneve-Servette HC",
    "Genève-Servette": "Geneve-Servette HC",
    "Kölner Haie": "Kolner Haie",
    "Eisbären Berlin": "Eisbaren Berlin",
    "Växjö Lakers": "Vaxjo Lakers",
    "Bili Tygri Liberec": "Bili Tygri Liberec",
}


class Chl(Adapter):
    name = "chl"
    site = "chl.hockey"

    def applies(self, parsed, tm, kb):
        c = parsed["comp"].lower()
        return "champions" in c or c.strip() == "chl"

    async def find(self, parsed, tm, kb):
        page = await get_text(SCHEDULE_PAGE, ttl=24 * 3600)
        feeds = list(dict.fromkeys(FEED_RE.findall(page or "")))
        datas = await asyncio.gather(*[get_json(u, ttl=1800) for u in feeds])
        out, seen = [], set()
        for data in datas:
            for g in (data or {}).get("data", []):
                start = (g.get("startDate") or "")[:16]          # UTC, 2026-09-03T15:30
                date = start[:10]
                if not date or days_apart(parsed["date"], date) not in (0, 1):
                    continue
                link = (g.get("link") or {}).get("url") or ""
                if not link or link in seen:
                    continue
                seen.add(link)
                teams = g.get("teams") or {}
                home = (teams.get("home") or {}).get("name") or ""
                away = (teams.get("away") or {}).get("name") or ""
                sc = (g.get("results") or {}).get("scores") or {}
                score = f"{sc.get('home')}-{sc.get('away')}" if g.get("status") == "finished" and sc else None
                cand = dict(source=self.name, site=self.site, league="Champions Hockey League", home=NAMES.get(home, home),
                            away=NAMES.get(away, away), date=date, score=score, status=g.get("status"), kind="protocol",
                            adapter=self.name, url=BASE + "/en" + link, start_utc=start or None)
                score_candidate(cand, parsed, "")
                if min(cand["team_scores"]) >= 0.6:
                    out.append(cand)
        return out

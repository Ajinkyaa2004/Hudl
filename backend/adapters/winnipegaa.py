"""Winnipeg AA Hockey (winnipegaa.com): U13, U15 and U18 AA. The scoreboard page for a day,
/scoreboard/?date=YYYY-MM-DD, lists every game of that day as a card: both teams with their age
group ("Thrashers White", "U13"), the score once played, and the game's links. Report (the page
analysts use): https://winnipegaa.com/game/{id}/summary.

The site names teams without the city or level ("Thrashers White"), headers with both ("Winnipeg
Thrashers White U13 AA"): the age group and AA are added to the site's name. The sister sites on the
same platform (winnipegaaa.com, winnipeggirlswomenshockey.com, edgehockeyclassic.com) put a
"verify you are human" check in front of every page and can't be read."""
from __future__ import annotations
import datetime, html, re
from ..fetch import get_text
from .base import Adapter, score_candidate

BASE = "https://winnipegaa.com"
CARD = re.compile(r'class="card score-card(.*?)/game/(\d+)/summary', re.S)
TEAM = re.compile(r'class="team-details">\s*<div[^>]*>([^<]+)</div>\s*<div>\s*(U\d{2})\s*</div>.*?class="team-score[^"]*">\s*(\d*)\s*<', re.S)
TIME = re.compile(r'class="game-time[^"]*"[^>]*>\s*([^<]+?)\s*<')


def parse(page: str) -> list[dict]:
    """One dict per game card: id, away, home (with age and AA), score, time (local, None once final)."""
    out, seen = [], set()
    for m in CARD.finditer(page or ""):
        body, gid = m.group(1), m.group(2)
        if gid in seen:                   # the page draws each card twice (phone and desktop layouts)
            continue
        seen.add(gid)
        teams = TEAM.findall(body)
        if len(teams) < 2:
            continue
        (an, aa, asc), (hn, ha, hsc) = teams[:2]
        t = TIME.search(body)
        tm = re.match(r"(\d{1,2}):(\d{2})\s*([AP]M)", (t.group(1) if t else "").upper())
        out.append(dict(id=gid, away=f"{html.unescape(an).strip()} {aa} AA", home=f"{html.unescape(hn).strip()} {ha} AA",
                        score=f"{hsc}-{asc}" if hsc != "" and asc != "" else None,
                        time=f"{int(tm.group(1)) % 12 + (12 if tm.group(3) == 'PM' else 0):02d}:{tm.group(2)}" if tm else None))
    return out


class WinnipegAA(Adapter):
    name = "winnipegaa"
    site = "winnipegaa.com"

    def applies(self, parsed, tm, kb):
        c = parsed["comp"].lower()
        names = (parsed["t1"] + " " + parsed["t2"]).lower()
        aa = re.search(r"\bu?1[358]u?\s*aa\b(?!a)|\baa\b(?!a)", c + " " + names)
        return bool(aa) and ("winnipeg" in c or ("winnipeg" in names and "hockey canada" in c))

    def preferred_date(self, parsed):
        d = datetime.date.fromisoformat(parsed["date"])
        t = parsed.get("time")
        return (d - datetime.timedelta(days=1)).isoformat() if (not t or t < "12:00") else parsed["date"]

    async def find(self, parsed, tm, kb):
        d0 = datetime.date.fromisoformat(parsed["date"])
        out = []
        for day in (d0 - datetime.timedelta(days=1), d0, d0 + datetime.timedelta(days=1)):
            page = await get_text(f"{BASE}/scoreboard/?date={day.isoformat()}", ttl=1800, timeout=30)
            for g in parse(page or ""):
                cand = dict(source=self.name, site=self.site, league="Winnipeg AA Hockey", home=g["home"], away=g["away"],
                            date=day.isoformat(), time=g["time"], score=g["score"], status="final" if g["score"] else None,
                            kind="protocol", adapter=self.name, url=f"{BASE}/game/{g['id']}/summary", start_utc=None)
                score_candidate(cand, parsed, "Winnipeg AA Hockey")
                if min(cand["team_scores"]) >= 0.6:
                    out.append(cand)
        return out

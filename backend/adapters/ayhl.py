"""Atlantic Youth Hockey League (atlantichockey.org): one public HTML page lists the whole
season's schedule and results; each game has a recap page (the report)."""
from __future__ import annotations
import datetime, html, re
from ..fetch import get_text
from .base import Adapter, local_to_utc
from .na_util import local_date, near, clock24, swap_words, division_age, score_local

SITE = "https://atlantichockey.org"
SCHEDULE = SITE + "/full_schedule.php"
REPORT = SITE + "/game_recap.php?gameid={gid}"
TZ = "America/New_York"
# the site spells clubs out; headers use the short forms
NAMES = {"Little Capitals": "Little Caps", "Junior Flyers": "Jr. Flyers", "Junior Titans": "Jr. Titans",
         "WB/Scranton Jr. Knights": "Wilkes-Barre/Scran Knights", "Elite Hockey": "Elite Hockey Academy"}
# 'Sat, 09/12/26 5:40 PM - 14U Major 12<br>4 <a ...>Visitor</a> <br> 2 <a ...>Home</a> ... gameid=46590'
ROW = re.compile(r"<td[^>]*>\s*\w{3},\s*(\d\d)/(\d\d)/(\d\d)\s+(\d{1,2}:\d\d\s*[AP]M)\s*-\s*([^<]*?)<br>(.*?)</td>\s*<td[^>]*>\s*<a href=\"game_(?:recap|preview)\.php\?gameid=(\d+)", re.S | re.I)
TEAM = re.compile(r"(\d+)?\s*(?:<span[^>]*>[^<]*</span>)?\s*<a href=\"teamroster\.php[^\"]*\">([^<]+)</a>", re.S)


def parse(page: str) -> list[dict]:
    games = []
    for m in ROW.finditer(page or ""):
        mo, dd, yy, tm, div, body, gid = m.groups()
        teams = TEAM.findall(body)
        if len(teams) != 2:
            continue
        (vs, vname), (hs, hname) = teams
        games.append(dict(gid=gid, date=f"20{yy}-{mo}-{dd}", time=clock24(tm), division=re.sub(r"\s+", " ", html.unescape(div)).strip(),
                          away=html.unescape(vname).strip(), home=html.unescape(hname).strip(),
                          score=f"{hs}-{vs}" if hs and vs else None))
    return games


class Ayhl(Adapter):
    name = "ayhl"
    site = "atlantichockey.org"

    def applies(self, parsed, tm, kb):
        c = parsed["comp"].lower()
        if "atlantic youth" in c or re.search(r"\bayhl\b", c):
            return True
        for key in ("t1n", "t2n"):
            b = tm[key]["best"] or {}
            if any("atlantichockey.org" in (b.get(f) or "") for f in ("site", "schedule", "roster")):
                return True
        return False

    def preferred_date(self, parsed):
        return local_date(parsed, TZ)

    async def find(self, parsed, tm, kb):
        page = await get_text(SCHEDULE, ttl=3 * 3600, timeout=40)
        out = []
        for g in parse(page):
            if not near(parsed, g["date"]):
                continue
            age, _ = division_age(g["division"])
            div = g["division"] if (not age or age.lower() in g["division"].lower()) else f"{age} {g['division'].split(' ', 1)[-1]}"
            league = f"Atlantic Youth Hockey League {div}"
            cand = dict(source=self.name, site=self.site, league=league,
                        home=swap_words(g["home"], NAMES), away=swap_words(g["away"], NAMES), date=g["date"],
                        score=g["score"], status=None, kind="protocol", adapter=self.name,
                        url=REPORT.format(gid=g["gid"]), start_utc=local_to_utc(g["date"], g["time"], TZ))
            score_local(cand, parsed, league, TZ)
            if min(cand["team_scores"]) >= 0.6:
                out.append(cand)
        return out

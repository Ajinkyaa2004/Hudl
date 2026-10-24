"""MMJHL, Manitoba Major Junior Hockey League (mmjhl.ca): the season schedule page lists every game
(date, both teams, score once played) with a box-score link, https://www.mmjhl.ca/box/{id}.
Pages: /schedule/2026-2027/league (also /exhibition and /playoffs). No start time on the schedule,
so the local date (America/Winnipeg) decides, with the day before preferred for a morning header."""
from __future__ import annotations
import datetime, html, re
from ..fetch import get_text
from .base import Adapter, score_candidate

BASE = "https://www.mmjhl.ca"
MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
DATE = re.compile(r">\s*([A-Z][a-z]{2}) (\d{1,2})\s*<")
TEAM = re.compile(r"<a[^>]*class=['\"]font-weight-(?:bold|normal)['\"][^>]*title=['\"]([^'\"]+)['\"]")
SCORE = re.compile(r"<span class=['\"]font-weight-(?:bold|normal)['\"]>(\d+)</span>")


def season_of(date: str) -> str:
    y = int(date[:4])
    return f"{y}-{y + 1}" if date[5:7] >= "07" else f"{y - 1}-{y}"


def parse(page: str, year_hint: int) -> list[dict]:
    """One dict per game with a box-score link: id, date, home, away, score."""
    out = []
    for m in re.finditer(r"href=['\"]/box/(\d+)['\"]", page):
        window = page[max(0, m.start() - 3500):m.start()]
        d = list(DATE.finditer(window))
        if not d:
            continue
        after = window[d[-1].end():]
        teams = TEAM.findall(after)[-2:]
        scores = SCORE.findall(after)[-2:]
        if len(teams) < 2:
            continue
        mon = MONTHS.get(d[-1].group(1))
        if not mon:
            continue
        year = year_hint if mon >= 7 else year_hint + 1
        out.append(dict(id=m.group(1), date=f"{year}-{mon:02d}-{int(d[-1].group(2)):02d}", away=html.unescape(teams[0]),
                        home=html.unescape(teams[1]), score=f"{scores[1]}-{scores[0]}" if len(scores) == 2 else None))
    return out


class Mmjhl(Adapter):
    name = "mmjhl"
    site = "mmjhl.ca"

    def applies(self, parsed, tm, kb):
        c = parsed["comp"].lower()
        return "mmjhl" in c or "manitoba major junior" in c

    def preferred_date(self, parsed):
        d = datetime.date.fromisoformat(parsed["date"])
        t = parsed.get("time")
        return (d - datetime.timedelta(days=1)).isoformat() if (not t or t < "12:00") else parsed["date"]

    async def find(self, parsed, tm, kb):
        season = season_of(parsed["date"])
        year = int(season[:4])
        out, seen = [], set()
        for kind in ("league", "exhibition", "playoffs"):
            page = await get_text(f"{BASE}/schedule/{season}/{kind}", ttl=1800, timeout=30)
            if not page or "404" in page[:400]:
                continue
            for g in parse(page, year):
                if g["id"] in seen or abs((datetime.date.fromisoformat(g["date"]) - datetime.date.fromisoformat(parsed["date"])).days) > 1:
                    continue
                seen.add(g["id"])
                cand = dict(source=self.name, site=self.site, league=f"MMJHL {kind}", home=g["home"], away=g["away"], date=g["date"],
                            score=g["score"], status="final" if g["score"] else None, kind="protocol", adapter=self.name,
                            url=f"{BASE}/box/{g['id']}", start_utc=None)
                score_candidate(cand, parsed, "MMJHL")
                if min(cand["team_scores"]) >= 0.6:
                    out.append(cand)
        return out

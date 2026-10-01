"""PENNY DEL (penny-del.org), Germany's top league: month schedule pages
(/spiele/monat/<month>) list date, time, both teams and the /statistik/spieldetails/ link."""
from __future__ import annotations
import asyncio, html, re
from ..fetch import get_text
from .base import Adapter, score_candidate, days_apart, local_to_utc

BASE = "https://www.penny-del.org"
MONTHS = {1: "januar", 2: "februar", 3: "maerz", 4: "april", 5: "mai", 6: "juni", 7: "juli", 8: "august",
          9: "september", 10: "oktober", 11: "november", 12: "dezember"}
PAGES = [BASE + "/spiele", BASE + "/spiele/monat/{m}"]

# site name -> names the headers use
NAMES = {
    "Pinguins Bremerhaven": "Fischtown Pinguins Bremerhaven",
    "EHC Red Bull München": "EHC Red Bull Munchen",
    "Nürnberg Ice Tigers": "Nurnberg Ice Tigers",
    "Kölner Haie": "Kolner Haie",
    "Löwen Frankfurt": "Lowen Frankfurt",
    "Eisbären Berlin": "Eisbaren Berlin",
    "Augsburger Panther": "Augsburger Panther",
}


def parse(page: str):
    games = []
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page or "", re.S):
        m = re.search(r'href="(/statistik/spieldetails/[^"#]+)"', r)
        if not m:
            continue
        cells = [c.strip() for c in html.unescape(re.sub(r"<[^>]+>", "|", r)).split("|") if c.strip()]
        d = next((re.search(r"(\d{2})\.(\d{2})\.(\d{4})", c) for c in cells if re.search(r"\d{2}\.\d{2}\.\d{4}", c)), None)
        if not d:
            continue
        date = f"{d.group(3)}-{d.group(2)}-{d.group(1)}"
        t = next((c for c in cells if re.fullmatch(r"\d{1,2}:\d{2}", c)), None)
        names = re.findall(r'<h6 class="team-meta__name[^"]*">\s*<a[^>]*>\s*([^<]+?)\s*</a>', r)
        if len(names) < 2:
            continue
        sc = re.search(r"(\d+)\s*:\s*(\d+)", html.unescape(re.sub(r"<[^>]+>", " ", r.split("spieldetails", 1)[1])) if "spieldetails" in r else "")
        games.append(dict(url=BASE + m.group(1), date=date, time=t, home=html.unescape(names[0]), away=html.unescape(names[1]),
                          score=f"{sc.group(1)}-{sc.group(2)}" if sc else None))
    return games


class PennyDel(Adapter):
    name = "pennydel"
    site = "penny-del.org"

    def applies(self, parsed, tm, kb):
        c = parsed["comp"].lower()
        if re.search(r"liga\s*2|del\s*-?\s*2|oberliga|u\d\d|dnl", c):
            return False
        return "deutsche eishockey liga" in c or re.search(r"\bdel\b", c) is not None or "penny" in c

    async def find(self, parsed, tm, kb):
        m = int(parsed["date"][5:7])
        prev = 12 if m == 1 else m - 1
        urls = [PAGES[0], PAGES[1].format(m=MONTHS[m])]
        if parsed["date"][8:10] == "01":
            urls.append(PAGES[1].format(m=MONTHS[prev]))
        pages = await asyncio.gather(*[get_text(u, ttl=1800) for u in urls])
        seen, out = set(), []
        for p in pages:
            for g in parse(p or ""):
                if g["url"] in seen or days_apart(parsed["date"], g["date"]) not in (0, 1):
                    continue
                seen.add(g["url"])
                cand = dict(source=self.name, site=self.site, league="PENNY DEL", home=NAMES.get(g["home"], g["home"]),
                            away=NAMES.get(g["away"], g["away"]), date=g["date"], score=g["score"], status=None, kind="protocol",
                            adapter=self.name, url=g["url"], start_utc=local_to_utc(g["date"], g["time"], "Europe/Berlin"))
                score_candidate(cand, parsed, "")
                if min(cand["team_scores"]) >= 0.6:
                    out.append(cand)
        return out

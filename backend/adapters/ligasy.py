"""pro.ligasy.kz (Kazakhstan): season schedule page. Teams appear as three-letter codes
(nom, ert, arl); codes are learned from report links whose header names are known
(seeded from the Progress sheet, extended whenever a Kazakh link is saved)."""
from __future__ import annotations
import re, json
from pathlib import Path
from ..fetch import get_text
from .base import Adapter, score_candidate, days_apart

CODES = Path(__file__).resolve().parent.parent.parent / "data" / "ligasy_codes.json"
SCHED = "https://pro.ligasy.kz/ru/schedule"
GAME = "https://pro.ligasy.kz/ru/game/{gid}-{a}-{b}"
LINK_RE = re.compile(r"ligasy\.kz/\w+/game/(\d+)-([a-z]+)-([a-z]+)")


def codes() -> dict:
    try:
        return json.loads(CODES.read_text())
    except Exception:
        return {}


def learn(url: str, home: str, away: str):
    m = LINK_RE.search(url or "")
    if not m:
        return
    c = codes()
    c.setdefault(m.group(2), home)
    c.setdefault(m.group(3), away)
    CODES.write_text(json.dumps(c, ensure_ascii=False, indent=1))


def parse(page: str):
    last, games = None, {}
    for m in re.finditer(r'\b(\d{1,2})\.\s?(\d{1,2})\.\s?(20\d\d)\b|href="[^"]*/game/(\d+)-([a-z]+)-([a-z]+)"', page):
        if m.group(1):
            last = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        elif m.group(4) not in games:
            games[m.group(4)] = (last, m.group(5), m.group(6))
    return games


class Ligasy(Adapter):
    name = "ligasy"
    site = "pro.ligasy.kz"
    # the schedule page mixes a strip where the date follows the game with a list where it
    # precedes it, and game pages do not show their own date: never pick between days
    ambiguous_dates = True

    def applies(self, parsed, tm, kb):
        c = parsed["comp"].lower()
        if "kazakh" in c:
            return True
        cn = {v.lower() for v in codes().values()}
        return parsed["t1"].lower() in cn or parsed["t2"].lower() in cn

    async def find(self, parsed, tm, kb):
        page = await get_text(SCHED, ttl=3600)
        if not page:
            return []
        names = codes()
        out = []
        for gid, (date, a, b) in parse(page).items():
            if not date or days_apart(parsed["date"], date) not in (0, 1):
                continue
            cand = dict(source=self.name, site=self.site, league="Kazakhstan Pro Hokei Ligasy", home=names.get(a, a.upper()), away=names.get(b, b.upper()),
                        date=date, score=None, status=None, kind="protocol", adapter=self.name, url=GAME.format(gid=gid, a=a, b=b))
            score_candidate(cand, parsed, "")
            if min(cand["team_scores"]) >= 0.6:
                out.append(cand)
        return out

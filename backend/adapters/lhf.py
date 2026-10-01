"""Latvia (lhf.lv): Optibet hokeja liga. The tournament page links the current season's
tournament id (/turniri/<slug>/<season>/<id>); its /speles page lists every game with date,
time, both teams and the /speles/<abc>-<abc>/<yyyymmdd>/<id> report link."""
from __future__ import annotations
import asyncio, html, re
from ..fetch import get_text
from .base import Adapter, score_candidate, days_apart, local_to_utc

BASE = "https://www.lhf.lv"
TOURNAMENTS = {"optibet-hokeja-liga": "Optibet hokeja liga"}

# site name -> names the headers use
NAMES = {
    "HK Prizma/RTU": "HS Prizma Riga RTU",
    "RĪGAS HS/DINABURGA": "HS Riga Rigas Dinaburga",
    "Liepājas hokeja komanda": "HK Liepaja hokeja komanda",
    "HC Hockey Punks-Mototoja": "Hockey Punks Mototoja",
}


def parse(page: str):
    games = []
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page or "", re.S):
        m = re.search(r'href="([^"]*/speles/[a-z0-9-]+/(\d{8})/(\d+))"', r)
        if not m:
            continue
        cells = [c.strip() for c in html.unescape(re.sub(r"<[^>]+>", "|", r)).split("|") if c.strip()]
        i = next((k for k, c in enumerate(cells) if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", c)), None)
        if i is None or i < 3:
            continue
        d = cells[i]
        # cells: #n, CODE, CODE, home, away, arena, date, time, ...
        names = [c for c in cells[:i] if not re.fullmatch(r"#\d+|[A-Z0-9]{2,4}", c)]
        if len(names) < 2:
            continue
        t = cells[i + 1] if i + 1 < len(cells) and re.fullmatch(r"\d{1,2}:\d{2}", cells[i + 1]) else None
        sc = re.fullmatch(r"(\d+):(\d+)", cells[-1])
        games.append(dict(url=BASE + m.group(1), gid=m.group(3), date=f"{d[6:]}-{d[3:5]}-{d[:2]}", time=t,
                          home=names[0], away=names[1], score=f"{sc.group(1)}-{sc.group(2)}" if sc else None))
    return games


class Lhf(Adapter):
    name = "lhf"
    site = "lhf.lv"

    def applies(self, parsed, tm, kb):
        c = parsed["comp"].lower()
        return "optibet" in c or "latvia" in c or "latvij" in c or "hokeja liga" in c

    async def find(self, parsed, tm, kb):
        y = int(parsed["date"][:4]) if parsed["date"][5:7] >= "07" else int(parsed["date"][:4]) - 1
        season = f"{y}-{y + 1}"
        out, seen = [], set()
        for slug, lname in TOURNAMENTS.items():
            idx = await get_text(f"{BASE}/lv/turniri/{slug}", ttl=24 * 3600)
            ids = sorted(set(re.findall(rf"/lv/turniri/{slug}/{season}/(\d+)", idx or "")))
            pages = await asyncio.gather(*[get_text(f"{BASE}/lv/turniri/{slug}/{season}/{tid}/speles", ttl=1800) for tid in ids])
            for page in pages:
                for g in parse(page):
                    if g["gid"] in seen or days_apart(parsed["date"], g["date"]) not in (0, 1):
                        continue
                    seen.add(g["gid"])
                    cand = dict(source=self.name, site=self.site, league=lname, home=NAMES.get(g["home"], g["home"]),
                                away=NAMES.get(g["away"], g["away"]), date=g["date"], score=g["score"], status=None, kind="protocol",
                                adapter=self.name, url=g["url"], start_utc=local_to_utc(g["date"], g["time"], "Europe/Riga"))
                    score_candidate(cand, parsed, "")
                    if min(cand["team_scores"]) >= 0.6:
                        out.append(cand)
        return out

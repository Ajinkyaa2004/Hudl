"""del-2.org (German second league): season and friendlies schedule pages. Team names
come from the game link (/spiel/home-vs-away_id), the date from the list, or from the
game page title when the list shows it in the undated 'current games' block."""
from __future__ import annotations
import re, asyncio, html
from ..fetch import get_text
from .base import Adapter, score_candidate, days_apart

LIST = "https://www.del-2.org/spielplan/?round={r}"
GAME = "https://www.del-2.org/spiel/{slug}_{gid}"
ROUND_RE = re.compile(r'value="/spielplan/\?round=(\d+)"[^>]*>\s*(\d{4})/(\d{4})\s*-\s*([^<]+)<')


def parse_list(page: str):
    last, seen = None, {}
    for m in re.finditer(r'\b(\d{2})\.(\d{2})\.(\d{2})\b|href="/spiel/([^"]+)_(\d+)"', page):
        if m.group(1):
            last = f"20{m.group(3)}-{m.group(2)}-{m.group(1)}"
        elif m.group(5) not in seen:
            seen[m.group(5)] = (last, m.group(4))
    return seen


def slug_teams(slug: str):
    parts = slug.split("-vs-")
    if len(parts) != 2:
        return None, None
    return tuple(p.replace("-", " ").title() for p in parts)


class Del2(Adapter):
    name = "del2"
    site = "del-2.org"

    def applies(self, parsed, tm, kb):
        c = parsed["comp"].lower()
        if "del2" in c or "del 2" in c or "2. liga" in c and "german" in c or "friendl" in c or "test tag" in c:
            return True
        for key in ("t1n", "t2n"):
            b = tm[key]["best"] or (tm[key]["candidates"][0] if tm[key]["candidates"] else None)
            if b and str(b.get("country") or "").lower() == "germany":
                return True
        return False

    async def find(self, parsed, tm, kb):
        idx = await get_text("https://www.del-2.org/spielplan/", ttl=24 * 3600)
        if not idx:
            return []
        season = parsed["date"][:4] if parsed["date"][5:7] >= "07" else str(int(parsed["date"][:4]) - 1)
        rounds = [r for r, y1, y2, _ in ROUND_RE.findall(idx) if y1 == season]
        pages = await asyncio.gather(*[get_text(LIST.format(r=r), ttl=3600) for r in rounds])
        games = {}
        for p in pages:
            if p:
                games.update(parse_list(p))
        out, need_title = [], []
        for gid, (date, slug) in games.items():
            home, away = slug_teams(slug)
            if not home:
                continue
            if date is None:
                need_title.append((gid, slug, home, away))
                continue
            if days_apart(parsed["date"], date) in (0, 1):
                out.append((gid, slug, home, away, date))
        # undated games: read the date from the page title, only for plausible team pairs
        from .base import team_similarity
        async def dated(gid, slug, home, away):
            if max(team_similarity(parsed["t1"], home)[0], team_similarity(parsed["t1"], away)[0]) < 0.6:
                return None
            t = await get_text(GAME.format(slug=slug, gid=gid), ttl=6 * 3600)
            m = re.search(r"<title>[^<]*?(\d{2})\.(\d{2})\.(\d{2})", t or "")
            return (gid, slug, home, away, f"20{m.group(3)}-{m.group(2)}-{m.group(1)}") if m else None
        for r in await asyncio.gather(*[dated(*x) for x in need_title]):
            if r and days_apart(parsed["date"], r[4]) in (0, 1):
                out.append(r)
        cands = []
        for gid, slug, home, away, date in out:
            cand = dict(source=self.name, site=self.site, league="DEL2", home=home, away=away, date=date, score=None, status=None,
                        kind="protocol", adapter=self.name, url=GAME.format(slug=slug, gid=gid))
            score_candidate(cand, parsed, "DEL2")
            if min(cand["team_scores"]) >= 0.6:
                cands.append(cand)
        return cands

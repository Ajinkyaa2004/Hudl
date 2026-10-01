"""sihf.ch (Switzerland): the game-center games page is server-rendered per date."""
from __future__ import annotations
import re, html
from ..fetch import get_text
from .base import Adapter, score_candidate, local_to_utc

BY_DATE = "https://www.sihf.ch/de/game-center/spiele/?date={date}"
PREVIEW_RE = re.compile(r'<a href="https://www\.sihf\.ch/de/game-center/game/(\d+)"[^>]*class="c-game-preview[^"]*"[^>]*>(.*?)</a>', re.S)


def _cells(fragment):
    t = re.sub(r"<[^>]+>", "|", fragment)
    t = html.unescape(t)
    return [c.strip() for c in t.split("|") if c.strip()]


class Sihf(Adapter):
    name = "sihf"
    site = "sihf.ch"

    def applies(self, parsed, tm, kb):
        c = parsed["comp"].lower()
        if any(k in c for k in ("swiss", "national league", "sky swiss", "myhockey", "u20-elit", "u17-elit", "u15-elit", "postfinance", "switzerland")):
            return True
        for key in ("t1n", "t2n"):
            b = tm[key]["best"] or (tm[key]["candidates"][0] if tm[key]["candidates"] else None)
            if b and (str(b.get("country") or "").lower() == "switzerland" or "sihf.ch" in (b.get("site") or "") + (b.get("schedule") or "") + (b.get("roster") or "")):
                return True
        return False

    async def find(self, parsed, tm, kb):
        out = []
        page = await get_text(BY_DATE.format(date=parsed["date"]), ttl=1800)
        if not page:
            return out
        for gid, body in PREVIEW_RE.findall(page):
            cells = _cells(body)
            if len(cells) < 4:
                continue
            league = cells[0]
            scores = [c for c in cells if re.fullmatch(r"\d+", c)]
            # drop scores, dashes and times; the last two remaining cells are home and away
            names = [c for c in cells if c not in ("-", ":") and not re.fullmatch(r"\d+|\d{1,2}:\d{2}", c)]
            if len(names) < 4:
                continue
            home, away = names[-2], names[-1]
            score = f"{scores[0]}-{scores[1]}" if len(scores) >= 2 else None
            tm_ = next((c for c in cells if re.fullmatch(r"\d{1,2}:\d{2}", c)), None)
            cand = dict(source=self.name, site=self.site, league=league, home=home, away=away, date=parsed["date"], score=score,
                        status=None, kind="protocol", adapter=self.name, url=f"https://www.sihf.ch/de/game-center/game/{gid}",
                        start_utc=local_to_utc(parsed["date"], tm_, "Europe/Zurich"))
            score_candidate(cand, parsed, league)
            if min(cand["team_scores"]) >= 0.6:
                out.append(cand)
        return out

"""ECHL: each game has a page at echl.com/games/{local date}/{home}-vs-{away} (the link analysts
use), carrying the HockeyTech game id of its official report. No schedule feed is needed: the
page is built from the header's teams and date, and a wrong guess is a plain 404."""
from __future__ import annotations
import datetime, re
from ..fetch import get_text
from .base import Adapter, score_candidate

PAGE = "https://echl.com/games/{date}/{home}-vs-{away}"


def _slug(name: str) -> str:
    n = name.lower().replace("&", "and").replace("'", "").replace(".", "")
    return re.sub(r"[^a-z0-9]+", "-", n).strip("-")


class Echl(Adapter):
    name = "echl"
    site = "echl.com"

    def applies(self, parsed, tm, kb):
        c = parsed["comp"].lower()
        return "echl" in c or "east coast hockey league" in c

    def preferred_date(self, parsed):
        d = datetime.date.fromisoformat(parsed["date"])
        t = parsed.get("time")
        return (d - datetime.timedelta(days=1)).isoformat() if (not t or t < "12:00") else parsed["date"]

    async def find(self, parsed, tm, kb):
        first = self.preferred_date(parsed)
        dates = [first] + [d for d in (parsed["date"], (datetime.date.fromisoformat(first) - datetime.timedelta(days=1)).isoformat()) if d != first]
        names = []
        for key, raw in (("t1n", parsed["t1"]), ("t2n", parsed["t2"])):
            b = (tm.get(key) or {}).get("best") or {}
            base = [_slug(raw), _slug(b.get("name") or "")]
            # 'New Mexico Goathead' in a header, 'new-mexico-goatheads' on the site
            plural = [x[:-1] if x.endswith("s") else x + "s" for x in base if x]
            names.append([s for s in dict.fromkeys(base + plural) if s])
        out = []
        for date in dates:
            ymd = date.replace("-", "/")
            for h in names[0]:
                for a in names[1]:
                    for home, away in ((h, a), (a, h)):
                        url = PAGE.format(date=ymd, home=home, away=away)
                        page = await get_text(url, ttl=6 * 3600, timeout=20)
                        if not page or "client_code=echl" not in page:
                            continue
                        title = re.search(r"<title>\s*(.+?)\s*@\s*(.+?)\s*\|", page)
                        if not title:
                            continue
                        cand = dict(source=self.name, site=self.site, league="ECHL", home=title.group(2), away=title.group(1),
                                    date=date, score=None, status=None, kind="protocol", adapter=self.name, url=url, start_utc=None)
                        score_candidate(cand, parsed, "ECHL")
                        if min(cand["team_scores"]) >= 0.6:
                            out.append(cand)
                            return out
        return out

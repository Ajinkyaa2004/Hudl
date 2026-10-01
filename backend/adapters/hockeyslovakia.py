"""Slovakia (hockeyslovakia.sk, SZLH). The site, its shl. subdomain and every stats page sit
behind a Cloudflare bot check that headless browsers do not pass, and the federation's API
(api.hockeyslovakia.sk) needs a login. So this adapter cannot read games; it only offers the
league's results page for the header's date, where the analyst clicks the game:
    /sk/stats/results-date/<competition id>/<slug>?MatchDate=DD.MM.YYYY
It never returns a strong match (confidence 0.3, no team names), so the verdict stays Check.
Senior Slovak games (Tipsport liga, SHL) and friendlies are found by the onlajny adapter.
Competition ids per season: data/hockeyslovakia_competitions.json."""
from __future__ import annotations
import json
from pathlib import Path
from .base import Adapter

DATA = Path(__file__).resolve().parent.parent.parent / "data" / "hockeyslovakia_competitions.json"
PAGE = "https://www.hockeyslovakia.sk/sk/stats/results-date/{cid}/{slug}?MatchDate={d}"


def _country(tm, key):
    b = tm[key]["best"] or (tm[key]["candidates"][0] if tm[key]["candidates"] else None)
    return str((b or {}).get("country") or "").lower()


def load():
    try:
        return json.loads(DATA.read_text())
    except Exception:
        return {}


class HockeySlovakia(Adapter):
    name = "hockeyslovakia"
    site = "hockeyslovakia.sk"

    def _comp(self, parsed, tm):
        from ..lookup import is_friendly, strip_accents
        season = parsed["date"][:4] if parsed["date"][5:7] >= "07" else str(int(parsed["date"][:4]) - 1)
        comps = load().get(season) or {}
        c = strip_accents(parsed["comp"]).lower()
        for words, v in comps.items():
            if words != "friendly" and all(w in c for w in words.split()):
                return v
        if is_friendly(parsed["comp"]) and "slovakia" in {_country(tm, "t1n"), _country(tm, "t2n")}:
            return comps.get("friendly")
        return None

    def applies(self, parsed, tm, kb):
        return self._comp(parsed, tm) is not None

    async def find(self, parsed, tm, kb):
        v = self._comp(parsed, tm)
        if not v:
            return []
        cid, slug, lname = v
        y, m, d = parsed["date"].split("-")
        return [dict(source=self.name, site=self.site, league=lname, home="", away="", date=parsed["date"], score=None, status=None,
                     kind="schedule", adapter=self.name, url=PAGE.format(cid=cid, slug=slug, d=f"{d}.{m}.{y}"), start_utc=None,
                     confidence=0.3, team_scores=[0.0, 0.0],
                     reasons=[f"{lname} results for {d}.{m}.{y} on hockeyslovakia.sk; open the game there",
                              "hockeyslovakia.sk blocks automated reading (Cloudflare), so the game itself is not checked"])]

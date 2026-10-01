"""GameSheet, live: every GameSheet game of the header's day (all seasons at once) from the
site's public game feed. Only needed for games the daily index does not have yet, such as a
tournament season created this week; the games it reads go into the index."""
from __future__ import annotations
import datetime
from .base import Adapter, score_candidate, keep


class GameSheetLive(Adapter):
    name = "GameSheet"
    site = "gamesheetstats.com"
    budget_s = 30

    def applies(self, parsed, tm, kb):
        # North American only: skip when the competition or a team is known to be from elsewhere
        from ..lookup import comp_country
        from . import team_countries
        na = {"usa", "united states", "us", "canada"}
        cc = str(comp_country(parsed["comp"]) or "").lower()
        if cc and cc not in na:
            return False
        known = team_countries(tm["t1n"]["best"]) | team_countries(tm["t2n"]["best"])
        return not (known and not (known & na))

    def preferred_date(self, parsed):
        d = datetime.date.fromisoformat(parsed["date"])
        t = parsed.get("time")
        return (d - datetime.timedelta(days=1)).isoformat() if (not t or t < "12:00") else parsed["date"]

    async def find(self, parsed, tm, kb):
        from .. import gamesheet_api as ga, gameindex
        from ..harvest import gamesheet_rows
        d = datetime.date.fromisoformat(parsed["date"])
        games = await ga.games_by_date((d - datetime.timedelta(days=1)).isoformat(), d.isoformat())
        rows = gamesheet_rows(games or [])
        gameindex.add_games(rows)
        out = []
        for g in rows:
            cand = dict(g, adapter=self.name, kind="protocol")
            score_candidate(cand, parsed, g.get("league") or "")
            if keep(cand):
                out.append(cand)
        return out

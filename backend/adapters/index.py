"""The local game index as a source: games already harvested or seen in earlier searches.
Answers in milliseconds, so it runs first; the live sources run only when it has no single
strong match."""
from __future__ import annotations
from .base import Adapter, score_candidate, keep


class Index(Adapter):
    name = "index"
    site = "local game index"

    def applies(self, parsed, tm, kb):
        return True

    async def find(self, parsed, tm, kb):
        from .. import gameindex
        want1, want2 = gameindex.words(parsed["t1"]), gameindex.words(parsed["t2"])
        out = []
        for g in gameindex.games_on(gameindex.around(parsed["date"], 1, 1)):
            gw = gameindex.words(g["home"]) | gameindex.words(g["away"])
            # cheap filter before the full scorer: a distinctive word of either team must appear,
            # or the names are short/odd enough that words don't help
            if want1 and want2 and not ((want1 | want2) & gw):
                continue
            cand = dict(g, adapter=g["source"], kind="protocol", via="index")
            score_candidate(cand, parsed, g.get("league") or "")
            if keep(cand):
                out.append(cand)
        return out

"""Probe: what data/hockeytech_additions.json adds to the HockeyTech adapter, on the Progress
sheet's 2026-27 games linked to those sites. The additions are merged into a COPY of the
adapter's league table inside this test only (hockeytech.py is not edited).
Run: .venv/bin/python -m tests.probe_hockeytech_additions"""
from __future__ import annotations
import asyncio, json, re
from pathlib import Path
from tests.probe_na import probe, ids
from backend.adapters import hockeytech

ADD = json.loads((Path(__file__).resolve().parent.parent / "data" / "hockeytech_additions.json").read_text())
SITES = sorted({a["site"].replace("www.", "") for a in ADD} | {"csshl.ca", "lscluster.hockeytech.com"})


def same(found, want):
    a = ids(found, r"game_id=(\d+)")
    b = ids(want, r"(?:game-center/|game-summary/|game_id=)(\d+)")
    return None if b is None else a == b


class Only(hockeytech.HockeyTech):
    """The HockeyTech adapter reading only the added leagues (plus the clients they belong to)."""
    name = "HockeyTech + additions"

    def __init__(self, with_additions=True):
        self.with_additions = with_additions

    async def find(self, parsed, tm, kb):
        saved = dict(hockeytech.LEAGUES)
        try:
            keep = {k: v for k, v in saved.items() if k.split("#")[0] in {a["client_code"] for a in ADD}}
            if self.with_additions:
                for a in ADD:
                    keep[f"{a['client_code']}#{a['league_id']}"] = dict(key=a["key"], name=a["name"], client=a["client_code"], league_id=a["league_id"])
            hockeytech.LEAGUES.clear()
            hockeytech.LEAGUES.update(keep)
            return await super().find(parsed, tm, kb)
        finally:
            hockeytech.LEAGUES.clear()
            hockeytech.LEAGUES.update(saved)


async def main(verbose=True):
    pat = "|".join(re.escape(s) for s in SITES if s != "lscluster.hockeytech.com") + r"|client_code=(?:csshl|hockeyalberta|ut1hl|oua)"
    print("== without additions (current clients file only)")
    before = await probe(Only(False), pat, same, verbose=False)
    print("== with additions")
    after = await probe(Only(True), pat, same, verbose=verbose)
    return before, after

if __name__ == "__main__":
    asyncio.run(main())

"""Probe: hockeydata adapter (alps.hockey, eishockey.at federation leagues) on the Progress
sheet's 2026-27 games.  Run: .venv/bin/python -m tests.probe_hockeydata"""
from __future__ import annotations
import asyncio
from tests.probe_europe import probe, ids
from backend.adapters.hockeydata import HockeyData

UUID = r"(?:gameId=|/spiel/)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"


def same(found, want):
    a, b = ids(found, UUID), ids(want, UUID)
    return a is not None and a == b


async def main(verbose=True):
    return await probe(HockeyData(), r"alps\.hockey/|eishockey\.at/(?:en/)?game-center/(?!ice/)", same, verbose=verbose)

if __name__ == "__main__":
    asyncio.run(main())

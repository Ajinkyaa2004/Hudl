"""Probe: rampt adapter (RAMP tournament sites, EAK Tournament) on the Progress sheet's
2026-27 games.  Run: .venv/bin/python -m tests.probe_rampt"""
from __future__ import annotations
import asyncio
from tests.probe_europe import probe, ids
from backend.adapters.rampt import RampTournament


def same(found, want):
    a, b = ids(found, r"/game/view/(\d+)"), ids(want, r"/game/view/(\d+)")
    return a is not None and a == b


async def main(verbose=True):
    return await probe(RampTournament(), r"tournamentsgurus\.com/", same, verbose=verbose)

if __name__ == "__main__":
    asyncio.run(main())

"""Probe: ramp adapter (OWHL U22 Elite, OWHA links, Eastern Alliance Kickoff, Ottawa 67's AA) on the
Progress sheet's 2026-27 games. Run: .venv/bin/python -m tests.probe_ramp"""
from __future__ import annotations
import asyncio
from tests.probe_na import probe, ids
from backend.adapters.ramp import Ramp

PAT = r"/(?:game/view|gamesheet)/(\d+)"


def same(found, want):
    a, b = ids(found, PAT), ids(want, PAT)
    return None if b is None else a == b          # schedule-page links name no game


async def main(verbose=True):
    return await probe(Ramp(), r"owhlu22elite\.ca|owha\.on\.ca|tournamentsgurus\.com|ottawa67saa\.org|rampinteractive\.com", same, verbose=verbose)

if __name__ == "__main__":
    asyncio.run(main())

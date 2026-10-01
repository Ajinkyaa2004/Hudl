"""Probe: kreezee adapter (Blue Line tournaments, SFHL, Boston Breakout) on the Progress
sheet's 2026-27 games. Run: .venv/bin/python -m tests.probe_kreezee"""
from __future__ import annotations
import asyncio
from tests.probe_na import probe, ids
from backend.adapters.kreezee import Kreezee

PAT = r"(?:game-|gameId=)(\d+)"


def same(found, want):
    a, b = ids(found, PAT), ids(want, PAT)
    return None if b is None else a == b


async def main(verbose=True):
    return await probe(Kreezee(), r"bluelinetournaments\.com|sfhlhockey\.com|kreezee-sports\.com", same, verbose=verbose)

if __name__ == "__main__":
    asyncio.run(main())

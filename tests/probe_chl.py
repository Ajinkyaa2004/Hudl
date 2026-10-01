"""Probe: chl adapter (chl.hockey) on the Progress sheet's 2026-27 games.
Run: .venv/bin/python -m tests.probe_chl"""
from __future__ import annotations
import asyncio
from tests.probe_europe import probe, ids
from backend.adapters.chl import Chl


def same(found, want):
    a, b = ids(found, r"/matches/([0-9a-f]{24})"), ids(want, r"/matches/([0-9a-f]{24})")
    return a is not None and a == b


async def main(verbose=True):
    return await probe(Chl(), r"chl\.hockey/\w+/matches/", same, verbose=verbose)

if __name__ == "__main__":
    asyncio.run(main())

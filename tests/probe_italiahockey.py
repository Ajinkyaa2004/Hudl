"""Probe: italiahockey adapter (italia.hockey) on the Progress sheet's 2026-27 games.
Run: .venv/bin/python -m tests.probe_italiahockey"""
from __future__ import annotations
import asyncio
from tests.probe_europe import probe, ids
from backend.adapters.italiahockey import ItaliaHockey


def same(found, want):
    a, b = ids(found, r"[?&]id=(\d+)"), ids(want, r"[?&]id=(\d+)")
    return a is not None and a == b


async def main(verbose=True):
    return await probe(ItaliaHockey(), r"italia\.hockey/", same, verbose=verbose)

if __name__ == "__main__":
    asyncio.run(main())

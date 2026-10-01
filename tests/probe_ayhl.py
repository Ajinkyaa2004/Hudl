"""Probe: ayhl adapter (atlantichockey.org) on the Progress sheet's 2026-27 games.
Run: .venv/bin/python -m tests.probe_ayhl"""
from __future__ import annotations
import asyncio
from tests.probe_na import probe, ids
from backend.adapters.ayhl import Ayhl


def same(found, want):
    a, b = ids(found, r"gameid=(\d+)"), ids(want, r"gameid=(\d+)")
    return None if b is None else a == b


async def main(verbose=True):
    return await probe(Ayhl(), r"atlantichockey\.org/game_", same, verbose=verbose)

if __name__ == "__main__":
    asyncio.run(main())

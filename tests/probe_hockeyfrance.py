"""Probe: hockeyfrance adapter (hockeyfrance.com, FFHG) on the Progress sheet's 2026-27 games.
Run: .venv/bin/python -m tests.probe_hockeyfrance"""
from __future__ import annotations
import asyncio
from tests.probe_europe import probe, ids
from backend.adapters.hockeyfrance import HockeyFrance


def same(found, want):
    a, b = ids(found, r"rencontre/(\d+)"), ids(want, r"rencontre/(\d+)")
    return a is not None and a == b


async def main(verbose=True):
    return await probe(HockeyFrance(), r"hockeyfrance\.com/", same, verbose=verbose)

if __name__ == "__main__":
    asyncio.run(main())

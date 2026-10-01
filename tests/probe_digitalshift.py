"""Probe: digitalshift adapter (hselitehockey.com, girlshselitehockey.com, dallasstarstournaments.com)
on the Progress sheet's 2026-27 games. Run: .venv/bin/python -m tests.probe_digitalshift"""
from __future__ import annotations
import asyncio
from tests.probe_na import probe, ids
from backend.adapters.digitalshift import DigitalShift

PAT = r"/game/(\d+)"


def same(found, want):
    a, b = ids(found, PAT), ids(want, PAT)
    return None if b is None else a == b


async def main(verbose=True):
    return await probe(DigitalShift(), r"hselitehockey\.com|dallasstarstournaments\.com|hockeyshift\.com", same, verbose=verbose)

if __name__ == "__main__":
    asyncio.run(main())

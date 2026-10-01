"""Probe: pennydel adapter (penny-del.org) on the Progress sheet's 2026-27 games.
Run: .venv/bin/python -m tests.probe_pennydel"""
from __future__ import annotations
import asyncio
from tests.probe_europe import probe, ids
from backend.adapters.pennydel import PennyDel


def same(found, want):
    a, b = ids(found, r"spieldetails/\d{8}_[^/#]*?_(\d+)"), ids(want, r"spieldetails/\d{8}_[^/#]*?_(\d+)")
    return a is not None and a == b


async def main(verbose=True):
    return await probe(PennyDel(), r"penny-del\.org/", same, verbose=verbose)

if __name__ == "__main__":
    asyncio.run(main())

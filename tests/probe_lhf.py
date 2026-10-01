"""Probe: lhf adapter (lhf.lv, Optibet hokeja liga) on the Progress sheet's 2026-27 games.
Run: .venv/bin/python -m tests.probe_lhf"""
from __future__ import annotations
import asyncio
from tests.probe_europe import probe, ids
from backend.adapters.lhf import Lhf


def same(found, want):
    a, b = ids(found, r"/\d{8}/(\d+)"), ids(want, r"/\d{8}/(\d+)")
    return a is not None and a == b


async def main(verbose=True):
    return await probe(Lhf(), r"lhf\.lv/", same, verbose=verbose)

if __name__ == "__main__":
    asyncio.run(main())

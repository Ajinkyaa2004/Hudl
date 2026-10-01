"""Probe: regystra adapter (my.200x85.com tournaments) on the Progress sheet's 2026-27 games.
Run: .venv/bin/python -m tests.probe_regystra"""
from __future__ import annotations
import asyncio
from tests.probe_na import probe, ids
from backend.adapters.regystra import Regystra

PAT = r"/games/([0-9a-f-]{36})"


def same(found, want):
    a, b = ids(found, PAT), ids(want, PAT)
    return None if b is None else a == b        # 200x85.com/ccm-denver schedule links name no game


async def main(verbose=True):
    return await probe(Regystra(), r"200x85\.com", same, verbose=verbose)

if __name__ == "__main__":
    asyncio.run(main())

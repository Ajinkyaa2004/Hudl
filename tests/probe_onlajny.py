"""Probe: onlajny adapter (onlajny.com) on the Progress sheet's 2026-27 games.
Part 1: games whose analyst link is onlajny.com (compared by match id).
Part 2: Slovak games whose analyst link is hockeyslovakia.sk (blocked site): counts how many
get a Found on onlajny.com instead; ids cannot be compared, so these are listed for a look.
Run: .venv/bin/python -m tests.probe_onlajny"""
from __future__ import annotations
import asyncio
from tests.probe_europe import probe, ids
from backend.adapters.onlajny import Onlajny


def same(found, want):
    a, b = ids(found, r"/id/(\d+)"), ids(want, r"/id/(\d+)")
    return a is not None and a == b


async def main(verbose=True):
    r1 = await probe(Onlajny(), r"onlajny\.com/", same, verbose=verbose)
    print("\n-- Slovak games (analyst used hockeyslovakia.sk; 'WRONG' below only means a different site) --")
    r2 = await probe(Onlajny(), r"hockeyslovakia\.sk/", lambda f, w: False, verbose=verbose)
    return r1

if __name__ == "__main__":
    asyncio.run(main())

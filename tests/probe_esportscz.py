"""Probe: esportscz adapter (ICE Hockey League on ice.hockey, Metal Ligaen on metalligaen.dk)
on the Progress sheet's 2026-27 games.  Run: .venv/bin/python -m tests.probe_esportscz"""
from __future__ import annotations
import asyncio, re
from tests.probe_europe import probe
from backend.adapters.esportscz import EsportsCz


def gid(u):
    m = re.search(r"(?:gameId=|icehl-)(\d+)", u or "")
    host = "den" if "metalligaen" in (u or "") else "ice"
    return (host, m.group(1)) if m else None


def same(found, want):
    return gid(found) is not None and gid(found) == gid(want)


async def main(verbose=True):
    return await probe(EsportsCz(), r"ice\.hockey/|metalligaen\.dk|eishockey\.at/(?:en/)?game-center/ice/", same, verbose=verbose)

if __name__ == "__main__":
    asyncio.run(main())

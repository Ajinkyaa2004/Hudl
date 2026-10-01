"""Probe: ukhockey adapter (eliteleague.co.uk, nihlnational.com) on the Progress sheet's
2026-27 games.  Run: .venv/bin/python -m tests.probe_ukhockey
stats.nihlnational.com/pdf/print/de-html/<n> links (pre-season PDFs) use another numbering and
cannot be compared by id; for those the probe checks that the found game page links that gamesheet."""
from __future__ import annotations
import asyncio
from tests.probe_europe import probe, ids
from backend.adapters.ukhockey import UkHockey


def same(found, want):
    if "stats.nihlnational.com" in want:
        # the game page links its official gamesheet: same game when it links the analyst's sheet
        import httpx
        n = ids(want, r"de-html/(\d+)")
        page = httpx.get(found, headers={"User-Agent": "Mozilla/5.0"}, timeout=20, follow_redirects=True).text
        return bool(n) and f"de-html/{n}\"" in page
    a, b = ids(found, r"/game/(\d+)-"), ids(want, r"/game/(\d+)-")
    host = lambda u: "eihl" if "eliteleague" in u else "nihl"
    return a is not None and a == b and host(found) == host(want)


async def main(verbose=True):
    return await probe(UkHockey(), r"eliteleague\.co\.uk/|nihlnational\.com/", same, verbose=verbose)

if __name__ == "__main__":
    asyncio.run(main())

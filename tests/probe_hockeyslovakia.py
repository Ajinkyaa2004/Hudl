"""Probe: hockeyslovakia helper (results page per league and date) on the Progress sheet's
2026-27 Slovak games. It never returns a strong match; this counts how many headers get a
results-page link whose competition id equals the analyst's.
Run: .venv/bin/python -m tests.probe_hockeyslovakia"""
from __future__ import annotations
import asyncio, re
from tests.probe_europe import sheet_rows
from backend import lookup
from backend.adapters.hockeyslovakia import HockeySlovakia


async def main(verbose=True):
    a, kb = HockeySlovakia(), lookup.load_kb()
    rows = sheet_rows(r"hockeyslovakia\.sk/")
    n = linked = same_comp = 0
    for header, want in rows:
        n += 1
        p = lookup.parse_header(header)
        tm = lookup.search(header, kb)["teams"]
        c = await a.find(p, tm, kb) if a.applies(p, tm, kb) else []
        if not c:
            if verbose:
                print(f"  no link   {header[:90]}")
            continue
        linked += 1
        got = re.search(r"results-date/(\d+)/", c[0]["url"]).group(1)
        w = re.search(r"/(?:matches|results-date|teams)/(\d+)/", want)
        ok = bool(w) and w.group(1) == got
        same_comp += ok
        if verbose:
            print(f"  {'same comp' if ok else 'OTHER':9} {header[:90]}\n            {c[0]['url']}\n            want {want}")
    print(f"\n[hockeyslovakia] sheet games {n}, results-page link {linked}, same competition as analyst {same_comp} (never a Found)")

if __name__ == "__main__":
    asyncio.run(main())

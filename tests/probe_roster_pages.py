"""How well backend/roster_pages.py reads the roster pages analysts actually used.
Every non-screenshot roster link in the Progress sheet is loaded and its players extracted.

    .venv/bin/python tests/probe_roster_pages.py [--limit N] [--domain x.com]
Writes data/roster_truth.json: url -> players, used as the answer key for roster tests."""
from __future__ import annotations
import argparse, asyncio, json, re, sys, time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import openpyxl
from backend.lookup import parse_header, first_url
from backend import roster_pages

ROOT = Path(__file__).resolve().parent.parent
SHOT = {"prnt.sc", "ibb.co", "skrinshoter.ru", "drive.google.com", "imgur.com", "i.imgur.com", "flashscore.com"}


def links(include_api=False):
    out = {}
    ws = openpyxl.load_workbook(ROOT / "data" / "progress.xlsx", read_only=True, data_only=True)["Progress"]
    for r in ws.iter_rows(values_only=True):
        h = r[2]
        if not (isinstance(h, str) and h.startswith("[")):
            continue
        p = parse_header(h)
        if not p:
            continue
        for link, name in ((r[11], p["t1"]), (r[13], p["t2"])):
            u = first_url(link) if isinstance(link, str) else None
            # EliteProspects and GameSheet are read through their data servers in probe_roster_truth.py
            if u and urlparse(u).netloc.replace("www.", "") not in SHOT and (include_api or not re.search(r"eliteprospects|gamesheetstats", u)):
                out.setdefault(u, dict(team=name, date=p["date"], header=h.strip()))
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--domain", default="")
    a = ap.parse_args()
    L = links()
    all_links = links(include_api=True)
    items = [(u, v) for u, v in L.items() if a.domain in u]
    if a.limit:
        items = items[:a.limit]
    print(len(items), "roster pages")
    gates = defaultdict(asyncio.Lock)
    sem = asyncio.Semaphore(4)
    res = {}

    async def one(u, v):
        d = urlparse(u).netloc.replace("www.", "")
        async with sem, gates[d]:
            t = time.time()
            try:
                r = await asyncio.wait_for(roster_pages.read(u), 60)
            except Exception as e:
                r = dict(players=[], season=None, how="error:" + type(e).__name__)
            res[u] = dict(v, domain=d, n=len(r["players"]), season=r.get("season"), how=r.get("how"), via=r.get("via"),
                          players=r["players"], seconds=round(time.time() - t, 1))
    await asyncio.gather(*[one(u, v) for u, v in items])
    ok = [u for u, r in res.items() if r["n"] >= 8]
    print(f"\n=== pages with >= 8 players read: {len(ok)}/{len(res)} ({len(ok)/max(1,len(res)):.0%})")
    by = defaultdict(Counter)
    for u, r in res.items():
        by[r["domain"]]["ok" if r["n"] >= 8 else "fail"] += 1
    for d, c in sorted(by.items(), key=lambda x: -sum(x[1].values()))[:45]:
        print(f"  {d:42} ok {c['ok']:3}  fail {c['fail']:3}")
    print("how:", Counter(r["how"] for r in res.values()), "via:", Counter(r.get("via") for r in res.values()))
    fails = [(u, r["n"], r["how"]) for u, r in res.items() if r["n"] < 8]
    for f in fails[:60]:
        print("  FAIL", f[1], f[2], f[0][:110])
    if not a.domain and not a.limit:
        # EliteProspects / GameSheet links join the answer key unread (players filled in by probe_roster_truth.py)
        for u, v in all_links.items():
            if u not in res and re.search(r"eliteprospects|gamesheetstats", u):
                res[u] = dict(v, domain=urlparse(u).netloc.replace("www.", ""), n=0, players=[], season=None, how="api")
        (ROOT / "data" / "roster_truth.json").write_text(json.dumps(res, indent=1))

if __name__ == "__main__":
    asyncio.run(main())

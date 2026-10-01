"""Roster replay: games where the analyst attached rosters instead of a report.
For each such team, run the roster check and compare with the analyst's roster link.

    .venv/bin/python -m backend.replay_rosters --sample 120
"""
from __future__ import annotations
import argparse, asyncio, csv, datetime, random, re
from collections import Counter
from pathlib import Path
import openpyxl

from . import lookup
from .lookup import parse_header, first_url, domain
from .rosters import check_team, MHR_T

ROOT = Path(__file__).resolve().parent.parent
SHOT = {"prnt.sc", "ibb.co", "skrinshoter.ru", "drive.google.com"}
EP_T = re.compile(r"eliteprospects\.com/team/(\d+)")


def same_page(a: str, b: str) -> bool | None:
    """True/False when the two roster links can be compared, None when not comparable."""
    if not a or not b or domain(a) in SHOT:
        return None
    for rx in (MHR_T, EP_T):
        ma, mb = rx.search(a), rx.search(b)
        if ma and mb:
            return ma.group(1) == mb.group(1)
    na = re.sub(r"^https?://(www\.)?|[?#].*$|/$", "", a).lower()
    nb = re.sub(r"^https?://(www\.)?|[?#].*$|/$", "", b).lower()
    if domain(a) == domain(b):
        return na == nb
    return False


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=120)
    ap.add_argument("--start", default="2026-08-01")
    ap.add_argument("--end", default="2026-09-21")
    args = ap.parse_args()
    kb = lookup.load_kb()
    rows = []
    for r in openpyxl.load_workbook(ROOT / "data" / "progress.xlsx", read_only=True, data_only=True)["Progress"].iter_rows(values_only=True):
        h = r[2]
        if not (isinstance(h, str) and h.startswith("[")):
            continue
        p = parse_header(h)
        if not p or not (args.start <= p["date"] <= args.end):
            continue
        for key, status, link, name in (("t1n", r[10], r[11], p["t1"]), ("t2n", r[12], r[13], p["t2"])):
            if "Not in" in str(status):
                rows.append(dict(header=h.strip(), parsed=p, key=key, team=name, analyst=first_url(link)))
    random.seed(11)
    rows = random.sample(rows, min(args.sample, len(rows)))
    print(f"{len(rows)} team rosters to check")
    sem = asyncio.Semaphore(4)
    out = []

    async def one(x):
        async with sem:
            s = lookup.search(x["header"], kb)
            team = s["teams"][x["key"]]["best"]
            res = await check_team(team, x["team"], x["parsed"]["date"])
        best = res[0] if res else {}
        agree = None
        if x["analyst"]:
            comps = [same_page(x["analyst"], c["url"]) for c in res]
            known = [c for c in comps if c is not None]
            agree = any(known) if known else None
        out.append(dict(header=x["header"], team=x["team"], matched=(team or {}).get("name", ""), analyst=x["analyst"] or "",
                        analyst_site=domain(x["analyst"]) if x["analyst"] else "", best_status=best.get("status", "no link on file"),
                        best_url=best.get("url", ""), players=best.get("players", ""), season=best.get("season", ""), age_fit=best.get("age_fit", ""),
                        n_links=len(res), any_verified=any(c.get("status") == "verified" for c in res),
                        age_mismatch=any((c.get("age_fit") or "").startswith("does not") for c in res),
                        agree="" if agree is None else ("yes" if agree else "no")))

    await asyncio.gather(*[one(x) for x in rows])
    n = len(out)
    C = Counter(o["best_status"] for o in out)
    good = C["verified"] + C["probable"]
    print("\n=== ROSTER CHECK ===")
    print(f"best link per team: {dict(C)}")
    print(f"verified or probably current: {good}/{n} ({good/max(1,n):.0%})")
    print(f"team matched in Club Data: {sum(1 for o in out if o['matched'])}/{n}")
    comp = [o for o in out if o["agree"]]
    print(f"analyst's roster page among the tool's links (where comparable): {sum(o['agree']=='yes' for o in comp)}/{len(comp)}")
    print(f"  comparable by site: {dict(Counter(o['analyst_site'] for o in comp))}")
    print(f"rosters flagged as the wrong age group: {sum(o['age_mismatch'] for o in out)}")
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    path = ROOT / "tests" / f"rosters_{stamp}.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader(); w.writerows(out)
    print(f"rows written to {path}")


if __name__ == "__main__":
    asyncio.run(main())

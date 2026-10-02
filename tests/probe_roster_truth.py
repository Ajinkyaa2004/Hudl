"""Are the tool's rosters the right ones? The answer key is the roster page the analyst used
(data/roster_truth.json from tests/probe_roster_pages.py; EliteProspects and GameSheet links are
read through their data servers). For each team the tool's best roster is compared with the
analyst's by player names.

    .venv/bin/python tests/probe_roster_truth.py [--sample N] [--no-fallback]

correct:  the tool's best roster shares >= 60% of the analyst's players
partial:  30-60% shared (same team, older or partial list)
other:    the tool's best roster is a different team (< 30% shared)
none:     no roster from the tool"""
from __future__ import annotations
import argparse, asyncio, json, random, re, sys, time
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import roster_sources as RS
from backend.lookup import load_kb, match_team, parse_header, strip_accents

ROOT = Path(__file__).resolve().parent.parent


def key(name: str) -> str:
    parts = re.findall(r"[a-z]+", strip_accents(str(name or "")).lower())
    return (parts[-1] + parts[0][:1]) if len(parts) > 1 else (parts[0] if parts else "")


def shared(truth: list, got: list) -> float:
    a, b = {key(p["name"]) for p in truth if p.get("name")}, {key(p["name"]) for p in got if p.get("name")}
    return len(a & b) / len(a) if a else 0.0


async def truth_players(u: str, v: dict) -> list:
    if v.get("n", 0) >= 8:
        return v["players"]
    m = RS.EP_TEAM.search(u)
    if m:
        r = await RS.ep_roster(m.group(1), v["date"])
        return (r or {}).get("players") or []
    m = RS.GS_TEAM_LINK.search(u)
    if m:
        r = await RS.gs_roster(m.group(1), m.group(2), v["date"])
        return (r or {}).get("players") or []
    return []


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--no-fallback", action="store_true")
    ap.add_argument("--seed", type=int, default=5)
    a = ap.parse_args()
    T = json.loads((ROOT / "data" / "roster_truth.json").read_text())
    kb = load_kb()
    items = list(T.items())
    random.seed(a.seed)
    if a.sample:
        items = random.sample(items, min(a.sample, len(items)))
    sem = asyncio.Semaphore(3)
    rows = []

    async def one(u, v):
        async with sem:
            tp = await truth_players(u, v)
            if len(tp) < 8:
                return
            p = parse_header(v["header"])
            best_team = next(iter(match_team(v["team"], kb)), None)
            t0 = time.time()
            try:
                rs = await asyncio.wait_for(RS.find_rosters(v["team"], v["date"], dict(team=best_team or {}, comp=p["comp"] if p else "", kb=kb,
                                                                                    fallback=not a.no_fallback)), 120)
            except Exception as e:
                rs = []
            top = [r for r in rs if r.get("players")][:3]
            sb = shared(tp, top[0]["players"]) if top else 0.0
            s3 = max([shared(tp, r["players"]) for r in top], default=0.0)
            verdict = "none" if not top else "correct" if sb >= 0.6 else "partial" if sb >= 0.3 else "other"
            rows.append(dict(team=v["team"], url=u, domain=v["domain"], verdict=verdict, best_shared=round(sb, 2), top3_shared=round(s3, 2),
                             best_status=top[0]["status"] if top else "", best_source=top[0]["source"] if top else "", best_url=top[0]["url"] if top else "",
                             seconds=round(time.time() - t0, 1)))
    await asyncio.gather(*[one(u, v) for u, v in items])
    n = len(rows)
    C = Counter(r["verdict"] for r in rows)
    print(f"\n=== ROSTER CORRECTNESS vs the analyst's roster page ({n} teams, fallback {'off' if a.no_fallback else 'on'}) ===")
    for k in ("correct", "partial", "other", "none"):
        print(f"  {k:8} {C[k]:4}  ({C[k]/max(1,n):.0%})")
    print(f"  right roster anywhere in the top 3: {sum(1 for r in rows if r['top3_shared'] >= 0.6)}/{n}")
    print("  best source when correct:", Counter(r["best_source"] for r in rows if r["verdict"] == "correct"))
    print("  'other' with status verified (shown as current but another team):", sum(1 for r in rows if r["verdict"] == "other" and r["best_status"] == "verified"))
    for r in sorted(rows, key=lambda r: r["verdict"]):
        if r["verdict"] in ("other", "none", "partial"):
            print(f"  {r['verdict']:7} {r['team'][:38]:38} {r['best_status']:12} {r['best_source'][:14]:14} shared {r['best_shared']:.2f} | analyst {r['url'][:70]}")
    stamp = time.strftime("%Y%m%d_%H%M")
    (ROOT / "tests" / f"roster_truth_{stamp}.json").write_text(json.dumps(rows, indent=1))

if __name__ == "__main__":
    asyncio.run(main())

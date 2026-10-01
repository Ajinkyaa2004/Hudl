"""Probe backend/roster_sources.find_rosters on the 200 teams where analysts attached rosters
(tests/rosters_20260924_2018.csv, from the Progress sheet's "Not in protocol" rows).

For each team: does the tool now give a CURRENT-season (2026-27) roster with 10+ players, which source,
and does it agree with the roster the analyst attached?

    .venv/bin/python tests/probe_roster_sources.py                 # API sources + fallback page checks
    .venv/bin/python tests/probe_roster_sources.py --no-fallback   # API sources only (fast)
    .venv/bin/python tests/probe_roster_sources.py --limit 40

Agreement:
  same page    the analyst's link is one of the tool's roster pages (same EP team id, MHR team id,
               GameSheet team, or same URL)
  same source  the tool's best roster comes from the analyst's site family
  players      when the analyst linked an EliteProspects team, the share of the tool's best-roster
               players that are also on that EP team's 2026-27 roster (read through the GraphQL API)
Rows are written to data/roster_probe_results.json.
"""
from __future__ import annotations
import argparse, asyncio, csv, json, re, sys, time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import lookup                                   # noqa: E402
from backend.lookup import parse_header, first_url, domain, norm   # noqa: E402
from backend.roster_sources import find_rosters, ep_roster, EP_TEAM  # noqa: E402

CSV = ROOT / "tests" / "rosters_20260924_2018.csv"
OUT = ROOT / "data" / "roster_probe_results.json"
MHR_T = re.compile(r"myhockeyrankings\.com/team[-_]info(?:\.php)?(?:\?[^#]*?\bt=|/)(\d+)")
GS_T = re.compile(r"gamesheetstats\.com/seasons/\d+/teams/(\d+)")
SHOTS = {"prnt.sc", "ibb.co", "skrinshoter.ru", "drive.google.com", "imgur.com", "gyazo.com"}


def family(url: str) -> str:
    d = domain(url)
    if "eliteprospects" in d: return "EliteProspects"
    if "myhockeyrankings" in d: return "MyHockeyRankings"
    if "gamesheet" in d: return "GameSheet"
    if "timetoscore" in d: return "TimeToScore"
    if "hockeytech" in d or "/stats/roster/" in url: return "HockeyTech"
    if d in SHOTS: return "screenshot"
    return "other site" if d else ""


def same_page(a: str, b: str) -> bool:
    for rx in (EP_TEAM, MHR_T, GS_T):
        ma, mb = rx.search(a or ""), rx.search(b or "")
        if ma and mb:
            return ma.group(1) == mb.group(1)
    na = re.sub(r"^https?://(www\.)?|[?#].*$|/$", "", a or "").lower()
    nb = re.sub(r"^https?://(www\.)?|[?#].*$|/$", "", b or "").lower()
    return bool(na) and na == nb


def pnorm(name: str) -> str:
    parts = re.findall(r"[a-z]+", lookup.strip_accents(str(name or "")).lower())
    return parts[-1] + (parts[0][:1] if len(parts) > 1 else "") if parts else ""


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fallback", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    kb = lookup.load_kb()
    rows = list(csv.DictReader(open(CSV)))
    if args.limit:
        rows = rows[:args.limit]
    sem = asyncio.Semaphore(4)
    out = []
    t0 = time.time()

    async def one(r):
        p = parse_header(r["header"])
        key = "t1n" if norm(r["team"]) == p["t1n"] else "t2n"
        s = lookup.search(r["header"], kb)
        team = s["teams"][key]["best"] or {}
        async with sem:
            try:
                res = await asyncio.wait_for(find_rosters(r["team"], p["date"], dict(team=team, kb=kb, comp=p['comp'], fallback=not args.no_fallback)), 240)
            except Exception as e:
                res = []
                print("  error", r["team"], type(e).__name__, str(e)[:80], flush=True)
        best = res[0] if res else {}
        analyst = first_url(r["analyst"] or "") or ""
        fam = family(analyst)
        row = dict(team=r["team"], date=p["date"], matched=team.get("name", ""), before=r["best_status"], before_url=r["best_url"],
                   status=best.get("status", "none"), source=best.get("source", ""), url=best.get("url", ""), players=len(best.get("players") or []),
                   season=best.get("season"), age_fit=best.get("age_fit"), note=best.get("note", ""),
                   sources=[f"{x['status']}|{x['source']}|{len(x['players'])}|{x['url']}" for x in res],
                   analyst=analyst, analyst_family=fam,
                   same_page=any(same_page(analyst, x["url"]) for x in res) if analyst and fam not in ("screenshot",) else None,
                   same_source=(best.get("source") == fam) if best and fam not in ("", "screenshot", "other site") else None)
        row["site_team"], row["league"] = best.get("team"), best.get("league")
        # precision check for teams found by name: players shared with the team's EP roster (EP id from Club Data)
        ep_ref = next((x for x in res if x["source"] == "EliteProspects" and x.get("match") == 1.0 and len(x["players"]) >= 10), None)
        if best and ep_ref and best["source"] != "EliteProspects":
            a = {pnorm(x["name"]) for x in ep_ref["players"]}
            b = [pnorm(x["name"]) for x in best["players"]]
            row["cross_overlap"] = round(sum(1 for x in b if x in a) / max(1, len(b)), 2)
        m = EP_TEAM.search(analyst)
        if m and best.get("players"):
            ep = await ep_roster(m.group(1), p["date"])
            if ep and ep["players"]:
                a = {pnorm(x["name"]) for x in ep["players"]}
                b = [pnorm(x["name"]) for x in best["players"]]
                row["player_overlap"] = round(sum(1 for x in b if x in a) / max(1, len(b)), 2)
                row["analyst_ep_players"] = len(ep["players"])
        out.append(row)
        if len(out) % 20 == 0:
            print(f"  {len(out)}/{len(rows)} teams, {time.time() - t0:.0f}s", flush=True)

    await asyncio.gather(*[one(r) for r in rows])
    n = len(out)
    before = Counter(o["before"] for o in out)
    after = Counter(o["status"] for o in out)
    b_ok = before["verified"]
    b_ok2 = before["verified"] + before["probable"]
    a_ok = after["verified"]
    a_ok2 = after["verified"] + after["probable"]
    print(f"\n=== CURRENT ROSTERS ({n} teams, {time.time() - t0:.0f}s, fallback {'off' if args.no_fallback else 'on'}) ===")
    print(f"before (rosters.py):  verified {b_ok}/{n} ({b_ok / n:.0%}), verified or probable {b_ok2}/{n} ({b_ok2 / n:.0%})")
    print(f"after (roster_sources): verified {a_ok}/{n} ({a_ok / n:.0%}), verified or probable {a_ok2}/{n} ({a_ok2 / n:.0%})")
    print(f"  best status after: {dict(after)}")
    print(f"  source of verified rosters: {dict(Counter(o['source'] for o in out if o['status'] == 'verified'))}")
    print(f"  teams blocked before (EliteProspects) now verified: "
          f"{sum(1 for o in out if o['before'] == 'blocked' and o['status'] == 'verified')}/{before['blocked']}")
    print(f"  still without a verified roster: {n - a_ok} -> {dict(Counter(o['status'] for o in out if o['status'] != 'verified'))}")
    sp = [o for o in out if o["same_page"] is not None]
    ss = [o for o in out if o["same_source"] is not None]
    print(f"\nanalyst's roster page among the tool's pages: {sum(o['same_page'] for o in sp)}/{len(sp)} "
          f"(comparable links: {dict(Counter(o['analyst_family'] for o in sp))})")
    print(f"tool's best roster from the analyst's site: {sum(o['same_source'] for o in ss)}/{len(ss)}")
    ov = [o for o in out if "player_overlap" in o]
    if ov:
        good = sum(1 for o in ov if o["player_overlap"] >= 0.5)
        print(f"analyst linked EP: tool's best roster shares >=50% of players with that EP 2026-27 roster in {good}/{len(ov)} "
              f"(median overlap {sorted(o['player_overlap'] for o in ov)[len(ov) // 2]:.0%})")
    co = [o for o in out if "cross_overlap" in o]
    if co:
        print(f"cross-check, best roster found by name vs the team's EP 2026-27 roster (EP id from Club Data): "
              f">=50% shared players in {sum(1 for o in co if o['cross_overlap'] >= 0.5)}/{len(co)}, "
              f"<20% in {sum(1 for o in co if o['cross_overlap'] < 0.2)} ({', '.join(o['team'] for o in co if o['cross_overlap'] < 0.2)})")
    print(f"analyst links by site: {dict(Counter(o['analyst_family'] or 'none' for o in out))}")
    OUT.write_text(json.dumps(dict(run=time.strftime("%Y-%m-%d %H:%M"), fallback=not args.no_fallback, rows=out), indent=1))
    print(f"rows written to {OUT}")


if __name__ == "__main__":
    asyncio.run(main())

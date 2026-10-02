"""Replay games from a Progress sheet export through the tool and score it.

The sheet is the answer key: for each game it holds the link an analyst found.

    .venv/bin/python -m backend.replay                 # phase 1 only, all games
    .venv/bin/python -m backend.replay --auto           # plus adapters on the covered subset
    .venv/bin/python -m backend.replay --auto --sample 40 --random 80

Writes tests/replay_<timestamp>.csv and prints a summary.
"""
from __future__ import annotations
import argparse, asyncio, csv, datetime, json, random, re, sys, time
from collections import Counter
from pathlib import Path
import openpyxl

from . import lookup
from .lookup import parse_header, first_url, domain

ROOT = Path(__file__).resolve().parent.parent
PROGRESS = ROOT / "data" / "progress.xlsx"
OUT = ROOT / "tests"
SCREENSHOT = {"prnt.sc", "ibb.co", "skrinshoter.ru", "drive.google.com", "upload-only-hudlvid.s3.amazonaws.com"}
COVERED = {"stats.swehockey.se": "swehockey", "chl.ca": "HockeyTech feed", "ushl.com": "HockeyTech feed",
           "theahl.com": "HockeyTech feed", "lscluster.hockeytech.com": "HockeyTech feed", "gamesheetstats.com": "GameSheet",
           "sihf.ch": "sihf", "tulospalvelu.leijonat.fi": "leijonat", "live.hockey.no": "hockey.no",
           "ceskyhokej.cz": "ceskyhokej", "deb-online.live": "deb-online"}


def canonical(url: str):
    """(system, game id) for a report link, so the same game on different sites compares equal."""
    if not url:
        return None
    from .adapters.hockeytech import SITES
    d = domain(url)
    pats = [
        ("hockeytech", r"lscluster\.hockeytech\.com/.*game_id=(\d+)"),
        ("hockeytech", r"chl\.ca/.*gamecentre/(\d+)"),
        ("hockeytech", r"ushl\.com/ht/#/game-summary/(\d+)"),
        ("leijonat", r"leijonat\.fi/.*gameid=(\d+)"),
        ("leijonat", r"(?:mestis|liiga)\.fi/.*?/(27\d{5})(?:/|$|\?)"),
        ("swehockey", r"swehockey\.se/Game/(?:Events|LineUps|Reports)/(\d+)"),
        ("sihf", r"sihf\.ch/.*game-center/game/(\d+)"),
        ("hockeyno", r"hockey\.no/.*matchId=(\d+)"),
        ("ceskyhokej", r"ceskyhokej\.cz/game/detail/(\d+)"),
        ("hockeydata", r"deb-online\.live/.*gameId=([0-9a-f-]{36})"),
        ("gamesheet", r"gamesheetstats\.com/seasons/\d+/games/(\d+)"),
        ("khl", r"(?:online\.khl\.ru/online/|khl\.ru/game/\d+/)(\d+)"),
        ("khl", r"(?:text\.mhl\.khl\.ru/|mhl\.khl\.ru/game/\d+/)(\d+)"),
        ("khl", r"(?:online\.vhlru\.ru/online/|vhlru\.ru/.*idgame=)(\d+)"),
        ("nhl", r"nhl\.com/.*gamecenter/(?:.*/)?(20\d{8})"),
        ("sportsadmin", r"sportsadmin\.dk/.*GameId=(\d+)"),
        ("hockeyslovakia", r"hockeyslovakia\.sk/.*/match/(\d+)"),
        ("del2", r"del-2\.org/spiel/[^_]*_(\d+)"),
        ("ligasy", r"ligasy\.kz/\w+/game/(\d+)-"),
        ("hockeyby", r"hockey\.by/gamecenter/(\d+)"),
        ("chl-europe", r"chl\.hockey/.*?/game/(?:[^/]*?-)?(\d+)"),
        ("icehl", r"ice\.hockey/.*gameId=(\d+)"),
        ("icehl", r"eishockey\.at/.*icehl-(\d+)"),
        ("metalligaen", r"metalligaen\.dk/.*gameId=(\d+)"),
        ("hockeydata-at", r"eishockey\.at/game-center/[^/]+/spiel/(\d+)"),
        ("hockeydata", r"alps\.hockey/.*gameId=([0-9a-f-]{36})"),
        ("hockeyfrance", r"hockeyfrance\.com/competitions/rencontre/(\d+)"),
        ("onlajny", r"onlajny\.com/match/.*id/(\d+)"),
        ("tts-blackbear", r"stats\.blackbear\.timetoscore\.com/.*game_id=(\d+)"),
        ("tts-blackbear", r"(?:atlantichockeyfederation|tier1hockeyfederation|nghlhockey)\.com/.*[?&]game=(\d+)"),
        ("tts-usphl", r"stats\.usphl\.timetoscore\.com/.*game_id=(\d+)"),
        ("tts-usphl", r"usphl\.com/.*game_id=(\d+)"),
        ("tts-blackbear", r"atlanticgirlshockeyfederation\.com/.*[?&]game=(\d+)"),
        ("tts-caha", r"stats\.caha\.timetoscore\.com/.*game_id=(\d+)"),
        ("tts-asec", r"stats\.asec\.timetoscore\.com/.*game_id=(\d+)"),
        ("tts-cna", r"stats\.cna\.timetoscore\.com/.*game_id=(\d+)"),
    ]
    for system, pat in pats:
        m = re.search(pat, url, re.I)
        if m:
            return system, m.group(1)
    if d in SITES:
        m = re.search(r"/stats/game-center/(\d+)", url)
        if m:
            return "hockeytech", m.group(1)
    gid = game_id(url)                   # any other site: same site and same game number
    if gid:
        return d, gid
    m = re.search(r"/(\d{4,})/?(?:[?#]|$)", url)
    if m:
        return d, m.group(1)
    return None


def game_id(url: str) -> str | None:
    if not url:
        return None
    for pat in (r"Game/Events/(\d+)", r"game_id=(\d+)", r"gamecentre/(\d+)", r"game-summary/(\d+)", r"game-center/game/(\d+)", r"game-center/(\d+)",
                r"/games/(\d+)", r"gameid=(\d+)", r"matchId=(\d+)", r"game/detail/(\d+)", r"gameId=([0-9a-f-]{8,})"):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def load_games(start: str, end: str):
    wb = openpyxl.load_workbook(PROGRESS, read_only=True, data_only=True)
    games = []
    for r in wb["Progress"].iter_rows(values_only=True):
        h = r[2]
        if not (isinstance(h, str) and h.startswith("[")):
            continue
        p = parse_header(h)
        if not p or not (start <= p["date"] <= end):
            continue
        games.append(dict(header=h.strip(), parsed=p, status=r[7], link=first_url(r[8]), r1=r[10], r2=r[12],
                          analyst=r[4], expected_domain=domain(first_url(r[8])), expected_id=game_id(first_url(r[8]) or "")))
    return games


def phase1(g, kb):
    r = lookup.search(g["header"], kb)
    p = r["parsed"]
    t1, t2 = r["teams"]["t1n"], r["teams"]["t2n"]
    links = [l for s in r["steps"] for l in s["links"]]
    doms = {l["domain"] for l in links if l["kind"] not in ("search", "manual")}
    doms_no_hist = {l["domain"] for l in links if l["kind"] not in ("search", "manual", "history")}
    exp = g["expected_domain"]
    row = dict(header=g["header"], date=p["date"], comp=p["comp"], status=g["status"], analyst=g["analyst"],
               expected_domain=exp, expected_id=g["expected_id"],
               t1_match="exact" if t1["best"] and t1["best"]["exact"] else "fuzzy" if t1["best"] else "blocked" if t1["candidates"] else "none",
               t2_match="exact" if t2["best"] and t2["best"]["exact"] else "fuzzy" if t2["best"] else "blocked" if t2["candidates"] else "none",
               comp_match="yes" if r["tournament"] else "friendly" if r["friendly"] else "no",
               outcome_p1=r["outcome"],
               source_hit=("n/a" if not exp or exp in SCREENSHOT else "yes" if exp in doms else "no"),
               source_hit_no_history=("n/a" if not exp or exp in SCREENSHOT else "yes" if exp in doms_no_hist else "no"),
               n_links=len(links))
    return row, r


def _corrections():
    try:
        d = json.loads((OUT / "sheet_corrections.json").read_text())
        return {k: v for k, v in d.items() if not k.startswith("_")}
    except Exception:
        return {}


async def phase2(g, row, kb, sem):
    async with sem:
        t0 = time.time()
        r = await lookup.search_full(g["header"], kb)
    a = r.get("auto") or {}
    best = a.get("best")
    row.update(auto_verdict=a.get("verdict"), auto_seconds=a.get("seconds"), auto_n=len(a.get("candidates") or []),
               auto_url=best["url"] if best else "", auto_conf=best["confidence"] if best else "",
               auto_adapters=";".join(f"{x['name']}:{'ok' if x['ok'] else x.get('error')}" for x in a.get("adapters", [])),
               outcome_p2=r["outcome"])
    exp = canonical(g["link"])
    row["expected_system"] = exp[0] if exp else ""
    corr = _corrections()
    if a.get("verdict") == "found":
        got = canonical(best["url"])
        if exp and got == exp:
            row["auto_result"] = "correct"
        elif exp and got and got[0] == exp[0] and (g["parsed"].get("match_id") or "") in corr:
            row["auto_result"] = "sheet error"
            row["note"] = corr[g["parsed"]["match_id"]]
        elif exp and got and got[0] == exp[0]:
            row["auto_result"] = "WRONG"
        elif exp:
            row["auto_result"] = "found elsewhere"
        else:
            row["auto_result"] = "found (unverified)"   # analyst link is a screenshot, blank or an unmapped site
    elif a.get("verdict") == "check":
        row["auto_result"] = "candidates" + ("" if exp else " (unverified)")
    else:
        row["auto_result"] = "missed" if exp else "none"
    return row


def summarize(rows, auto):
    n = len(rows)
    c = Counter
    print(f"\n=== PHASE 1 on {n} games ===")
    both = sum(r["t1_match"] in ("exact", "fuzzy") and r["t2_match"] in ("exact", "fuzzy") for r in rows)
    print(f"both teams matched: {both} ({both/n:.0%})  | team1 {dict(c(r['t1_match'] for r in rows))}  | team2 {dict(c(r['t2_match'] for r in rows))}")
    print(f"competition: {dict(c(r['comp_match'] for r in rows))}")
    print(f"outcome: {dict(c(r['outcome_p1'] for r in rows))}")
    sh = [r for r in rows if r["source_hit"] != "n/a"]
    print(f"analyst's site is in the tool's links: {sum(r['source_hit']=='yes' for r in sh)}/{len(sh)} ({sum(r['source_hit']=='yes' for r in sh)/max(1,len(sh)):.0%})")
    print(f"  same, excluding history hints learned from this sheet: {sum(r['source_hit_no_history']=='yes' for r in sh)}/{len(sh)} ({sum(r['source_hit_no_history']=='yes' for r in sh)/max(1,len(sh)):.0%})")
    miss = c(r["expected_domain"] for r in sh if r["source_hit_no_history"] == "no")
    print(f"  sites most often missing from Club Data links: {miss.most_common(8)}")
    if auto:
        a = [r for r in rows if "auto_result" in r]
        print(f"\n=== AUTOMATIC SEARCH on {len(a)} games ===")
        res = c(r['auto_result'] for r in a)
        print(f"results: {dict(res)}")
        found = res["correct"] + res["WRONG"] + res["sheet error"] + res["found elsewhere"] + res["found (unverified)"]
        print(f"HEADLINE resolved automatically: {found}/{len(a)} ({found/max(1,len(a)):.0%}) | report or candidates: {found + res['candidates'] + res['candidates (unverified)']}/{len(a)} ({(found + res['candidates'] + res['candidates (unverified)'])/max(1,len(a)):.0%})")
        ver = res["correct"] + res["WRONG"]
        print(f"verified precision: {res['correct']}/{ver} correct, {res['WRONG']} wrong ({res['WRONG']/max(1,ver):.1%}); sheet errors set aside with evidence: {res['sheet error']}")
        for sysname in sorted({r['expected_system'] for r in a if r.get('expected_system')}):
            x = [r for r in a if r.get("expected_system") == sysname]
            print(f"  {sysname:16s} n={len(x):4d}  {dict(c(r['auto_result'] for r in x))}")
        wrong = [r for r in a if r["auto_result"] == "WRONG"]
        secs = [r["auto_seconds"] for r in a if isinstance(r.get("auto_seconds"), (int, float))]
        if secs:
            secs.sort(); print(f"seconds per game: median {secs[len(secs)//2]}, p90 {secs[int(len(secs)*.9)]}")
        for r in wrong[:10]:
            print("  WRONG:", r["header"][:70], "| expected", r["expected_id"], "| got", r["auto_url"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-08-01")
    ap.add_argument("--end", default=datetime.date.today().isoformat())
    ap.add_argument("--auto", action="store_true", help="also run the adapters")
    ap.add_argument("--sample", type=int, default=30, help="GameSheet games to include (browser, slow)")
    ap.add_argument("--random", type=int, default=60, help="random uncovered games, to catch false positives")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--all", action="store_true", help="run the automatic search on every game")
    ap.add_argument("--no-gamesheet", action="store_true", help="leave GameSheet out (it loads one page every 6 s)")
    ap.add_argument("--gamesheet-only", action="store_true", help="only the GameSheet sample")
    ap.add_argument("--seed", type=int, default=7, help="sampling seed; change it for a fresh sample")
    ap.add_argument("--system", default="", help="only games whose sheet link is on this system (prefix, e.g. tts-)")
    ap.add_argument("--comp", default="", help="only games whose competition matches this regex")
    args = ap.parse_args()
    kb = lookup.load_kb()
    games = load_games(args.start, args.end)
    if args.system:
        games = [g for g in games if (canonical(g["link"]) or ("",))[0].startswith(args.system)]
    if args.comp:
        games = [g for g in games if re.search(args.comp, g["parsed"]["comp"], re.I)]
    if args.limit:
        games = games[:args.limit]
    print(f"{len(games)} games from {args.start} to {args.end}")
    rows = []
    for g in games:
        row, _ = phase1(g, kb)
        rows.append(row)
    lookup.CHECK_ROSTERS = False
    lookup.USE_MEMORY = False
    from . import adapters as _adp
    _adp.WEB_SEARCH = False
    if args.no_gamesheet:
        from . import adapters as _ad
        _ad.ADAPTERS[:] = [x for x in _ad.ADAPTERS if x.name != "GameSheet"]
    if args.system or args.comp:
        args.auto = True
    if args.auto:
        random.seed(args.seed)
        idx_by_dom = {}
        for i, g in enumerate(games):
            idx_by_dom.setdefault(g["expected_domain"], []).append(i)
        chosen = []
        for d in COVERED:
            if d != "gamesheetstats.com":
                chosen += idx_by_dom.get(d, [])
        gs = idx_by_dom.get("gamesheetstats.com", [])
        chosen += random.sample(gs, min(args.sample, len(gs)))
        others = [i for i, g in enumerate(games) if g["expected_domain"] not in COVERED]
        chosen += random.sample(others, min(args.random, len(others)))
        chosen = sorted(set(chosen))
        if args.all:
            chosen = list(range(len(games)))
        if args.gamesheet_only:
            chosen = sorted(set(random.sample(gs, min(args.sample, len(gs)))))
        if args.system or args.comp:
            chosen = list(range(len(games)))
        print(f"phase 2 on {len(chosen)} games (covered sites + {args.sample} GameSheet + {args.random} random)")

        async def run():
            # GameSheet loads one page at a time, so its games run one by one after the rest
            gs_idx = [i for i in chosen if games[i]["expected_domain"] == "gamesheetstats.com"]
            rest = [i for i in chosen if i not in gs_idx]
            sem = asyncio.Semaphore(4)
            await asyncio.gather(*[phase2(games[i], rows[i], kb, sem) for i in rest])
            one = asyncio.Semaphore(1)
            await asyncio.gather(*[phase2(games[i], rows[i], kb, one) for i in gs_idx])
        asyncio.run(run())
    OUT.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    path = OUT / f"replay_{stamp}.csv"
    keys = sorted({k for r in rows for k in r}, key=lambda k: (k not in ("header", "date", "comp"), k))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    summarize(rows, args.auto)
    print(f"\nrows written to {path}")


if __name__ == "__main__":
    main()

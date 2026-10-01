"""Probe the GameSheet JSON API against the sheet's GameSheet games (2026-08-01 on).

For every replay row whose expected_domain is gamesheetstats.com, the true link comes from
data/progress.xlsx (col H/I, matched by header; the replay CSV holds only the game id).
Checks:
  1. season   : the true game is in season_games(true_sid), and its teams (shared scorer) and date match the header
  2. firestore: the true game is in games_by_date(header UTC day +-1), the cross-season list by date
  3. lookup   : picking the best candidate from ALL games of those days (no season id known) gives the true game
  4. index    : (--index) time to fetch every game of every current season, and whether the true game is in it

    .venv/bin/python -m tests.probe_gamesheet_api                 # checks 1-3
    .venv/bin/python -m tests.probe_gamesheet_api --index         # also build data/gamesheet_games.json and check 4
"""
from __future__ import annotations
import argparse, asyncio, csv, datetime, json, re, sys, time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend import lookup, gamesheet_api as api          # noqa: E402
from backend.adapters.base import score_candidate, strong, header_utc, days_apart, _words   # noqa: E402

REPLAY = ROOT / "tests" / "replay_20260925_0017.csv"
LINK = re.compile(r"gamesheetstats\.com/seasons/(\d+)/games/(\d+)")


def sheet_links():
    import openpyxl
    wb = openpyxl.load_workbook(ROOT / "data" / "progress.xlsx", read_only=True, data_only=True)
    out = {}
    for r in wb["Progress"].iter_rows(values_only=True):
        for v in r[7:9] if len(r) > 8 else []:
            m = LINK.search(str(v or ""))
            if m and r[2]:
                out[str(r[2]).strip()] = (m.group(1), m.group(2))
    return out


def cases():
    links = sheet_links()
    out = []
    for r in csv.DictReader(open(REPLAY)):
        if r["expected_domain"] != "gamesheetstats.com" or r["date"] < "2026-08-01":
            continue
        p = lookup.parse_header(r["header"])
        lk = links.get(r["header"].strip())
        if not p or not lk:
            out.append(dict(header=r["header"], parsed=p, sid=None, gid=r["expected_id"] or None))
            continue
        out.append(dict(header=r["header"], parsed=p, sid=lk[0], gid=lk[1]))
    return out


def cand_of(g, parsed):
    c = {k: v for k, v in g.items() if k != "_w"}
    c.update(source="GameSheet", league=g.get("season_name") or "")
    return score_candidate(c, parsed, c["league"])


def words(name):
    return {w for w in _words(name) if len(w) >= 3 and not w.isdigit()}


def plausible(g, w1, w2):
    """Cheap prefilter before the (slow) shared scorer: a header club word appears in either site team."""
    gw = g.get("_w")
    if gw is None:
        gw = g["_w"] = words(g["home"]) | words(g["away"])
    return bool(gw & w1) or bool(gw & w2)


def utc_days(parsed):
    hu = header_utc(parsed)
    d = hu.date() if hu else datetime.date.fromisoformat(parsed["date"])
    return [(d + datetime.timedelta(days=k)).isoformat() for k in (-1, 0, 1)]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", action="store_true", help="build the full current-season index and time it")
    ap.add_argument("--no-firestore", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    cs = cases()
    if a.limit:
        cs = cs[:a.limit]
    print(f"{len(cs)} GameSheet games from 2026-08-01 ({sum(1 for c in cs if c['sid'])} with a sheet link)")
    kb_sids = set((lookup.load_kb().get("gamesheet_seasons") or {}).keys())

    # 1. season_games(true_sid)
    t = time.time()
    sids = sorted({c["sid"] for c in cs if c["sid"]}, key=int)
    per = await api.all_games(sids)
    t_season = time.time() - t
    res = Counter()
    fails = []
    for c in cs:
        if not c["sid"]:
            res["no link"] += 1
            continue
        res["sid in kb"] += c["sid"] in kb_sids
        gs = per.get(c["sid"])
        if gs is None:
            res["season unreadable"] += 1
            fails.append((c, "season unreadable"))
            continue
        g = next((g for g in gs if g["game_id"] == c["gid"]), None)
        if not g:
            res["game not in season"] += 1
            fails.append((c, "game not in season"))
            continue
        res["game in season"] += 1
        cand = cand_of(g, c["parsed"])
        dd = days_apart(c["parsed"]["date"], g["date"])
        hu = header_utc(c["parsed"])
        utc_ok = bool(hu and g["start_utc"] and abs((datetime.datetime.fromisoformat(g["start_utc"]) - hu).total_seconds()) <= 3600)
        res["teams ok (>=0.6 both)"] += min(cand["team_scores"]) >= 0.6
        res["strong (scorer)"] += strong(cand)
        res["date ok (0-1 day)"] += dd in (0, 1)
        res["UTC start within 1h of header-3h"] += utc_ok
        if not strong(cand):
            fails.append((c, f"weak {cand['team_scores']} {cand['confidence']}: {g['away']} @ {g['home']} {g['date']}"))
    print(f"\n1. season_games(true sid): {len(sids)} seasons in {t_season:.1f}s")
    for k, v in res.items():
        print(f"   {k}: {v}")

    # 2 + 3. Firestore, by date, across seasons
    if not a.no_firestore:
        t = time.time()
        days = sorted({d for c in cs for d in utc_days(c["parsed"])})
        by_day = {}
        for d in days:
            by_day[d] = await api.games_by_date(d)
        t_fs = time.time() - t
        n_docs = sum(len(v) for v in by_day.values())
        print(f"\n2. games_by_date: {len(days)} UTC days, {n_docs} games, {t_fs:.0f}s ({t_fs / max(1, len(days)):.1f}s per day)")
        r2 = Counter()
        for c in cs:
            if not c["gid"]:
                continue
            pool = [g for d in utc_days(c["parsed"]) for g in by_day.get(d, [])]
            ids = {g["game_id"] for g in pool}
            r2["true game in firestore days"] += c["gid"] in ids
            # 3. lookup with no season known: best strong candidate among all games of the 3 days
            # the header's Moscow time pins the game: rank strong candidates by distance to header UTC, then score
            best, n_strong = None, 0
            w1, w2 = words(c["parsed"]["t1"]), words(c["parsed"]["t2"])
            hu = header_utc(c["parsed"])

            def rank(cd):
                off = abs((datetime.datetime.fromisoformat(cd["start_utc"]) - hu).total_seconds()) / 3600 if hu and cd.get("start_utc") else 12
                return (0 if off <= 1.5 else 1 if off <= 4 else 2, -cd["confidence"], -sum(cd["team_scores"]), off)
            for g in pool:
                if not plausible(g, w1, w2):
                    continue
                cand = cand_of(g, c["parsed"])
                if strong(cand):
                    n_strong += 1
                    if best is None or rank(cand) < rank(best):
                        best = cand
            if best is None:
                r2["lookup: none"] += 1
            elif best["game_id"] == c["gid"]:
                r2["lookup: correct"] += 1
                r2["lookup: correct and unique strong"] += n_strong == 1
            else:
                r2["lookup: WRONG"] += 1
                if r2["lookup: WRONG"] <= 12:
                    tg = next((g for g in pool if g["game_id"] == c["gid"]), None)
                    tc = cand_of(tg, c["parsed"]) if tg else None
                    print(f"   WRONG {c['header'][:110]}  (header UTC {hu})\n         got  {best['away']} @ {best['home']} {best['start_utc']}Z {best['team_scores']} {best['url']}"
                          + (f"\n         true {tg['away']} @ {tg['home']} {tg['start_utc']}Z {tc['team_scores']} {tc['confidence']} {tg['url']}" if tg else f"\n         true {c['sid']}/{c['gid']} not in the Firestore days"))
        for k, v in sorted(r2.items()):
            print(f"   {k}: {v}")

    # 4. full index
    if a.index:
        idx = await api.build_index()
        ids = {g["game_id"] for g in idx["games"]}
        in_idx = sum(1 for c in cs if c["gid"] in ids)
        sids_idx = set(idx["seasons"])
        print(f"\n4. index: {len(idx['seasons'])} seasons, {len(idx['games'])} games, seasons {idx['seconds']['seasons']}s + games {idx['seconds']['games']}s, "
              f"{len(idx['failed'])} seasons failed")
        print(f"   true game in index: {in_idx}/{len(cs)}; true season in index: {sum(1 for c in cs if c['sid'] in sids_idx)}/{len(cs)}")
        miss = Counter(c["sid"] for c in cs if c["sid"] and c["sid"] not in sids_idx)
        if miss:
            print("   seasons missing from index:", dict(miss.most_common(10)))

    if fails:
        print("\nfailures (first 15):")
        for c, why in fails[:15]:
            print(f"   {c['header'][:95]}  [{c['sid']}/{c['gid']}] {why}")
    print("\nrequests:", api.stats)
    await api.close()


if __name__ == "__main__":
    asyncio.run(main())

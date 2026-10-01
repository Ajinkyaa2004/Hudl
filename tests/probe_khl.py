"""Probe the KHL-platform adapter (KHL, MHL, VHL, their friendlies) and the hockey.by adapter on
the Progress sheet's 2026-27 games (from 2026-08-01).

    .venv/bin/python -m tests.probe_khl            # both adapters
    .venv/bin/python -m tests.probe_khl khl        # one adapter (khl or hockeyby)
    .venv/bin/python -m tests.probe_khl -v         # print every game

A game is in the set when the adapter's applies() accepts it or the analyst's link is on its site.
correct = strong match whose game id equals the analyst's; WRONG = strong match, different id;
found (no key) = strong match, analyst link missing or not a game link; check = candidates, none strong.
The KHL, MHL and VHL sites share one game-id space, so ids are compared across their domains."""
from __future__ import annotations
import asyncio, re, sys, time
from pathlib import Path
import openpyxl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend import lookup  # noqa: E402
from backend.adapters.base import strong  # noqa: E402
from backend.adapters.khl import Khl  # noqa: E402
from backend.adapters.hockeyby import HockeyBy  # noqa: E402
from backend.replay import canonical  # noqa: E402

ID_RE = {
    "khl": re.compile(r"(?:online\.khl\.ru/online/|khl\.ru/game/\d+/|text\.mhl\.khl\.ru/|online\.vhlru\.ru/online/|vhlru\.ru/.*idgame=)(\d+)"),
    "hockeyby": re.compile(r"hockey\.by/gamecenter/(\d+)"),
}
SITE_RE = {"khl": re.compile(r"khl\.ru|vhlru\.ru"), "hockeyby": re.compile(r"hockey\.by")}
START = "2026-08-01"


def sheet_games():
    wb = openpyxl.load_workbook(ROOT / "data" / "progress.xlsx", read_only=True, data_only=True)
    seen = {}
    for row in wb["Progress"].iter_rows(values_only=True):
        h = str(row[2] or "").strip()
        if not h.startswith("["):
            continue
        try:
            p = lookup.parse_header(h)
        except Exception:
            continue
        if not p or not p.get("date") or p["date"] < START:
            continue
        link = " ".join(str(x) for x in row[7:9] if x)
        key = p.get("match_id") or h
        if key not in seen or (link and not seen[key][1]):
            seen[key] = (h, link)
    return list(seen.values())


async def run(names, verbose):
    kb = lookup.load_kb()
    adapters = [a for a in (Khl(), HockeyBy()) if not names or a.name in names]
    games = sheet_games()
    tms = {}

    def teams(h, p):
        # lookup.search is ~0.1 s; only friendlies and cups need it (the site check in applies())
        if not re.search(r"friendl|cup|tournament", p["comp"], re.I):
            return {}
        if h not in tms:
            tms[h] = lookup.search(h, kb)["teams"]
        return tms[h]
    for a in adapters:
        stats = dict(same_system=0, games=0, correct=0, wrong=0, found_nokey=0, check=0, missed=0, keyed=0)
        wrongs, misses = [], []
        t0 = time.time()
        for h, link in games:
            p = lookup.parse_header(h)
            tm = teams(h, p)
            try:
                ok = a.applies(p, tm, kb)
            except Exception:
                ok = False
            on_site = bool(SITE_RE[a.name].search(link or ""))
            if not ok and not on_site:
                continue
            stats["games"] += 1
            m = ID_RE[a.name].search(link or "")
            exp = m.group(1) if m else None
            stats["keyed"] += bool(exp)
            try:
                cands = await a.find(p, tm, kb) if ok else []
            except Exception as e:
                cands = []
                print("  ERROR", h[:80], e)
            best = sorted(cands, key=lambda c: -c["confidence"])
            st = [c for c in best if strong(c)]
            got = ID_RE[a.name].search(st[0]["url"]).group(1) if st and ID_RE[a.name].search(st[0]["url"]) else None
            if st and exp:
                res = "correct" if got == exp else "WRONG"
            elif st:
                res = "found (no key)"
            elif best:
                res = "check"
            else:
                res = "missed"
            stats[{"correct": "correct", "WRONG": "wrong", "found (no key)": "found_nokey", "check": "check", "missed": "missed"}[res]] += 1
            if res == "correct":
                cg, ce = canonical(st[0]["url"]), canonical(re.search(r"https?://\S+", link).group(0))
                if cg and cg == ce:
                    stats["same_system"] += 1
                elif verbose or cg != ce:
                    print(f"   system label differs: got {cg} expected {ce} | {h[:70]}")
            if res == "WRONG":
                wrongs.append((h, link, st[0]["url"]))
            if res in ("missed", "check") and exp:
                misses.append((h, link, best[0]["url"] if best else None, best[0]["reasons"] if best else None, ok))
            if verbose:
                print(f"  {res:15} {h[:95]}\n  {'':15} expected {link[:80] if link else '-'}\n  {'':15} got      {st[0]['url'] if st else (best[0]['url'] + ' (check)' if best else '-')}")
        print(f"\n== {a.name}: {stats['games']} games ({stats['keyed']} with the analyst's game link), {time.time() - t0:.0f}s")
        print(f"   correct {stats['correct']} / {stats['keyed']} keyed | WRONG {stats['wrong']} | found, no key {stats['found_nokey']} | "
              f"check {stats['check']} | missed {stats['missed']}")
        print(f"   of the correct ones, {stats['same_system']} also equal under backend.replay.canonical() (same system label)")
        for h, link, got in wrongs:
            print("   WRONG:", h[:90], "| expected", link[:70], "| got", got)
        for h, link, got, reasons, ok in misses:
            print("   not found:", h[:90], "| expected", link[:70], "| best", got, reasons, "" if ok else "(applies() is False)")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    asyncio.run(run(args, "-v" in sys.argv))

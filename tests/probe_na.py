"""Shared probe for the North American adapters (ramp, regystra, kreezee, ayhl, digitalshift):
runs one adapter on the Progress sheet's 2026-27 games whose analyst link matches the
adapter's site and prints found / correct / WRONG counts.

    .venv/bin/python -m tests.probe_na            # every NA adapter, summary only
"""
from __future__ import annotations
import asyncio, re, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import lookup                      # noqa: E402
from backend.adapters.base import strong        # noqa: E402

SEASON_START = "2026-08-01"


def sheet_rows(link_re: str):
    """(header, analyst link) from data/progress.xlsx (col C header, col H/I links), games
    from 2026-08-01 whose links match link_re. One row per header id."""
    import openpyxl
    wb = openpyxl.load_workbook(ROOT / "data" / "progress.xlsx", read_only=True)
    ws = wb["Progress"]
    pat = re.compile(link_re, re.I)
    out, seen = [], set()
    for r in ws.iter_rows(min_row=3, values_only=True):
        if not r or len(r) < 9 or not r[2]:
            continue
        header = str(r[2]).strip()
        links = " ".join(str(x) for x in (r[7], r[8]) if x)
        if not pat.search(links):
            continue
        p = lookup.parse_header(header)
        if not p or not p.get("date") or p["date"] < SEASON_START:
            continue
        key = p.get("match_id") or header
        if key in seen:
            continue
        seen.add(key)
        url = next((u for u in re.findall(r"https?://\S+", links) if pat.search(u)), links)
        out.append((header, url))
    return out


def pick(adapter, p, cands):
    """The runner's tie-breaks, in short: one strong candidate, else same orientation, else preferred date."""
    seen, uniq = set(), []
    for c in sorted(cands, key=lambda c: -c["confidence"]):     # the runner drops repeats of one URL
        if c["url"] not in seen:
            seen.add(c["url"])
            uniq.append(c)
    strongs = [c for c in uniq if strong(c)]
    if len(strongs) > 1:
        same_way = [c for c in strongs if "home and away are swapped on the site" not in c["reasons"]]
        strongs = same_way or strongs
    if len(strongs) > 1:
        pd = adapter.preferred_date(p) if hasattr(adapter, "preferred_date") else p["date"]
        pref = [c for c in strongs if c.get("date") == pd]
        strongs = pref if len(pref) == 1 else strongs
    return strongs


async def probe(adapter, link_re: str, same, limit: int | None = None, verbose: bool = True):
    """same(found_url, analyst_url) -> True when both point at the same game;
    None when the analyst link does not name a single game (schedule page), counted apart."""
    kb = lookup.load_kb()
    rows = sheet_rows(link_re)[:limit]
    n = applies = found = correct = wrong = unverified = cand_only = none = 0
    t0 = time.time()
    for header, want in rows:
        n += 1
        p = lookup.parse_header(header)
        tm = lookup.search(header, kb)["teams"]
        try:
            ok = adapter.applies(p, tm, kb)
        except Exception as e:
            ok = False
            print("  applies error", e)
        if not ok:
            if verbose:
                print(f"  NOT APPLIED  {header[:95]}")
            continue
        applies += 1
        try:
            cands = await asyncio.wait_for(adapter.find(p, tm, kb), timeout=getattr(adapter, "budget_s", 60) + 30)
        except Exception as e:
            cands = []
            print(f"  ERROR {type(e).__name__}: {str(e)[:100]}  {header[:80]}")
        strongs = pick(adapter, p, cands)
        if len(strongs) == 1:
            found += 1
            c = strongs[0]
            verdict = same(c["url"], want)
            if verdict is None:
                unverified += 1
                tag = "found?"
            elif verdict:
                correct += 1
                tag = "correct"
            else:
                wrong += 1
                tag = "WRONG"
            if verbose or tag == "WRONG":
                print(f"  {tag:8} {header[:90]}\n           got  {c['url']}  ({c['home']} - {c['away']} {c['date']})\n           want {want}")
        elif cands:
            cand_only += 1
            if verbose:
                b = cands[0]
                print(f"  CHECK    {header[:90]}  ({len(cands)} cands, {len(strongs)} strong; best {b['home']} - {b['away']} {b['date']} conf {b['confidence']} {b['team_scores']})\n           want {want}")
        else:
            none += 1
            if verbose:
                print(f"  NONE     {header[:90]}\n           want {want}")
    print(f"\n[{adapter.name}] sheet games {n}, applies {applies}, found {found} (correct {correct}, WRONG {wrong}, "
          f"unverifiable {unverified}), check-only {cand_only}, nothing {none}   ({time.time() - t0:.0f}s)")
    return dict(n=n, applies=applies, found=found, correct=correct, wrong=wrong, unverified=unverified, check=cand_only, none=none)


def ids(url: str, pattern: str):
    m = re.search(pattern, url or "", re.I)
    return m.group(1).lower() if m else None


if __name__ == "__main__":
    import importlib
    for name in ("ayhl", "kreezee", "regystra", "digitalshift", "ramp"):
        try:
            mod = importlib.import_module(f"tests.probe_{name}")
            asyncio.run(mod.main(verbose=False))
        except Exception as e:
            print(name, "failed:", e)

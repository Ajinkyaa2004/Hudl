"""Check PuckTrace's answers in the search log against the RTT Progress sheet and send the result
to the search-log spreadsheet's 'Accuracy' tab (its Apps Script draws the Dashboard from it).

    .venv/bin/python -m backend.sheet_report            # uses data/progress.xlsx, sends
    .venv/bin/python -m backend.sheet_report --dry-run  # only prints the counts

One row per game (match id) searched: PuckTrace's last answer, the analyst's link, and a verdict.
"""
from __future__ import annotations
import argparse, collections, csv, datetime, io, json, re
from pathlib import Path

from .lookup import first_url, domain
from .replay import canonical, same_game, PROGRESS

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "data" / "sheet_log.json"
SHEET_ID = "1WclJXwbQ6zdBUboSOWw92-PTuRn3w7ycUg2j0oq-ahg"
NO_PAGE = {"prnt.sc", "myhockeyrankings.com", "drive.google.com", "upload-only-hudlvid.s3.amazonaws.com", "docs.google.com"}
V_OK, V_WRONG, V_MISSED = ("Correct (same game as the analyst)", "Wrong (different game from the analyst)",
                           "Missed (a report existed, PuckTrace did not find it)")
V_CANT, V_NONE = ("Cannot check yet (analyst used a screenshot or has no link yet)",
                  "No report anywhere (the analyst found none either)")
V_OTHER = "Found on a different website from the analyst (not compared)"


def _plain(u: str) -> str:
    return re.sub(r"[?#].*$", "", (u or "").strip().lower()).rstrip("/").replace("://www.", "://")


def _same(a: str, b: str) -> bool:
    pa, pb = _plain(a), _plain(b)
    return same_game(canonical(a), canonical(b)) or (bool(pa) and bool(pb) and (pa.startswith(pb) or pb.startswith(pa)))


def _comparable(a: str, b: str) -> bool:
    """Both links on the same system (or site): a different link there is a different game."""
    ca, cb = canonical(a), canonical(b)
    return bool(ca and cb and ca[0] == cb[0]) or domain(a) == domain(b)


def log_rows() -> list[dict]:
    import httpx
    r = httpx.get(f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=0", follow_redirects=True, timeout=90)
    r.raise_for_status()
    return list(csv.DictReader(io.StringIO(r.text)))


def verdicts(log: list[dict], progress: Path = PROGRESS) -> list[list]:
    from . import sheet
    prog = {}
    for r in sheet.rows(progress):
        m = re.match(r"\s*\[(\d+)\]", r["header"])
        if m:
            prog[m.group(1)] = r
    games = collections.OrderedDict()
    for r in log:
        m = re.match(r"\s*\[(\d+)\]", r.get("Header") or "")
        if not m:
            continue
        g = games.setdefault(m.group(1), dict(header=r["Header"], found=None, result="Not found"))
        res = (r.get("Result") or ("Found" if (r.get("Found") or "").strip() == "Yes" else "")).strip()
        if res == "Found" and r.get("Report link"):
            g["found"], g["result"] = r["Report link"], "Found"
        elif g["result"] != "Found":
            g["result"] = "Pick one" if res == "Shortlist" else "Not found"
    today = datetime.date.today().isoformat()
    out = []
    for mid, g in games.items():
        p = prog.get(mid)
        link = first_url(p.get("link")) if p else None
        real = bool(link) and domain(link) not in NO_PAGE
        if g["found"]:
            v = (V_OK if _same(g["found"], link) else V_WRONG if _comparable(g["found"], link) else V_OTHER) if real else V_CANT
        elif real:
            v = V_MISSED
        elif p and (link or str(p.get("status") or "").strip() not in ("", "None", "WFV")):
            v = V_NONE
        else:
            v = V_CANT
        out.append([mid, g["header"], g["result"], g["found"] or "", link or "", v, today])
    return out


def send(rows: list[list]) -> str:
    import httpx
    cfg = json.loads(CFG.read_text())
    r = httpx.post(cfg["url"], json=dict(key=cfg["key"], type="accuracy", rows=rows), follow_redirects=True, timeout=180)
    return r.text[:200]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--progress", default=str(PROGRESS))
    a = ap.parse_args()
    rows = verdicts(log_rows(), Path(a.progress))
    c = collections.Counter(r[5] for r in rows)
    for k, n in c.most_common():
        print(f"{n:5}  {k}")
    ok, wrong = c[V_OK], c[V_WRONG]
    if ok + wrong:
        print(f"accuracy where it can be checked: {100 * ok / (ok + wrong):.1f}% ({ok} of {ok + wrong})")
    for r in rows:
        if r[5] == V_WRONG:
            print("  wrong?", r[0], r[1][:70], "| ours", r[3][:60], "| analyst", r[4][:60])
    if not a.dry_run:
        print("sent:", send(rows))


if __name__ == "__main__":
    main()

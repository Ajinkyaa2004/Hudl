"""Why were headers not found? Joins the PuckTrace search log with the RTT Progress sheet.

    .venv/bin/python tests/tools/analyse_log.py                       # downloads the log, uses data/progress.xlsx
    .venv/bin/python tests/tools/analyse_log.py --log log.csv --progress data/progress.xlsx --out tests/logs/missed.json

Prints: the Found / Pick one / Not found split (as searched), then every never-found game by reason
  - analyst used a screenshot / MyHockeyRankings / file        -> no public report (unsolvable)
  - analyst found nothing (no link, WFV)                        -> no report anywhere
  - analyst's link is on a site that blocks automated readers   -> blocked
  - analyst's link is on a readable site                        -> FIXABLE (written to --out for rerun_missed.py)
Run from the project folder (it imports backend/).
"""
import argparse, collections, csv, io, json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend import sheet
from backend.lookup import first_url, domain

SHEET_ID = "1WclJXwbQ6zdBUboSOWw92-PTuRn3w7ycUg2j0oq-ahg"
NOPAGE = {"prnt.sc", "myhockeyrankings.com", "drive.google.com", "upload-only-hudlvid.s3.amazonaws.com",
          "docs.google.com", "www.dropbox.com", "dropbox.com"}
BLOCKED = {"chl.ca", "scolaire.rseqhockey.com", "senatorsaaashowcase.com", "iihf.com", "page.spordle.com",
           "pdf.play.spordle.com", "dwgha.com", "neajbhl.ca", "winnipegaaa.com", "acac.ab.ca",
           "atlanticuniversitysport.com", "saahl.ca", "ajfhl.ca", "hockeyslovakia.sk", "shl.hockeyslovakia.sk",
           "ersteliga.hu", "juniorprospectshockeyleague.com"}


def result(r):
    x = (r.get("Result") or "").strip()
    return "Found" if x == "Found" or (not x and (r.get("Found") or "").strip() == "Yes") else "Pick one" if x == "Shortlist" else "Not found"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", help="log CSV (default: download from the Google Sheet)")
    ap.add_argument("--progress", default="data/progress.xlsx")
    ap.add_argument("--out", default="tests/logs/missed.json")
    a = ap.parse_args()
    if a.log:
        text = Path(a.log).read_text()
    else:
        import httpx
        text = httpx.get(f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=0", follow_redirects=True, timeout=90).text
    log = [r for r in csv.DictReader(io.StringIO(text)) if r.get("Header")]
    prog = {}
    for r in sheet.rows(a.progress):
        m = re.match(r"\s*\[(\d+)\]", r["header"])
        if m:
            prog[m.group(1)] = r
    n = len(log)
    c = collections.Counter(result(r) for r in log)
    print(f"{n} searches: " + ", ".join(f"{k} {v} ({100 * v / n:.0f}%)" for k, v in c.items()))
    found, games = set(), {}
    for r in log:
        m = re.match(r"\s*\[(\d+)\]", r["Header"])
        k = m.group(1) if m else None
        if not k:
            continue
        if result(r) == "Found":
            found.add(k)
        games.setdefault(k, dict(header=r["Header"], when=r["Time (IST)"], searches=0))["searches"] += 1
    why, fixable = collections.Counter(), {}
    for r in log:  # per SEARCH, as the dashboard counts
        if result(r) != "Not found":
            continue
        m = re.match(r"\s*\[(\d+)\]", r["Header"])
        k = m.group(1) if m else None
        p = prog.get(k)
        link = first_url(p.get("link")) if p else None
        if not p:
            x = "not in the RTT sheet"
        elif not link:
            x = "no report anywhere (analyst found none / WFV)"
        elif domain(link) in NOPAGE:
            x = "no public report (screenshot / MHR / file)"
        elif domain(link) in BLOCKED:
            x = "report on a site that blocks readers"
        else:
            x = "FIXABLE: report on a readable site"
        why[x] += 1
        if x.startswith("FIXABLE") and k not in found:
            fixable[k] = dict(games[k], link=link, status=str(p.get("status")))
    print("\nNot-found SEARCHES by reason:")
    for k, v in why.most_common():
        print(f"  {v:5} {100 * v / n:5.1f}%  {k}")
    print("\nFixable games by website:", collections.Counter(domain(v["link"]) for v in fixable.values()).most_common())
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(fixable, indent=1))
    print(f"\n{len(fixable)} fixable games written to {a.out}")


if __name__ == "__main__":
    main()

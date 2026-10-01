"""Harvest GameSheet seasons: for each season id, read the season games page once
and store the season name, every team name with its team id, and the date range.
Season ids come from links already in the knowledge base and from --ids / --from-progress.

    .venv/bin/python -m backend.harvest_gamesheet --from-progress
"""
from __future__ import annotations
import argparse, asyncio, datetime, re, sys
from collections import Counter
from . import lookup
from .fetch import browser_page

JS = """() => {
  const rows = [...document.querySelectorAll('[role=row]')].map(tr => {
    const a = tr.querySelector('a[href*="/games/"]');
    const cells = [...tr.querySelectorAll('[role=cell]')];
    return {href: a ? a.getAttribute('href') : null,
            cells: cells.map(td => td.innerText.replace(/\\s+/g,' ').trim()),
            teams: cells.map(td => { const t = td.querySelector('a[href*="/teams/"]'); return t ? [t.getAttribute('href'), t.innerText.trim()] : null })};
  }).filter(r => r.href);
  return {title: document.title, rows};
}"""
MAX_DAYS = 8
CURRENT_FROM = "2026-08-01"
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_date(s):
    m = re.search(r"([A-Za-z]{3})[a-z]*\.? (\d{1,2}), (\d{4})", s or "")
    try:
        return datetime.date(int(m.group(3)), MONTHS[m.group(1).lower()[:3]], int(m.group(2))).isoformat() if m else None
    except Exception:
        return None


async def _load(url):
    d = await browser_page(url, wait_ms=5000, js=JS, ttl=0, retries=0)
    await asyncio.sleep(5)   # GameSheet challenges rapid loads
    return d


async def harvest_season(sid: str):
    """Default page (latest day), plus every day of a tournament whose name carries dates:
    the games page shows one day at a time."""
    from .adapters.gamesheet import name_dates
    data = await _load(f"https://gamesheetstats.com/seasons/{sid}/games")
    if not data or not data.get("rows"):
        return None
    nd = name_dates(re.sub(r"^Games \| ", "", data.get("title") or ""))
    if nd and nd[1] < CURRENT_FROM:
        nd = None   # a tournament before this season: no need to walk its days
    if nd:
        d, end = datetime.date.fromisoformat(nd[0]), datetime.date.fromisoformat(nd[1])
        extra = []
        while d <= end and len(extra) < 8:
            extra.append(d.isoformat()); d += datetime.timedelta(days=1)
        # pool play: most teams appear on the first two days; the last day is the default view
        for day in extra[:-1][:MAX_DAYS]:
            more = await _load(f"https://gamesheetstats.com/seasons/{sid}/games?filter%5Bstart_time_from%5D={day}&filter%5Bstart_time_to%5D={day}")
            if more and more.get("rows"):
                data["rows"] += more["rows"]
    teams, dates, games = {}, [], []
    for r in data["rows"]:
        cells = r["cells"]
        d = parse_date(cells[0] if cells else "")
        if d:
            dates.append(d)
        for t in r["teams"]:
            if t:
                m = re.search(r"/teams/(\d+)", t[0])
                if m and t[1]:
                    teams[t[1]] = m.group(1)
        score_i = next((i for i, c in enumerate(cells[1:4], 1) if re.search(r"\d+\s*-\s*\d+", c)), None)
        away = cells[1] if len(cells) > 1 else ""
        home = cells[score_i + 1] if score_i is not None and len(cells) > score_i + 1 else (cells[2] if len(cells) > 2 else "")
        sc = re.search(r"(\d+)\s*-\s*(\d+)", cells[score_i]) if score_i is not None else None
        games.append(dict(date=d, away=away, home=home, score=f"{sc.group(1)}-{sc.group(2)}" if sc else None, href=r["href"]))
    name = re.sub(r"^Games \| ", "", data["title"] or "")
    seen_names = sorted({re.sub(r"\b(\d{1,2}U)\s+\1\b", r"\1", n) for g in games for n in (g["home"], g["away"]) if n})
    teams = teams or {n: None for n in seen_names}
    return dict(name=name, teams=teams, first=min(dates) if dates else None, last=max(dates) if dates else None,
                games=games, harvested=datetime.datetime.now().isoformat(timespec="seconds"))


def season_ids_from_kb(kb):
    ids = set()
    for t in list(kb["teams"].values()) + list(kb["tournaments"].values()):
        for f in ("site", "schedule", "roster", "mhr", "ep"):
            for m in re.finditer(r"gamesheetstats\.com/seasons/(\d+)", t.get(f) or ""):
                ids.add(m.group(1))
    for h in kb.get("history", []):
        for m in re.finditer(r"gamesheetstats\.com/seasons/(\d+)", h.get("protocol") or ""):
            ids.add(m.group(1))
    return ids


def season_ids_from_progress(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    c = Counter()
    for r in wb["Progress"].iter_rows(values_only=True):
        u = lookup.first_url(r[8]) if len(r) > 8 else None
        m = re.search(r"gamesheetstats\.com/seasons/(\d+)", u or "")
        if m:
            c[m.group(1)] += 1
    return c


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", nargs="*", default=[])
    ap.add_argument("--from-progress", action="store_true")
    ap.add_argument("--min-year", type=int, default=2026, help="skip seasons whose last game is before this year")
    ap.add_argument("--refresh", action="store_true", help="re-harvest seasons already on file")
    ap.add_argument("--scan", nargs=2, type=int, metavar=("FROM", "TO"), help="try every season id in a range (GameSheet numbers seasons in sequence)")
    ap.add_argument("--new", type=int, default=0, help="scan this many ids above the highest season on file (daily top-up)")
    ap.add_argument("--max-days", type=int, default=8, help="tournament days to walk per season")
    args = ap.parse_args()
    kb = lookup.load_kb()
    kb.setdefault("gamesheet_seasons", {})
    ids = set(args.ids)
    if not args.from_progress and not args.ids and not args.scan and not args.new:
        ids |= season_ids_from_kb(kb)
    if args.from_progress:
        ids |= set(season_ids_from_progress(lookup.DATA / "progress.xlsx"))
    global MAX_DAYS
    MAX_DAYS = args.max_days
    if args.scan:
        ids |= {str(i) for i in range(args.scan[0], args.scan[1] + 1)}
    if args.new:
        top = max([int(k) for k in kb["gamesheet_seasons"]] or [0])
        ids |= {str(i) for i in range(top + 1, top + 1 + args.new)}
    def stale_fail(i):
        e = kb["gamesheet_seasons"].get(i) or {}
        t = e.get("tried")
        return e.get("failed") and (not t or (datetime.datetime.now() - datetime.datetime.fromisoformat(t)).days >= 1)
    if args.new:   # the daily top-up also retries seasons that were empty or blocked a day ago
        ids |= {k for k in kb["gamesheet_seasons"] if stale_fail(k)}
    todo = sorted((i for i in ids if args.refresh or i not in kb["gamesheet_seasons"] or stale_fail(i)), key=int)
    print(f"{len(ids)} season ids, {len(todo)} to harvest")
    for n, sid in enumerate(todo, 1):
        s = await harvest_season(sid)
        if not s:
            print(f"[{n}/{len(todo)}] {sid}: blocked or empty")
            kb["gamesheet_seasons"].setdefault(sid, dict(name=None, teams={}, first=None, last=None, games=[], harvested=None, failed=True))
        elif not re.search(str(args.min_year) + r"|" + str(args.min_year + 1), s["name"] or "") or re.search(r"20(\d\d)\s*[-/]\s*(20)?(\d\d)", s["name"] or "") and int(re.search(r"20(\d\d)\s*[-/]\s*(20)?(\d\d)", s["name"]).group(1)) < args.min_year % 100:
            print(f"[{n}/{len(todo)}] {sid}: not a {args.min_year} season by name, skipped: {s['name'][:50]}")
            kb["gamesheet_seasons"][sid] = dict(name=s["name"], teams={}, first=None, last=None, harvested=s["harvested"], old=True)
        else:
            s.pop("games", None)  # games are re-read live when searching; keep the file small
            kb["gamesheet_seasons"][sid] = s
            print(f"[{n}/{len(todo)}] {sid}: {len(s['teams'])} teams, {s['first']}..{s['last']}: {s['name'][:60]}")
        lookup.save_kb(kb)
    print("done. seasons on file:", len(kb["gamesheet_seasons"]))


if __name__ == "__main__":
    asyncio.run(main())

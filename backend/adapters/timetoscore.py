"""TimeToScore (stats.*.timetoscore.com): AHF, Tier 1 Hockey Federation (THF), NGHL, AGHF,
USPHL, CAHA/SCAHA/NorCal, and many showcases and tournaments.

Each league's current-season schedule is one public HTML table
(`display-schedule?league=N`), with the away team first. Dates carry no year; the weekday
fixes it. The harvester (`python -m backend.adapters.timetoscore`) loads every league of
every host into the game index; a live search also reads the leagues that match the header.
"""
from __future__ import annotations
import asyncio, datetime, html, json, re, time
from pathlib import Path
from ..fetch import get_text
from .base import Adapter, score_candidate, keep, local_to_utc

HOSTS = {"blackbear": "America/New_York", "usphl": "America/New_York", "caha": "America/Los_Angeles",
         "asec": "America/Los_Angeles", "cna": "America/Los_Angeles"}
SCHED = "https://stats.{host}.timetoscore.com/display-schedule?league={league}"
REPORT = "https://stats.{host}.timetoscore.com/oss-scoresheet?game_id={gid}&mode=display"   # web page; generate-scorecard.php is the PDF
LEAGUES_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "timetoscore_leagues.json"
SEASON_FROM, SEASON_TO = datetime.date(2026, 7, 1), datetime.date(2027, 6, 30)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)

# competition words in headers -> (host, league ids); anything else still goes through the index
COMP_HINTS = [
    (r"\bahf\b|atlantic hockey fed", "blackbear", [4]),
    (r"\bthf\b|tier ?1 hockey fed|\bt1hf\b", "blackbear", [5]),
    (r"\bnghl\b|national girls hockey", "blackbear", [41, 42]),
    (r"\baghf\b", "blackbear", [3]),
    (r"usphl|ncdc", "usphl", [1, 2, 3]),
    (r"\bcaha\b|norcal|california", "caha", [3, 4, 5, 16]),
    (r"\bscaha\b", "caha", [4]),
]


def _cell(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).replace("\xa0", " ").strip()


def season_date(text: str) -> str | None:
    """'Sat Aug 29' -> '2026-08-29': the year whose calendar puts that weekday on that date,
    within the 2026-27 season window."""
    m = re.match(r"(\w{3})\s+(\w{3})\s+(\d{1,2})", text or "")
    if not m:
        return None
    wd, mon, day = m.group(1).lower(), MONTHS.get(m.group(2).lower()), int(m.group(3))
    if not mon:
        return None
    for y in (SEASON_FROM.year, SEASON_TO.year):
        try:
            d = datetime.date(y, mon, day)
        except ValueError:
            continue
        if SEASON_FROM <= d <= SEASON_TO and d.strftime("%a").lower() == wd:
            return d.isoformat()
    return None


def clock(text: str) -> str | None:
    t = (text or "").lower().strip()
    if "noon" in t:
        return "12:00"
    m = re.match(r"(\d{1,2}):(\d{2})\s*([ap])", t)
    if not m:
        return None
    h = int(m.group(1)) % 12 + (12 if m.group(3) == "p" else 0)
    return f"{h:02d}:{m.group(2)}"


def fix_name(name: str) -> str:
    """Site shorthand -> header style: '16AA' -> '16U AA', '18GRY' -> '18U', '14-2' -> '14U'."""
    n = re.sub(r"\s+", " ", name or "").strip()
    n = re.sub(r"\((\d{1,2})u\s?(aaa|aa|a)\)", lambda m: f"{m.group(1)}U {m.group(2).upper()}", n, flags=re.I)   # "(18uaa)"
    n = re.sub(r"\b(\d{1,2})\s?(AAA|AA|A)\b", r"\1U \2", n)
    n = re.sub(r"\b(\d{1,2})(GRY|L|B|P|T|S|N|G|W)\b", r"\1U", n, flags=re.I)
    n = re.sub(r"\b(\d{1,2})-\d\b", r"\1U", n)
    n = re.sub(r"\b(\d{1,2})/(\d{1,2})u\b", r"\2U", n, flags=re.I)
    return n


def parse_schedule(page: str, host: str) -> list[dict]:
    tz = HOSTS.get(host, "America/New_York")
    out, dated = [], 0
    for row in ROW_RE.findall(page or ""):
        cells = CELL_RE.findall(row)
        if len(cells) < 10:
            continue
        c = [_cell(x) for x in cells]
        m = re.search(r"game_id=(\d+)", row) or re.match(r"(\d{3,})", c[0])
        if not m:
            continue
        gid = m.group(1)
        if re.match(r"\w{3}\s+\w{3}\s+\d", c[1]):
            dated += 1
        date = season_date(c[1])
        if not date:
            continue
        hhmm = clock(c[2])
        league, level = c[4], c[5]
        away, home = fix_name(c[6]), fix_name(c[8])
        ga, gh = c[7], c[9]
        score = f"{gh}-{ga}" if gh.isdigit() and ga.isdigit() else None
        out.append(dict(url=REPORT.format(host=host, gid=gid), source="timetoscore", site=f"stats.{host}.timetoscore.com", gid=gid,
                        date=date, time=hhmm, start_utc=local_to_utc(date, hhmm, tz), home=home, away=away,
                        league=f"{league} {fix_name(level)}".strip(), status="final" if score else "scheduled", score=score))
    # dates carry no year, and a weekday+date repeats every few years: a schedule where most
    # rows don't fit 2026-27 is another year's, and the few that fit are coincidences
    if dated and len(out) < 0.8 * dated:
        return []
    return out


def load_leagues() -> list[dict]:
    try:
        return json.loads(LEAGUES_FILE.read_text())
    except Exception:
        return []


class TimeToScore(Adapter):
    name = "timetoscore"
    site = "timetoscore.com"
    budget_s = 40

    def _targets(self, parsed, tm, kb) -> list[tuple[str, int]]:
        c = parsed["comp"].lower()
        out = []
        for pat, host, ids in COMP_HINTS:
            if re.search(pat, c):
                out += [(host, i) for i in ids]
        # leagues learned for these teams (harvest records team -> league)
        learned = kb.get("tts_team_leagues", {})
        for key in ("t1n", "t2n"):
            for hl in learned.get(parsed[key], []):
                host, lid = hl.split("#")
                out.append((host, int(lid)))
        return list(dict.fromkeys(out))[:4]

    def applies(self, parsed, tm, kb):
        return bool(self._targets(parsed, tm, kb))

    def preferred_date(self, parsed):
        d = datetime.date.fromisoformat(parsed["date"])
        t = parsed.get("time")
        return (d - datetime.timedelta(days=1)).isoformat() if (not t or t < "12:00") else parsed["date"]

    async def find(self, parsed, tm, kb):
        from .. import gameindex
        pages = await asyncio.gather(*[get_text(SCHED.format(host=h, league=l), ttl=1800, timeout=40) for h, l in self._targets(parsed, tm, kb)])
        want = set(gameindex.around(parsed["date"], 1, 0))
        out = []
        for (h, l), page in zip(self._targets(parsed, tm, kb), pages):
            games = parse_schedule(page or "", h)
            gameindex.add_games(games)                  # learn the whole schedule while we have it
            for g in games:
                if g["date"] not in want:
                    continue
                cand = dict(g, adapter=self.name, kind="protocol", rosters="on the scoresheet")
                score_candidate(cand, parsed, cand["league"])
                if keep(cand):
                    out.append(cand)
        return out


# ------------------------------------------------------------------ harvest

SHEET = "https://stats.{host}.timetoscore.com/oss-scoresheet?game_id={gid}"
# hosts whose exhibition games are missing from the schedule pages: read the missing game
# numbers between this season's first and last one from the public scoresheets
GAPFILL_HOSTS = {"usphl"}


def parse_scoresheet(page: str, host: str, gid: str) -> dict | None:
    t = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "|", page or ""))).replace("\xa0", " ")
    t = re.sub(r"\s*\|[\s|]*", "|", t)
    m = re.search(r"Date:(\d\d)-(\d\d)-(\d\d)\|Time:([^|]*)\|League:([^|]*)\|Level:([^|]*)", t)
    v = re.search(r"\|Visitor\|([^|]+)\|", t)
    h = re.search(r"\|Home\|([^|]+)\|", t)
    if not (m and v and h):
        return None
    date = f"20{m.group(3)}-{m.group(1)}-{m.group(2)}"
    if not ("2026-07-01" <= date <= "2027-06-30"):
        return None
    hhmm = clock(m.group(4))
    tz = HOSTS.get(host, "America/New_York")
    return dict(url=REPORT.format(host=host, gid=gid), source="timetoscore", site=f"stats.{host}.timetoscore.com", gid=gid, date=date,
                time=hhmm, start_utc=local_to_utc(date, hhmm, tz), home=fix_name(h.group(1).strip()), away=fix_name(v.group(1).strip()),
                league=f"{m.group(5).strip()} {fix_name(m.group(6).strip())} exhibition".strip(), status=None, score=None)


async def gapfill(host: str, verbose: bool = True) -> int:
    from .. import gameindex
    with gameindex._lock:
        ids = [int(r[0]) for r in gameindex.conn().execute("select gid from games where site=? and date>='2026-07-01'", (f"stats.{host}.timetoscore.com",))]
    if not ids:
        return 0
    have, lo, hi = set(ids), min(ids), max(ids) + 150
    missing = [i for i in range(lo, hi) if i not in have]
    sem = asyncio.Semaphore(3)
    found = []

    async def one(gid):
        async with sem:
            page = await get_text(SHEET.format(host=host, gid=gid), ttl=30 * 86400, timeout=30)   # a game number never changes owner
        g = parse_scoresheet(page or "", host, str(gid))
        if g:
            found.append(g)
    await asyncio.gather(*[one(i) for i in missing])
    n = gameindex.add_games(found)
    if verbose:
        print(f"  {host}: {len(missing)} game numbers not in schedules, {n} are 2026-27 games (exhibitions)", flush=True)
    return n


STATS = "https://stats.{host}.timetoscore.com/display-stats.php?league={league}"
OPTION_RE = re.compile(r'<option value="(\d+)"[^>]*>([^<]*)')


def recent_season(sid: int, name: str, top: int) -> bool:
    """Tournaments and seasons are 'seasons' of a league; keep the 2026-27 ones."""
    n = name.lower()
    if re.search(r"2026|2027|26/27|26-27", n):
        return True
    if re.search(r"\b(19|20)\d\d\b|\b\d\d/\d\d\b", n):     # names another year
        return False
    return sid >= top - 25


async def harvest(max_league: int = 130, verbose: bool = True) -> dict:
    """Read every league of every host (its current season and every 2026-27 tournament listed
    as a season), store 2026-27 games in the index, and record which teams play where."""
    from .. import gameindex, lookup
    from ..lookup import norm
    found, team_leagues, failed = [], {}, []
    sem = asyncio.Semaphore(3)

    async def load(host, lid, season=None, sname=""):
        url = SCHED.format(host=host, league=lid) + (f"&season={season}" if season else "")
        page = None
        for attempt in range(3):              # the big schedules (AHF is 3.4 MB) can time out
            async with sem:
                page = await get_text(url, ttl=3 * 3600, timeout=150)
            if page:
                break
            await asyncio.sleep(5)
        if page is None:
            failed.append((host, lid, season))
            return
        games = parse_schedule(page or "", host)
        if not games:
            return
        n = gameindex.add_games(games)
        names = {}
        for g in games:
            names[g["league"].split(" ")[0]] = names.get(g["league"].split(" ")[0], 0) + 1
        found.append(dict(host=host, league=lid, season=season, season_name=sname, games=n, name=max(names, key=names.get),
                          first=min(g["date"] for g in games), last=max(g["date"] for g in games)))
        for g in games:
            for t in (g["home"], g["away"]):
                team_leagues.setdefault(norm(t), set()).add(f"{host}#{lid}")
        if verbose:
            print(f"  {host} league {lid} season {season or 'current'}: {n} games ({found[-1]['name']} {sname}, {found[-1]['first']} to {found[-1]['last']})", flush=True)

    async def league(host, lid):
        await load(host, lid)
        async with sem:
            stats_page = await get_text(STATS.format(host=host, league=lid), ttl=12 * 3600, timeout=60)
        opts = [(int(v), html.unescape(t).strip()) for v, t in OPTION_RE.findall(stats_page or "") if v != "0"]
        top = max([v for v, _ in opts] or [0])
        await asyncio.gather(*[load(host, lid, v, t) for v, t in opts if recent_season(v, t, top)])

    await asyncio.gather(*[league(h, l) for h in HOSTS for l in range(1, max_league + 1)])
    for h in GAPFILL_HOSTS:
        await gapfill(h, verbose)
    # a league that could not be read this time keeps what the last harvest knew
    got = {(x["host"], x["league"], x.get("season")) for x in found}
    for old in load_leagues():
        k = (old["host"], old["league"], old.get("season"))
        if k in {(h, l, s_) for h, l, s_ in failed} and k not in got:
            found.append(old)
    found.sort(key=lambda x: (x["host"], x["league"], x.get("season") or 0))
    LEAGUES_FILE.write_text(json.dumps(found, indent=1))
    kb = lookup.load_kb()
    prev = kb.get("tts_team_leagues") or {}
    if failed:
        for k, v in prev.items():
            team_leagues.setdefault(k, set()).update(v)
    kb["tts_team_leagues"] = {k: sorted(v) for k, v in team_leagues.items()}
    lookup.save_kb(kb)
    return dict(leagues=len(found), games=sum(x["games"] for x in found), teams=len(team_leagues), failed=len(failed))


if __name__ == "__main__":
    t = time.time()
    print(asyncio.run(harvest()), f"{time.time() - t:.0f}s")

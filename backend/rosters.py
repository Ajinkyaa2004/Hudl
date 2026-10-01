"""Roster check: when no report exists, open each team's roster links and say which
one is this season's, how many players it lists, and whether the players' birth years
fit the header's age group. The best link per team comes first."""
from __future__ import annotations
import asyncio, datetime, html, re, statistics
from .fetch import get_text, browser_page
from .lookup import tokens, domain

BLOCKED = {"eliteprospects.com", "chl.ca", "hockeyslovakia.sk", "iihf.com", "hockeydb.com", "khl.ru", "en.khl.ru", "mhl.khl.ru"}
BROWSER = {"myhockeyrankings.com", "gamesheetstats.com"}
MHR_T = re.compile(r"myhockeyrankings\.com/team[-_]info(?:\.php)?(?:\?[^#]*?\bt=|/)(\d+)")
SEASON_RE = re.compile(r"\b(20\d\d)\s*[-/]\s*(?:20)?(\d\d)\b")
ROW_RE = re.compile(r"(?:^|\n)\s*#?(\d{1,2})\s+([A-ZÀ-ÖØ-Þ][\w'’.\-]+(?:\s+[A-ZÀ-ÖØ-Þ][\w'’.\-]+){1,3})")
BIRTH_RE = re.compile(r"\b(19[89]\d|20[0-2]\d)\b")


_gates: dict = {}
_last: dict = {}


async def paced_browser(url: str, d: str):
    """One browser load at a time per site, a few seconds apart, one retry: sites such as
    MyHockeyRankings challenge bursts of automated loads."""
    import time
    gate = _gates.setdefault(d, asyncio.Lock())
    for attempt in range(2):
        async with gate:
            wait = 3 - (time.time() - _last.get(d, 0))
            if wait > 0:
                await asyncio.sleep(wait)
            txt = await browser_page(url, wait_ms=5000, js="() => document.body.innerText", ttl=6 * 3600, retries=0)
            _last[d] = time.time()
        if txt:
            return txt
        await asyncio.sleep(4)
    return None


def season_start(date: str) -> int:
    d = datetime.date.fromisoformat(date)
    return d.year if d.month >= 7 else d.year - 1


def page_text(raw: str) -> str:
    """HTML to text with one table row or block per line."""
    s = re.sub(r"<script.*?</script>|<style.*?</style>", "", raw, flags=re.S | re.I)
    s = re.sub(r"</(tr|li|p|div|h\d)>|<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return "\n".join(re.sub(r"[ \t\xa0]+", " ", line).strip() for line in s.splitlines() if line.strip())


def assess(text: str, date: str, header_team: str) -> dict:
    """Season, player count and age-group fit from a roster page's text."""
    cur = season_start(date)
    seasons = sorted({int(a) for a, b in SEASON_RE.findall(text) if (int(b) - int(a[2:])) % 100 == 1})
    if cur in seasons:
        season = "current"
    elif seasons and max(seasons) < cur:
        season = f"old ({max(seasons)}-{str(max(seasons) + 1)[2:]})"
    else:
        season = "not stated"
    players = {}
    listed = []
    for m in ROW_RE.finditer(text):
        num, name = m.group(1), m.group(2)
        if num not in players:
            players[num] = name
            b = BIRTH_RE.search(text[m.end():m.end() + 40])
            born = b.group(1) if b and cur - 45 <= int(b.group(1)) <= cur - 4 else None
            listed.append(dict(n=num, name=name, born=born))
    # age-group fit from birth years on the same lines as players
    births = []
    for m in ROW_RE.finditer(text):
        b = BIRTH_RE.search(text[m.end():m.end() + 40])
        if b and cur - 45 <= int(b.group(1)) <= cur - 4:   # plausible player birth years only
            births.append(int(b.group(1)))
    if len(births) < 5:
        births = []
    _, ages, _ = tokens(header_team)
    fit = "unknown"
    if births and ages:
        want = next(iter(ages))
        want = int(want) if len(want) <= 2 else None   # "2011" style age tokens are birth years
        med = statistics.median(births)
        if want:
            age = cur + 1 - med
            fit = "fits" if want - 3 <= age <= want + 1 else f"does not fit (players born around {int(med)})"
        else:
            byear = int(next(iter(ages)))
            fit = "fits" if abs(med - byear) <= 1 else f"does not fit (players born around {int(med)})"
    if fit == "unknown" and ages:
        want = next(iter(ages))
        head = text[:1500].lower()
        if re.search(rf"\b(u|j)\s?{want}\b|\b{want}\s?u\b", head):
            fit = "age group shown on page"
    return dict(season=season, players=len(players), age_fit=fit, listed=listed[:40])


def mhr_current(url: str, date: str) -> str | None:
    m = MHR_T.search(url or "")
    return f"https://myhockeyrankings.com/team-info/{m.group(1)}/{season_start(date)}/roster" if m else None


def candidates(team: dict | None, date: str) -> list[tuple[str, str]]:
    """(label, url) roster links to check for one team, most specific first."""
    if not team:
        return []
    def label(u, key):
        d = domain(u)
        if "myhockeyrankings" in d: return "MyHockeyRankings"
        if "eliteprospects" in d: return "EliteProspects"
        if "gamesheet" in d: return "GameSheet"
        return "Team site" if key == "site" else "Roster link on file"
    out = []
    for key in ("roster", "mhr", "ep", "site"):
        u = team.get(key)
        if u and u not in [x[1] for x in out]:
            out.append((label(u, key), u))
    cur = next((mhr_current(u, date) for _, u in out if mhr_current(u, date)), None)
    if cur:
        out = [x for x in out if "myhockeyrankings" not in domain(x[1])]
        out.insert(0, ("MyHockeyRankings, this season", cur))
    return out[:4]


async def check(url: str, date: str, header_team: str) -> dict:
    d = domain(url)
    base = dict(url=url, domain=d)
    if any(d.endswith(b) for b in BLOCKED):
        return dict(base, status="blocked", note="site blocks automated checks, open in Chrome")
    raw = None
    if not any(d.endswith(b) for b in BROWSER):
        raw = await get_text(url, ttl=6 * 3600, timeout=15)
    if raw is None or len(raw) < 3000:
        txt = await paced_browser(url, d)
        if not txt:
            return dict(base, status="unreachable", note="page did not load")
        text = txt
    else:
        text = page_text(raw)
    if len(text) < 200 and re.search(r"not found|404", text, re.I):
        return dict(base, status="not found", note="page does not exist")
    if "myhockeyrankings" in d and not re.search(r"#\s*Name\s*Pos", text):
        return dict(base, status="no roster", note="no roster posted on MyHockeyRankings")
    a = assess(text, date, header_team)
    youth = bool(tokens(header_team)[1])
    age_ok = (a["age_fit"] in ("fits", "age group shown on page")) or (not youth and not a["age_fit"].startswith("does not"))
    if a["players"] >= 10 and a["season"] == "current" and age_ok:
        status = "verified"
    elif a["players"] >= 12 and a["season"] == "not stated" and age_ok:
        status = "probable"
    elif a["players"] >= 10:
        status = "check"
    else:
        status = "not a roster"
    return dict(base, status=status, **a)


ORDER = {"verified": 0, "probable": 1, "check": 2, "blocked": 3, "not a roster": 4, "no roster": 5, "not found": 6, "unreachable": 7}


def rank(r: dict) -> tuple:
    return (ORDER.get(r.get("status"), 5), (r.get("season") or "").startswith("old"), -(r.get("players") or 0))


async def check_team(team: dict | None, header_team: str, date: str) -> list[dict]:
    cands = candidates(team, date)
    results = await asyncio.gather(*[check(u, date, header_team) for _, u in cands], return_exceptions=True)
    out = []
    for (label, u), r in zip(cands, results):
        if isinstance(r, Exception):
            r = dict(url=u, domain=domain(u), status="unreachable", note=str(r)[:60])
        out.append(dict(r, label=label))
    out.sort(key=rank)
    return out


async def team_rosters(team: dict | None, header_team: str, parsed: dict, game: dict | None = None) -> list[dict]:
    """Current rosters from the data sources (EliteProspects data server, GameSheet, HockeyTech,
    TimeToScore, ...), then the Club Data pages; shaped for the app's roster box."""
    from .roster_sources import find_rosters
    from .lookup import load_kb
    rs = await find_rosters(header_team, parsed["date"], dict(team=team or {}, comp=parsed["comp"], kb=load_kb(), game=game or {}))
    out = []
    for r in rs:
        pl = r.get("players")
        listed = [dict(n=p.get("number") or "", name=p.get("name") or "", born=p.get("birth_year") or "", pos=p.get("pos") or "")
                  for p in pl] if isinstance(pl, list) else r.get("listed")
        out.append(dict(r, label=r.get("source") or r.get("label") or "Roster", players=len(pl) if isinstance(pl, list) else (pl or 0),
                        listed=listed))
    return out


async def check_rosters(parsed: dict, teams: dict, budget: float = 30, games: dict | None = None) -> dict:
    games = games or {}
    t1 = asyncio.create_task(team_rosters(teams["t1n"]["best"], parsed["t1"], parsed, games.get("t1")))
    t2 = asyncio.create_task(team_rosters(teams["t2n"]["best"], parsed["t2"], parsed, games.get("t2")))
    done, pending = await asyncio.wait([t1, t2], timeout=budget)
    for t in pending:
        t.cancel()
    get = lambda t: t.result() if t in done and not t.cancelled() and t.exception() is None else []
    return dict(t1=get(t1), t2=get(t2))

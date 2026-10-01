"""RAMP InterActive sites: OWHL U22 Elite (owhlu22elite.ca), Eastern Alliance Kickoff
(eakickoff.tournamentsgurus.com), Ottawa 67's AA, and any site added to data/ramp_leagues.json.

Three sources, per site:
- classic JSON API (league games, whole season): {host}/api/leaguegame/get/{aid}/{sid}/0/0/0/0/
- Blazor schedule pages (division schedule, team masterschedule, tournament division
  masterschedule) read in the headless browser for the header's month; these also list
  tournament and exhibition games, with the game id (GID) on each card
- a tournament's menu (plain HTML) naming its divisions and teams, so only the division that
  holds a header team is opened in the browser.
Report: {site}/division/{cat}/{div}/game/view/{GID} (GIDs are global across RAMP hosts)."""
from __future__ import annotations
import asyncio, datetime, difflib, html, json, re
from pathlib import Path
from ..fetch import get_json, get_text, browser_page
from ..lookup import tokens
from .base import Adapter, local_to_utc
from .na_util import local_date, clock24, score_local, header_ages, division_age

DATA = Path(__file__).resolve().parent.parent.parent / "data" / "ramp_leagues.json"
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
NUMBER = re.compile(r"\s*#\d+(?:\s*\(\d+\))?\s*$")          # 'Cambridge Rivulettes #2214 (4)' -> 'Cambridge Rivulettes'

# Reads the Blazor schedule for one month: clicks the month arrows, then returns the game cards.
CARDS_JS = r"""async () => {
  const want = "__MONTH__";
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];
  const label = () => [...document.querySelectorAll('span')].map(e => e.textContent.trim()).find(t => /^[A-Z][a-z]+ \d{4}$/.test(t) && MONTHS.includes(t.split(' ')[0]));
  const idx = t => { const [m, y] = t.split(' '); return +y * 12 + MONTHS.indexOf(m); };
  for (let k = 0; k < 20 && !label(); k++) await sleep(300);
  for (let i = 0; i < 14 && want; i++) {
    const cur = label();
    if (!cur || cur === want) break;
    const span = [...document.querySelectorAll('span')].find(e => e.textContent.trim() === cur);
    const btns = [...span.parentElement.querySelectorAll('button')];
    const btn = idx(want) < idx(cur) ? btns[0] : btns[btns.length - 1];
    if (!btn || btn.disabled) break;
    btn.click();
    for (let k = 0; k < 20 && label() === cur; k++) await sleep(300);
    await sleep(800);
  }
  for (let k = 0; k < 15 && !document.querySelector('.master-schedule-type-card') && !/no events found/i.test(document.body.innerText); k++) await sleep(400);
  const cards = [...document.querySelectorAll('.master-schedule-type-card')].map(c => {
    const link = c.querySelector('a[href*="/game/view/"]');
    const bar = c.querySelector('.master-schedule-type-card-bar');
    const num = bar ? [...bar.children].map(e => e.textContent.trim()).find(t => /^\d{6,}$/.test(t)) : null;
    return {num: num || null, href: link ? link.getAttribute('href') : null,
            teams: [...c.querySelectorAll('.team')].map(e => e.textContent.trim()),
            vs: [...c.querySelectorAll('.vs p')].map(e => e.textContent.trim()),
            when: ((c.querySelector('.master-schedule-type-card-date') || {}).textContent || '').trim(),
            division: bar ? ((bar.querySelector('.box') || {}).textContent || '').trim() : '',
            title: ((c.querySelector('.master-schedule-type-card-title') || {}).textContent || '').trim()};
  });
  return JSON.stringify({month: label() || null, cards});
}"""


def _cfg() -> list[dict]:
    try:
        return json.loads(DATA.read_text())["sites"]
    except Exception:
        return []


def team(name: str) -> str:
    return NUMBER.sub("", html.unescape(name or "")).strip()


def parse_when(text: str) -> tuple[str | None, str | None]:
    """'Friday, September 4, 2026 1:00 p.m.' -> ('2026-09-04', '13:00')."""
    m = re.search(r"([A-Z][a-z]+) (\d{1,2}), (20\d\d)\s*(.*)", text or "")
    if not m or m.group(1) not in MONTHS:
        return None, None
    d = datetime.date(int(m.group(3)), MONTHS.index(m.group(1)) + 1, int(m.group(2)))
    return d.isoformat(), clock24(m.group(4))


def parse_cards(raw: str | None, cat, div) -> list[dict]:
    """Game cards from CARDS_JS -> games with GID, home first (as on the RAMP calendars)."""
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return []
    games = []
    for c in data.get("cards") or []:
        m = re.search(r"/division/(\d+)/(\d+)/game/view/(\d+)", c.get("href") or "")
        gid = m.group(3) if m else c.get("num")
        if not gid or len(c.get("teams") or []) != 2:
            continue
        date, hhmm = parse_when(c.get("when"))
        vs = c.get("vs") or []
        score = re.sub(r"\s+", "", vs[0]) if vs and re.fullmatch(r"\d+\s*-\s*\d+", vs[0]) else None
        final = len(vs) > 1 and "final" in vs[1].lower()
        games.append(dict(gid=gid, date=date, time=hhmm, home=team(c["teams"][0]), away=team(c["teams"][1]),
                          score=score if final else None, status=vs[1] if len(vs) > 1 else None,
                          division=c.get("division") or "", title=c.get("title") or "",
                          cat=m.group(1) if m else cat, div=m.group(2) if m else div))
    return games


def parse_menu(page: str) -> dict:
    """Tournament menu: division id -> (division name, [team names])."""
    out = {}
    for m in re.finditer(r'id="accordion-menu-title-(\d+)".*?<span>([^<]+)</span>(.*?)(?=id="accordion-menu-title-\d+"|$)', page or "", re.S):
        teams = [html.unescape(t).strip() for t in re.findall(r'href="/team/\d+/\d+/%s/\d+/[^"]*"[^>]*><p>([^<]+)</p>' % m.group(1), m.group(3))]
        out[m.group(1)] = (html.unescape(m.group(2)).strip(), teams)
    return out


def _sim(a: str, b: str) -> float:
    ta, tb = tokens(a)[0], tokens(b)[0]
    if not ta or not tb:
        return 0.0
    return 1.0 if ta in tb or tb in ta else difflib.SequenceMatcher(None, ta, tb).ratio()


class Ramp(Adapter):
    name = "ramp"
    site = "rampinteractive.com"
    budget_s = 60

    def _sites(self, parsed, tm):
        c = parsed["comp"].lower()
        names = (parsed["t1"] + " | " + parsed["t2"]).lower()
        d = parsed["date"]
        out = []
        for s in _cfg():
            if s.get("from") and not (s["from"] <= d <= (datetime.date.fromisoformat(s["to"]) + datetime.timedelta(days=1)).isoformat()):
                continue
            host = s["site"].split("//", 1)[-1].replace("www.", "")
            linked = any(host in ((tm[k]["best"] or {}).get(f) or "") for k in ("t1n", "t2n") for f in ("site", "schedule", "roster"))
            if linked or any(w in c for w in s.get("comp", [])) or any(w in names for w in s.get("teams", [])):
                out.append(s)
        return out

    def applies(self, parsed, tm, kb):
        return bool(self._sites(parsed, tm))

    def preferred_date(self, parsed):
        return local_date(parsed, "America/Toronto")

    def _months(self, parsed, tz) -> list[str]:
        days = {parsed["date"], local_date(parsed, tz)}
        return sorted({f"{MONTHS[int(x[5:7]) - 1]} {x[:4]}" for x in days})

    async def _classic(self, s) -> list[dict]:
        c = s["classic"]
        data = await get_json(f"{c['host']}/api/leaguegame/get/{c['aid']}/{c['sid']}/0/0/0/0/", ttl=3 * 3600, timeout=40)
        games = []
        for g in data or []:
            date, hhmm = (g.get("sDate") or "")[:10], (g.get("sDate") or "")[11:16]
            games.append(dict(gid=str(g.get("GID")), date=date, time=hhmm or None, home=team(g.get("HomeTeamName")),
                              away=team(g.get("AwayTeamName")), division=g.get("HomeDivision") or "", title="",
                              score=f"{g.get('homeScore')}-{g.get('awayScore')}" if g.get("completed") else None,
                              status="Final" if g.get("completed") else None, cat=g.get("CATID") or 0, div=g.get("homeDID") or 0))
        return games

    async def _page(self, s, path, cat, div, month) -> list[dict]:
        raw = await browser_page(s["site"] + path, wait_ms=2500, ttl=1800, js=CARDS_JS.replace("__MONTH__", month))
        return parse_cards(raw, cat, div)

    async def _site_games(self, s, parsed) -> list[dict]:
        months = self._months(parsed, s.get("tz") or "America/Toronto")
        jobs = []
        if s.get("classic"):
            jobs.append(self._classic(s))
        for p in s.get("pages", []):
            for mo in months:
                jobs.append(self._page(s, p["path"], p.get("cat", 0), p.get("div", 0), mo))
        if s.get("discover"):
            menu = parse_menu(await get_text(s["site"] + s["discover"], ttl=6 * 3600))
            ages = header_ages(parsed["t1"]) | header_ages(parsed["t2"])

            def rank(item):
                dname, teams = item[1]
                hits = sum(any(_sim(h, t) >= 0.85 for t in teams) for h in (parsed["t1"], parsed["t2"]))
                age, _ = division_age(dname)
                return hits + (0.5 if age and int(age[:-1]) in ages else 0)
            ranked = sorted(((rank(it), it[0]) for it in menu.items()), reverse=True)
            picked = [div for r, div in ranked if r >= 1][:3]
            for div in picked:
                for mo in months:
                    jobs.append(self._page(s, f"/division/0/{div}/masterschedule", 0, div, mo))
        out = []
        for r in await asyncio.gather(*jobs, return_exceptions=True):
            if not isinstance(r, Exception):
                out += r
        return out

    async def find(self, parsed, tm, kb):
        sites = self._sites(parsed, tm)
        res = await asyncio.gather(*[self._site_games(s, parsed) for s in sites], return_exceptions=True)
        d = datetime.date.fromisoformat(parsed["date"])
        out, seen = [], set()
        for s, r in zip(sites, res):
            if isinstance(r, Exception):
                continue
            tz = s.get("tz") or "America/Toronto"
            for g in r:
                if not g.get("date") or abs((datetime.date.fromisoformat(g["date"]) - d).days) > 1 or g["gid"] in seen:
                    continue
                seen.add(g["gid"])
                league = " ".join(x for x in (s["name"], g["title"], g["division"], s.get("context") or "") if x)
                league = re.sub(r"\bU(\d{1,2})(A{1,3})\b", r"U\1 \2", league)       # 'U18AA' -> 'U18 AA'
                cand = dict(source=self.name, site=s["site"].split("//", 1)[-1], league=league,
                            home=g["home"], away=g["away"], date=g["date"], score=g["score"], status=g["status"],
                            kind="protocol", adapter=self.name,
                            url=f"{s['site']}/division/{g['cat']}/{g['div']}/game/view/{g['gid']}",
                            start_utc=local_to_utc(g["date"], g["time"], tz))
                score_local(cand, parsed, league, tz)
                if min(cand["team_scores"]) >= 0.6:
                    out.append(cand)
        return out

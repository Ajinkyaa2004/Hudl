"""Regystra tournament portals (my.200x85.com: CCM Denver, Howies Hometown Cup, Boom Boom Cup,
Savannah Showdown, KSL, Klevr ...). The portal lists its tournaments with start date and state;
each division's schedule comes as JSON (an HTML table) when asked with X-Requested-With.
Report: https://{portal}/tournaments/{uuid}/games/{game uuid}/show. Portals: data/regystra_portals.json."""
from __future__ import annotations
import asyncio, datetime, html, json, re
from pathlib import Path
from ..fetch import get_text
from .base import Adapter, local_to_utc
from .na_util import local_date, clock24, division_age, age_close, header_ages, score_local, swap_words

DATA = Path(__file__).resolve().parent.parent.parent / "data" / "regystra_portals.json"
XHR = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/html"}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
BY = re.compile(r"(?<![0-9A-Za-z])(20[01][0-9])(?![0-9])")
GENERIC = re.compile(r"usa hockey|hockey canada|friendl|test tag|\b\d{1,2}U\b|\bU\d{1,2}\b", re.I)


def _cfg() -> dict:
    try:
        return json.loads(DATA.read_text())
    except Exception:
        return {"portals": [], "state_tz": {}}


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


TAG = r"(?:[A-Z]?\d+(?:st|nd|rd|th)(?:\s*Seed)?|CONS|[A-Z]\d?|Game\s*\d+\s*(?:Winner|Loser))"
SEED = re.compile(r"^\s*\(" + TAG + r"\)\s*|\s*\(" + TAG + r"\)\s*$|^\s*\d{2}'\s*", re.I)


def clean_team(name: str, season_start_year: int = 2026) -> str:
    """'(A1st) Foothills Flyers' / "14' Naperville Sabres (CONS)" -> the team name; a birth year
    in the name becomes the age group ('Team Ohio 2012 - Metzger' -> 'Team Ohio 14U - Metzger')."""
    prev = None
    while prev != name:
        prev, name = name, SEED.sub("", name or "").strip()
    name = re.sub(r"\b(\d{1,2})(A{1,3})\b", r"\1U \2", name)          # '12AA HP' -> '12U AA HP'
    return BY.sub(lambda m: f"{season_start_year - int(m.group(1))}U", name)


def parse_tournaments(page: str) -> list[dict]:
    """Cards on /tournaments: uuid, name, start date, state."""
    out = []
    for chunk in re.split(r'<a href="https://[^"/]+/tournaments/', page or "")[1:]:
        uid = chunk[:36]
        if not re.fullmatch(r"[0-9a-f-]{36}", uid):
            continue
        body = _text(chunk[:3000])
        m = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w* (\d{1,2}), (20\d\d)\s*\|?\s*([A-Za-z .]+), (US|CA)", body)
        if not m:
            continue
        name = html.unescape((re.search(r'alt="([^"]*)"', chunk) or [None, ""])[1])
        start = datetime.date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))
        out.append(dict(uuid=uid, name=name, start=start.isoformat(), state=m.group(4).strip()))
    return out


def parse_divisions(page: str) -> list[tuple[str, str]]:
    return [(v, html.unescape(n).strip()) for v, n in re.findall(r'<option\s+data-url="[^"]*division=(\d+)"[^>]*>\s*([^<]+?)\s*</option>', page or "")]


def parse_schedule(table: str, year: int) -> list[dict]:
    """Rows in page order, with the date taken from the last 'Friday, September 18th' header."""
    games, day = [], None
    for m in re.finditer(r'fa-calendar-alt[^>]*></i>\s*([^<]+?)\s*<|(<tr id="row-([0-9a-f-]{36})".*?</tr>)', table or "", re.S):
        if m.group(1):
            d = re.search(r"(\w+) (\d{1,2})(?:st|nd|rd|th)?", m.group(1).split(",", 1)[-1])
            if d and d.group(1)[:3].lower() in MONTHS:
                day = datetime.date(year, MONTHS[d.group(1)[:3].lower()], int(d.group(2)))
            continue
        tds = [_text(t) for t in re.findall(r"<td[^>]*>(.*?)</td>", m.group(2), re.S)]
        if len(tds) < 11 or not day:
            continue
        link = re.search(r"window\.location\.href='([^']+)'", m.group(2))
        final = "final" in tds[5].lower()
        games.append(dict(gid=m.group(3), date=day.isoformat(), time=clock24(tds[1]), home=clean_team(tds[2]), away=clean_team(tds[8]),
                          score=f"{tds[4]}-{tds[6]}" if final and tds[4] and tds[6] else None, status=tds[5] or None,
                          division=tds[10], url=(link.group(1).replace("\\/", "/") if link else None)))
    return games


class Regystra(Adapter):
    name = "regystra"
    site = "my.200x85.com"
    budget_s = 45

    def applies(self, parsed, tm, kb):
        c = parsed["comp"].lower()
        if any(k in c for p in _cfg()["portals"] for k in p.get("comp", [])):
            return True
        for key in ("t1n", "t2n"):
            b = tm[key]["best"] or {}
            if any(h in (b.get(f) or "") for f in ("site", "schedule", "roster") for h in ("200x85", "regystra")):
                return True
        if GENERIC.search(parsed["comp"]):
            countries = {((tm[k]["best"] or {}).get("country") or "").lower() for k in ("t1n", "t2n")}
            return "canada" not in countries
        return False

    def preferred_date(self, parsed):
        return local_date(parsed, "America/Chicago")

    async def _tournament_games(self, host, t, parsed, names=None):
        names = names or {}
        base = f"https://{host}/tournaments/{t['uuid']}/schedules"
        page = await get_text(base, ttl=6 * 3600)
        ages = header_ages(parsed["t1"]) | header_ages(parsed["t2"])
        out = []
        for div_id, div_name in parse_divisions(page):
            age, _ = division_age(div_name, 2026)
            if ages and age and not any(abs(int(age[:-1]) - a) <= 1 for a in ages):
                continue
            raw = await get_text(f"{base}?division={div_id}", ttl=1800, headers=XHR)
            try:
                table = json.loads(raw or "{}").get("html") or ""
            except Exception:
                table = ""
            for g in parse_schedule(table, int(t["start"][:4])):
                g["division"] = g["division"] or div_name
                g["home"], g["away"] = swap_words(g["home"], names), swap_words(g["away"], names)
                out.append((t, g))
        return out

    async def find(self, parsed, tm, kb):
        cfg = _cfg()
        d = datetime.date.fromisoformat(parsed["date"])
        jobs = []
        for p in cfg["portals"]:
            host = p["host"]
            for t in parse_tournaments(await get_text(f"https://{host}/tournaments", ttl=6 * 3600)):
                start = datetime.date.fromisoformat(t["start"])
                if start - datetime.timedelta(days=1) <= d <= start + datetime.timedelta(days=6):
                    jobs.append(self._tournament_games(host, t, parsed, p.get("names")))
        res = await asyncio.gather(*jobs, return_exceptions=True)
        out = []
        for r in res:
            if isinstance(r, Exception):
                continue
            for t, g in r:
                if abs((datetime.date.fromisoformat(g["date"]) - d).days) > 1 or not g["url"]:
                    continue
                age, from_birth_year = division_age(g["division"], 2026)
                if not age_close(parsed, age):
                    continue
                # teams play up and down: '11U' Littleton Hawks in the '12U Jetspeed' division, and
                # birth-year divisions ('2014 Jetspeed') hold 11U and 12U teams. The division's age
                # is context only when the header teams carry exactly that age.
                ages = header_ages(parsed["t1"]) | header_ages(parsed["t2"])
                exact = bool(age) and not from_birth_year and (not ages or int(age[:-1]) in ages)
                tz = cfg["state_tz"].get(t["state"], "America/Chicago")
                div = BY.sub(r"Y\1", g["division"])        # '2014 Jetspeed' -> 'Y2014 Jetspeed': not read as an age
                league = f"{t['name']} {div}"
                if not exact:
                    league = re.sub(r"\b(?:u\s?\d{1,2}|\d{1,2}\s?u)\b", "", league, flags=re.I)
                cand = dict(source=self.name, site=self.site, league=league, home=g["home"], away=g["away"],
                            date=g["date"], score=g["score"], status=g["status"], kind="protocol", adapter=self.name,
                            url=g["url"], start_utc=local_to_utc(g["date"], g["time"], tz))
                score_local(cand, parsed, f"{age} {div}" if exact else " ", tz)
                if min(cand["team_scores"]) >= 0.6:
                    out.append(cand)
        return out

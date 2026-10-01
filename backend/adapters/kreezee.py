"""Kreezee Sports sites: Blue Line tournaments (barn-burner.bluelinetournaments.com and the
other subdomains), Statewide Florida Hockey League (sfhlhockey.com), Boston Breakout
(boston-breakout.kreezee-sports.com). Each site has a public JSON API:
  api.kreezee.com/api/solution/{sid}/config            -> defaultSeasonId
  {host}/api/v2/solutions/{sid}/seasons/{season}/matches/dates?startDate=..&endDate=..
Report: {host}/scores/game-{id} (same id as handlers/GameSheet.ashx?gameId={id}).
Sites are listed in data/kreezee_sites.json."""
from __future__ import annotations
import asyncio, datetime, json, re
from pathlib import Path
from urllib.parse import quote
from ..fetch import get_text, get_json
from .base import Adapter, local_to_utc, COLOURS as _COLOURS
import re as _re
COLOURS = _re.compile(r"(?:gold|black|white|red|blue|navy|green|silver|orange|purple|grey|gray|maroon|teal|yellow|royal)", _re.I)
from .na_util import local_date, clock24, swap_words, division_age, age_close, score_local

DATA = Path(__file__).resolve().parent.parent.parent / "data" / "kreezee_sites.json"
CONFIG = "https://api.kreezee.com/api/solution/{sid}/config"
MATCHES = "https://{host}/api/v2/solutions/{sid}/seasons/{season}/matches/dates?startDate={a}%2000:00:00&endDate={b}%2023:59:59"
REPORT = "https://{host}/scores/game-{gid}"
PREFIX = re.compile(r"^\d{2}[:\-][0-9A-Za-z]+-\s*")          # '26:14ABK-TBHC Blue' -> 'TBHC Blue'
GENERIC = re.compile(r"usa hockey|hockey canada|friendl|test tag|\b1\dU\b|\bu1\d\b", re.I)


def _cfg() -> dict:
    try:
        return json.loads(DATA.read_text())
    except Exception:
        return {"hubs": [], "sites": [], "names": {}}


def split_division(division: str) -> tuple[str | None, bool, str, str]:
    """"14U 'A' BLACK" -> ('14U', False, 'A', 'Black'); '18UAA' -> ('18U', False, 'AA', '');
    '10UA-BLACK' -> ('10U', False, 'A', 'Black'); '2014' -> ('12U', True, '', '')."""
    d = re.sub(r"['\"]", " ", division or "")
    d = re.sub(r"\b(\d{1,2})\s?U(?=A|\b)", r"\1U ", d, flags=re.I)      # 18UAA -> 18U AA
    age, from_birth_year = division_age(d)
    rest = re.sub(r"\b(?:u\s?\d{1,2}|\d{1,2}\s?u|20[01]\d)\b", " ", d, flags=re.I)
    words = re.sub(r"[-_/]", " ", rest).split()
    level = " ".join(w.upper() for w in words if re.fullmatch(r"a{1,3}|b{1,2}", w, re.I))
    colour = " ".join(w.capitalize() for w in words if COLOURS.fullmatch(w))
    return age, from_birth_year, level, colour


def _closest_colours(team: str, parsed: dict) -> set:
    """Colours named by the header team whose name is closest to this site team."""
    import difflib
    from ..lookup import tokens
    tb = tokens(team)[0]
    best = max((parsed["t1"], parsed["t2"]), key=lambda h: difflib.SequenceMatcher(None, tokens(h)[0], tb).ratio())
    return {w.lower() for w in _COLOURS.findall(best)}


def team_name(raw: str, division: str, names: dict, parsed: dict | None = None) -> str:
    """Site team + the division's age and level, and the division colour when the matching header
    team names it: '26:14ABK-TBHC Blue' in "14U 'A' BLACK" -> 'Tampa Bay Hockey Club Blue 14U A Black'."""
    t = swap_words(PREFIX.sub("", raw or ""), names)
    header_colours = _closest_colours(t, parsed) if parsed else set()
    age, from_birth_year, level, colour = split_division(division)
    if from_birth_year:
        t = re.sub(r"\s+\d{2}$", "", t)          # Boston Breakout 'Darien 14' in division 2014
    if age and not from_birth_year and not re.search(r"\b(?:u\s?\d{1,2}|\d{1,2}\s?u)\b", t, re.I):
        t = f"{t} {age}"
    if level:
        t = f"{t} {level}"
    if colour and colour.lower() in header_colours and colour.lower() not in t.lower():
        t = f"{t} {colour}"
    return t.strip()


class Kreezee(Adapter):
    name = "kreezee"
    site = "kreezee-sports.com"
    budget_s = 40

    def applies(self, parsed, tm, kb):
        c = parsed["comp"].lower()
        cfg = _cfg()
        for s in cfg["hubs"] + cfg["sites"]:
            if any(k in c for k in s.get("comp", [])):
                return True
        for key in ("t1n", "t2n"):
            b = tm[key]["best"] or {}
            if any(h in (b.get(f) or "") for f in ("site", "schedule", "roster") for h in ("kreezee", "bluelinetournaments", "sfhlhockey")):
                return True
        # tournaments run under generic competition names ('USA Hockey 14U'); only US teams
        if GENERIC.search(parsed["comp"]):
            countries = {((tm[k]["best"] or {}).get("country") or "").lower() for k in ("t1n", "t2n")}
            return bool(countries & {"usa", "united states", ""}) and "canada" not in countries
        return False

    def preferred_date(self, parsed):
        return local_date(parsed, "America/New_York")

    async def _hosts(self) -> list[tuple[str, dict]]:
        cfg = _cfg()
        out = [(s["host"], s) for s in cfg["sites"]]
        for hub in cfg["hubs"]:
            page = await get_text(hub["url"], ttl=24 * 3600)
            for h in sorted(set(re.findall(hub["host_re"], page or ""))):
                if not h.startswith("www."):
                    out.append((h, hub))
        return out

    async def _solution(self, host: str):
        page = await get_text(f"https://{host}/scores", ttl=7 * 24 * 3600)
        m = re.search(r"solutions?/(\d{3,6})", page or "")
        if not m:
            return None, None
        sid = m.group(1)
        conf = await get_json(CONFIG.format(sid=sid), ttl=24 * 3600)
        season = (conf or {}).get("defaultSeasonId")
        return sid, season or None

    async def _games(self, host, meta, parsed):
        sid, season = await self._solution(host)
        if not sid or not season:
            return []
        d = datetime.date.fromisoformat(parsed["date"])
        a, b = (d - datetime.timedelta(days=1)).isoformat(), d.isoformat()
        data = await get_json(MATCHES.format(host=host, sid=sid, season=season, a=a, b=b), ttl=1800, timeout=30)
        return [(host, meta, g) for g in ((data or {}).get("Results") or [])]

    async def find(self, parsed, tm, kb):
        cfg = _cfg()
        hosts = await self._hosts()
        res = await asyncio.gather(*[self._games(h, m, parsed) for h, m in hosts], return_exceptions=True)
        out = []
        for r in res:
            if isinstance(r, Exception):
                continue
            for host, meta, g in r:
                names = cfg["names"].get(meta.get("names") or "", {})
                div_h, div_a = g.get("LocalDivision") or "", g.get("VisitorDivision") or ""
                age, from_birth_year = division_age(div_h)
                if from_birth_year and not age_close(parsed, age):
                    continue
                date = (g.get("Date") or "")[:10]
                tz = meta.get("tz") or "America/New_York"
                league = f"{g.get('SolutionName') or host} {div_h}".strip()
                final = g.get("Final") or g.get("StatusId") == 3
                cand = dict(source=self.name, site=host, league=league,
                            home=team_name(g.get("LocalTeamName"), div_h, names, parsed),
                            away=team_name(g.get("VisitorTeamName"), div_a, names, parsed), date=date,
                            score=f"{g.get('LocalResult')}-{g.get('VisitorResult')}" if final else None,
                            status="Final" if final else None, kind="protocol", adapter=self.name,
                            url=REPORT.format(host=host, gid=g.get("Id")),
                            start_utc=local_to_utc(date, clock24(g.get("StartTime")), tz))
                score_local(cand, parsed, " " if from_birth_year else league, tz)
                if min(cand["team_scores"]) >= 0.6:
                    out.append(cand)
        return out

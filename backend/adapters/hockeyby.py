"""Belarus (hockey.by): Betera-Extraleague, Betera-Higher League, friendlies of Belarusian clubs.
The calendar page is a Vue app fed by a Bitrix ajax action that also answers plain GET:
league + division + month, 12 games a page in date order. Report link: hockey.by/gamecenter/<id>/.
Names are Russian short names (Шахтер, Юность), mapped through data/hockeyby_teams.json.
Times are Minsk time (UTC+3), the same clock as the header."""
from __future__ import annotations
import datetime, html, json, re
from pathlib import Path
from ..fetch import get_json
from ..lookup import norm
from .base import Adapter, score_candidate, local_to_utc
from .khl import translit

DATA = Path(__file__).resolve().parent.parent.parent / "data" / "hockeyby_teams.json"
API = ("https://hockey.by/bitrix/services/main/ajax.php?mode=class&c=2quick%3Agame.calendar&action=getSelect"
       "&arFilter[0][name]=SEASON&arFilter[0][value]={season}&arFilter[1][name]=LEAGUE&arFilter[1][value]={league}"
       "&arFilter[2][name]=DIVISION&arFilter[2][value]={division}&arFilter[3][name]=TEAM&arFilter[3][value]=all"
       "&arFilter[4][name]=MONTH&arFilter[4][value]={month}&status=all&place=all&view=list&page={page}")
REPORT = "https://hockey.by/gamecenter/{gid}/"
MONTHS = {m: i + 1 for i, m in enumerate(["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
                                          "сентябрь", "октябрь", "ноябрь", "декабрь"])}
LEAGUE_NAME = {1: "Betera-Extraleague", 5: "Betera-Higher League", 9: "Belarus national junior teams"}

_data = None


def data() -> dict:
    global _data
    if _data is None:
        try:
            _data = json.loads(DATA.read_text())
        except Exception:
            _data = {"teams": {}}
    return _data


def english(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip()
    return data()["teams"].get(name) or data()["teams"].get(name.replace("ё", "е")) or translit(name)


def parse_items(items: list, season_start: int) -> list[dict]:
    out = []
    for it in items or []:
        h = it.get("HTML") or ""
        d = re.search(r'current-game-date">\s*(\d{1,2})\s*<div class="current-game-date-month">\s*([^<]+)', h)
        gid = re.search(r"/gamecenter/(\d+)/", h)
        names = [html.unescape(x).strip() for x in re.findall(r'current-game-team-name">([^<]*)', h)]
        if not (d and gid and len(names) == 2):
            continue
        month = MONTHS.get(d.group(2).strip().lower())
        if not month:
            continue
        year = season_start if month >= 7 else season_start + 1
        t = re.search(r'future-game-start">[^<]*?(\d{1,2}:\d\d)', h)
        pts = re.search(r'current-game-points">\s*<a[^>]*>\s*(\d+)\s*[-:]\s*(\d+)', h)
        out.append(dict(gid=gid.group(1), date=f"{year}-{month:02d}-{int(d.group(1)):02d}", home=names[0], away=names[1],
                        time=t.group(1) if t else None, score=f"{pts.group(1)}-{pts.group(2)}" if pts else None))
    return out


class HockeyBy(Adapter):
    name = "hockeyby"
    site = "hockey.by"
    budget_s = 40

    def leagues(self, parsed) -> list[int]:
        c = parsed["comp"].lower()
        # "Extraleague U20" / "Extraliga" alone are Slovak and Czech: only Betera or Belarus names count
        if not ("betera" in c or "belarus" in c):
            return []
        if "high" in c or "vysshaya" in c:
            return [5]
        return [1]

    def applies(self, parsed, tm, kb):
        if self.leagues(parsed):
            return True
        c = parsed["comp"].lower()
        if "friendl" in c or "cup" in c or "tournament" in c:
            kn = {norm(v) for v in data()["teams"].values()}
            if norm(parsed["t1"]) in kn or norm(parsed["t2"]) in kn:
                return True
            for k in ("t1n", "t2n"):
                b = (tm.get(k) or {}).get("best") or {}
                if "hockey.by" in f"{b.get('site')} {b.get('schedule')} {b.get('roster')}":
                    return True
        return False

    async def call(self, league, division, month, page, season, past: bool):
        return await get_json(API.format(season=season, league=league, division=division, month=month, page=page),
                              ttl=86400 if past else 1800)

    async def season_id(self, year: int) -> str | None:
        d = await self.call(1, "", 9, 1, "", False)
        for s in ((d or {}).get("data") or {}).get("SEASONS") or []:
            if str(s.get("UF_NAME", "")).startswith(f"{year}-"):
                return s["ID"]
        return None

    async def games_on(self, league: int, date: str) -> list[dict]:
        d = datetime.date.fromisoformat(date)
        start = d.year if d.month >= 7 else d.year - 1
        season = await self.season_id(start)
        if not season:
            return []
        past = d < datetime.date.today() - datetime.timedelta(days=2)
        first = await self.call(league, "", d.month, 1, season, past)
        divs = ((first or {}).get("data") or {}).get("DIVISIONS") or []
        default = next((x["ID"] for x in divs if x.get("selected")), None)
        out = []
        for div in [x["ID"] for x in divs if x.get("ID")] or [""]:
            for page in range(1, 15):
                # the division-less first call already is page 1 of the default division
                r = first if (page == 1 and div in (default, "")) else await self.call(league, div, d.month, page, season, past)
                data_ = (r or {}).get("data") or {}
                items = parse_items(data_.get("FUTURE_GAMES") or [], start)
                out += [g for g in items if g["date"] == date]
                pages = [int(x) for x in re.findall(r'data-page="(\d+)"', data_.get("NAV") or "")]
                if not items or page >= max(pages or [1]) or items[-1]["date"] > date:
                    break
        return out

    async def find(self, parsed, tm, kb):
        leagues = self.leagues(parsed) or [1, 5]
        if not self.leagues(parsed) and ("belarus" in parsed["t1"].lower() or "belarus" in parsed["t2"].lower()):
            leagues.append(9)
        out, seen = [], set()
        for lg in leagues:
            for g in await self.games_on(lg, parsed["date"]):
                if g["gid"] in seen:
                    continue
                seen.add(g["gid"])
                cand = dict(source=self.name, site=self.site, league=LEAGUE_NAME.get(lg, "hockey.by"),
                            home=english(g["home"]), away=english(g["away"]), date=g["date"], score=g["score"],
                            status=None, kind="protocol", adapter=self.name, url=REPORT.format(gid=g["gid"]),
                            start_utc=local_to_utc(g["date"], g["time"], "Europe/Minsk"),
                            site_names=f"{g['home']} - {g['away']}")
                score_candidate(cand, parsed, "")
                if min(cand["team_scores"]) >= 0.6:
                    out.append(cand)
        return out

"""KHL, MHL and VHL (khl.ru, mhl.khl.ru, vhlru.ru), plus the pre-season cups and friendlies of
their clubs. One source covers all of them: the KHL text-broadcast list for a date,
online.khl.ru/online/<YYYY-MM-DD>.html, lists every game of the KHL platform that day
(league games, pre-season cups, training games) with its game id. Names are Russian short names,
mapped to header names through data/khl_teams.json (transliteration for unlisted teams).
khl.ru answers the first request with a 307 that sets a cookie; the shared httpx client keeps
cookies, so no browser is needed. Dates on the list are Moscow dates, like the header."""
from __future__ import annotations
import datetime, html, json, re
from pathlib import Path
from ..fetch import get_text
from ..lookup import norm
from .base import Adapter, score_candidate, local_to_utc

DATA = Path(__file__).resolve().parent.parent.parent / "data" / "khl_teams.json"
DAY_LIST = "https://online.khl.ru/online/{date}.html"
ONLINE = {"khl": "https://online.khl.ru/online/{gid}.html",
          "mhl": "https://text.mhl.khl.ru/{gid}.html",
          "vhl": "https://online.vhlru.ru/online/{gid}.html"}
GAME = {"khl": "https://en.khl.ru/game/{tn}/{gid}/protocol/",
        "mhl": "https://engmhl.khl.ru/game/{tn}/{gid}/summary/",
        "vhl": "https://www.vhlru.ru/en/report/{tn}/?idgame={gid}"}
FRONT = {"khl": ("https://en.khl.ru/", r"/teams/en/(\d{4})/"),
         "mhl": ("https://engmhl.khl.ru/", r"/teams/en/(\d{4})/"),
         "vhl": ("https://www.vhlru.ru/en/", r"report/(\d{4})/")}
LEAGUE_NAME = {"khl": "KHL", "mhl": "MHL", "vhl": "VHL"}
SECTION_LEAGUE = (("КХЛ", "khl"), ("МХЛ", "mhl"), ("ВХЛ", "vhl"))

_data = None


def data() -> dict:
    global _data
    if _data is None:
        try:
            _data = json.loads(DATA.read_text())
        except Exception:
            _data = {"teams": {}, "seasons": {}}
    return _data


TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
              ["a", "b", "v", "g", "d", "e", "yo", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t",
               "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya"]))


def translit(name: str) -> str:
    s = re.sub(r"^МХК\s+", "MHK ", name.strip())
    s = re.sub(r"^ХК\s+", "HC ", s)
    out = []
    for ch in s:
        lo = ch.lower()
        if lo in TR:
            t = TR[lo]
            out.append(t.capitalize() if ch != lo and t else t)
        else:
            out.append(ch)
    return "".join(out)


def english(name: str) -> tuple[str, str | None]:
    """(header-style name, league) for a Russian short name from the list."""
    t = data()["teams"].get(name) or data()["teams"].get(name.replace("ё", "е"))
    if t:
        return t["en"], t.get("league")
    return translit(name), None


# Cyrillic letters that look Latin, as typed in some headers ("МHC Spartak Moskva")
HOMOGLYPHS = str.maketrans("АВЕКМНОРСТХаеорсух", "ABEKMHOPCTXaeopcyx")


def hnorm(name: str) -> str:
    return norm(str(name or "").translate(HOMOGLYPHS))


def known_names() -> set:
    return {hnorm(t["en"]) for t in data()["teams"].values()}


def header_league(name: str) -> str | None:
    """League of a header team when its name is one of the mapped names (KHL, MHL or VHL club)."""
    lg = {hnorm(t["en"]): t.get("league") for t in data()["teams"].values()}.get(hnorm(name))
    return lg if lg in ("khl", "mhl", "vhl") else None


def section_league(sec: str) -> str | None:
    for word, lg in SECTION_LEAGUE:
        if sec.startswith(word) or sec.endswith(" " + word):
            return lg
    return None


def parse_day(page: str, date: str) -> list[dict]:
    """Games on one online.khl.ru date page."""
    games, sec = [], ""
    for m in re.finditer(r'<h4>([^<]*)</h4>|<a href="([^"]*)" class="list-group-item[^"]*">\s*<h4 class="list-group-item-heading">(.*?)</h4>\s*(?:<p[^>]*>(.*?)</p>)?', page, re.S):
        if m.group(1) is not None:
            sec = html.unescape(m.group(1)).strip()
            continue
        gid = re.match(r"(\d+)\.html", m.group(2) or "")
        title = re.sub(r"<[^>]+>", " ", html.unescape(m.group(3))).replace("\xa0", " ")
        title = re.sub(r"\s+", " ", title).strip()
        tm = re.match(r"^\d+\.\s*(.+?)\s+-\s+(.+?)(?:\s+(\d+)\s*[–-]\s*(\d+))?(?:\s+(ОТ|Б))?\s*$", title)
        if not tm:
            continue
        note = re.sub(r"<[^>]+>", " ", html.unescape(m.group(4) or "")).strip()
        t = re.search(r"начало в (\d{1,2}:\d\d)", note)
        games.append(dict(sec=sec, gid=gid.group(1) if gid else None, home=tm.group(1).strip(), away=tm.group(2).strip(),
                          score=f"{tm.group(3)}-{tm.group(4)}" if tm.group(3) else None, extra=tm.group(5),
                          time=t.group(1) if t else None, finished="завершен" in note, date=date))
    return games


def season_of(date: str) -> str:
    d = datetime.date.fromisoformat(date)
    return str(d.year if d.month >= 7 else d.year - 1)


async def tournament_id(league: str, date: str) -> int | None:
    """Regular-season tournament id (KHL 1436, MHL 1440, VHL 1430 for 2026-27)."""
    tn = ((data().get("seasons") or {}).get(season_of(date)) or {}).get(league)
    if tn:
        return tn
    if season_of(date) != season_of(datetime.date.today().isoformat()):
        return None
    url, pat = FRONT[league]
    page = await get_text(url, ttl=86400)
    ids = re.findall(pat, page or "")
    return int(max(set(ids), key=ids.count)) if ids else None


class Khl(Adapter):
    name = "khl"
    site = "khl.ru"

    COMP = {"khl": ("kontinental", "khl"), "mhl": ("molodyozh", "molodezh", "mhl"),
            "vhl": ("supreme hockey league", "vhl", "vysshaya hokkeinaya", "higher hockey league")}

    def comp_league(self, parsed) -> str | None:
        c = parsed["comp"].lower()
        if "maritime" in c or "alps" in c:
            return None
        for lg, words in self.COMP.items():
            if any(re.search(r"\b" + re.escape(w) + (r"\b" if len(w) <= 3 else ""), c) for w in words):
                return lg
        return None

    def applies(self, parsed, tm, kb):
        if self.comp_league(parsed):
            return True
        c = parsed["comp"].lower()
        if "russia" in c or "russian" in c or "rhl" in c.split():
            return True
        if "friendl" in c or "cup" in c or "tournament" in c or "pre-season" in c or "preseason" in c:
            kn = known_names()
            if hnorm(parsed["t1"]) in kn or hnorm(parsed["t2"]) in kn:
                return True
            for k in ("t1n", "t2n"):
                b = (tm.get(k) or {}).get("best") or {}
                if re.search(r"khl\.ru|vhlru\.ru|mhl\.khl", f"{b.get('site')} {b.get('schedule')} {b.get('roster')}"):
                    return True
        return False

    async def day(self, date: str) -> list[dict]:
        today = datetime.date.today().isoformat()
        ttl = 600 if date >= today else (3600 * 6 if date >= (datetime.date.today() - datetime.timedelta(days=3)).isoformat() else 86400 * 7)
        page = await get_text(DAY_LIST.format(date=date), ttl=ttl)
        return parse_day(page or "", date)

    async def url_for(self, g: dict, lg_home: str | None, lg_away: str | None) -> tuple[str, str]:
        sec_lg = section_league(g["sec"])
        if sec_lg and "Регулярный чемпионат" in g["sec"]:
            tn = await tournament_id(sec_lg, g["date"])
            if tn:
                return GAME[sec_lg].format(tn=tn, gid=g["gid"]), f"{LEAGUE_NAME[sec_lg]} regular season"
            return ONLINE[sec_lg].format(gid=g["gid"]), f"{LEAGUE_NAME[sec_lg]} {g['sec']}"
        # pre-season cups, friendlies, play-offs: the text broadcast page
        if sec_lg == "mhl" or (sec_lg is None and "khl" not in (lg_home, lg_away) and "mhl" in (lg_home, lg_away)):
            return ONLINE["mhl"].format(gid=g["gid"]), g["sec"]
        if sec_lg == "vhl" and "Плей-офф" in g["sec"]:
            return ONLINE["vhl"].format(gid=g["gid"]), g["sec"]
        return ONLINE["khl"].format(gid=g["gid"]), g["sec"]

    async def candidates(self, parsed, date: str) -> list[dict]:
        want = self.comp_league(parsed)
        out = []
        for g in await self.day(date):
            if not g["gid"]:
                continue
            sec_lg = section_league(g["sec"])
            if want and sec_lg and sec_lg != want:
                continue
            if want and sec_lg is None and "Тренировки" in g["sec"]:
                continue
            home, lgh = english(g["home"])
            away, lga = english(g["away"])
            url, league = await self.url_for(g, lgh, lga)
            score = g["score"] + (" " + ("OT" if g["extra"] == "ОТ" else "SO") if g["extra"] else "") if g["score"] else None
            cand = dict(source=self.name, site=self.site, league=league, home=home, away=away, date=g["date"],
                        score=score, status="final" if g["finished"] else None, kind="protocol", adapter=self.name,
                        url=url, start_utc=local_to_utc(g["date"], g["time"], "Europe/Moscow"),
                        site_names=f"{g['home']} - {g['away']}")
            score_candidate(cand, parsed, "")
            # A KHL club and its MHL junior team share a name once "MHK" is dropped (Dynamo Moskva /
            # MHK Dynamo Moskva): a header team known to play in one league never matches a team of another.
            site_lgs = {lgh, lga} & {"khl", "mhl", "vhl"}
            if lgh and lga and any(header_league(t) and header_league(t) not in site_lgs for t in (parsed["t1"], parsed["t2"])):
                continue
            if min(cand["team_scores"]) >= 0.6:
                out.append(cand)
        return out

    async def find(self, parsed, tm, kb):
        out = await self.candidates(parsed, parsed["date"])
        if not out:
            # the list uses Moscow dates like the header; a neighbour day only when the game was moved,
            # and never as a sure match (MHL/VHL pairs often play on two days in a row)
            d = datetime.date.fromisoformat(parsed["date"])
            for nd in ((d - datetime.timedelta(days=1)).isoformat(), (d + datetime.timedelta(days=1)).isoformat()):
                for c in await self.candidates(parsed, nd):
                    c["confidence"] = min(c["confidence"], 0.8)
                    c["reasons"].append("game is listed on another day than the header")
                    out.append(c)
        return out

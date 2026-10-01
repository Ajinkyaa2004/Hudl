"""onlajny.com (eSports.cz live centre): one hockey page per day (/hokej?date=YYYY-MM-DD)
listing every covered game by league: Czech Extraliga, Maxa liga, Czech youth and regional
leagues, pre-season friendlies, and Slovakia's Tipsport liga and 1. liga. Each game links
/match/index/date/<date>/id/<id> (line-ups, events, stats).
For Slovak games the official report is on hockeyslovakia.sk, which blocks automated
access (Cloudflare); onlajny's page is the reachable alternative."""
from __future__ import annotations
import asyncio, datetime, html, re
from ..fetch import get_text
from .base import Adapter, score_candidate, days_apart, local_to_utc

DAY = "https://www.onlajny.com/hokej?date={date}"
REPORT = "https://www.onlajny.com/match/index/date/{date}/id/{id}"
ITEM_RE = re.compile(r'itemprop="startDate" content="([^"]+)".*?<a class="tymy" href="https://www\.onlajny\.com/match/index/date/[\d-]+/id/(\d+)"[^>]*>'
                     r'\s*<span itemprop="name">\s*([^<]+?)\s*</span>.*?data-field="total">([^<]*)<', re.S)

# league name words -> age group (the site shows club names without it)
AGES = [(r"juniorů|juniorov|junior", "U20"), (r"staršího dorostu|starsiho dorostu|dorastu|dorostu", "U18"),
        (r"mladšího dorostu|mladsiho dorostu", "U16"), (r"\bu(\d{2})\b", None), (r"9\. tříd|starších žáků|starsich ziakov", "U15"),
        (r"mladších žáků|mladsich ziakov", "U13")]
# North American and KHL games also appear (pre-season NHL); their own adapters cover them
SKIP_LEAGUES = re.compile(r"\b(NHL|AHL|KHL|OHL|WHL|QMJHL|NCAA)\b", re.I)
# German / Swiss / Austrian club markers (Club Data's country is often missing or wrong)
FOREIGN_CLUB = re.compile(r"\b(EHC|ERC|EV|ESV|EHF|ECDC|Eispiraten|Roosters|Black Hawks|Lakers|Pinguine|Pinguins|Towers|Fuchse|Falcons|Lowen|"
                          r"Wild Wings|Panther|Haie|Eisbaren|Grizzlys|Adler|Selber|Starbulls|Blue Devils|Dresdner|Kassel|Huskies|Ice Tigers|"
                          r"Straubing|Rapperswil|Zug|Lugano|Ambri|Biel|Bern|Kloten|Langnau|Lausanne|Fribourg|Servette|Olten|Visp|Thurgau|Sierre|"
                          r"Ajoie|Davos|ZSC|Salzburg|Villacher|KAC|Graz|Linz|Vienna|Innsbruck|Vorarlberg|Bolzano|Pustertal|Fehervar|Olimpija|"
                          r"Frolunda|Farjestad|Rogle|Skelleftea|Lulea|Vaxjo|Malmo|Tappara|Ilves|Karpat|KalPa|Lukko|HIFK|Jokerit|Storhamar|Herning)\b", re.I)
CZSK_WORDS = ("extraliga", "tipsport", "tipos", "maxa", "chance", "slovensk", "1. liga", "2. liga", "czech", "slovak", "dorost",
              "junior", "krajsk", "future olympians", "cesk", "česk", "mhl sk")


def league_age(league: str) -> str | None:
    l = league.lower()
    if "mladšího dorostu" in l or "mladsiho dorostu" in l:
        return "U16"
    for pat, age in AGES:
        m = re.search(pat, l)
        if m:
            return age or f"U{m.group(1)}"
    return None


def parse(page: str):
    games = []
    for block in re.split(r'<span class="nazevLiga">', page or "")[1:]:
        league = html.unescape(re.sub(r"<[^>]+>", "", block.split("</span>", 1)[0])).strip()
        for start, gid, name, total in ITEM_RE.findall(block):
            parts = re.split(r"\s+[–-]\s+", html.unescape(name), maxsplit=1)
            if len(parts) != 2:
                continue
            sc = re.fullmatch(r"\s*(\d+):(\d+)\s*", total or "")
            games.append(dict(league=league, gid=gid, date=start[:10], time=start[11:16], home=parts[0].strip(), away=parts[1].strip(),
                              score=f"{sc.group(1)}-{sc.group(2)}" if sc else None))
    return games


def _country(tm, key):
    b = tm[key]["best"] or (tm[key]["candidates"][0] if tm[key]["candidates"] else None)
    return str((b or {}).get("country") or "").lower()


class Onlajny(Adapter):
    name = "onlajny"
    site = "onlajny.com"

    def applies(self, parsed, tm, kb):
        """Slovak leagues, and friendlies / cups. Czech league games are left to the
        ceskyhokej adapter (the official Czech source), so the two never compete."""
        from ..lookup import is_friendly, strip_accents
        c = strip_accents(parsed["comp"]).lower()
        if any(w in c for w in ("tipos extraliga", "slovenska hokejova liga", "tipsport liga (sr)", "1. liga (sr)", "slovak")):
            return True
        # pre-season: Club Data often has no country for Czech and Slovak clubs, so every
        # friendly or tournament reads the day page (two cached pages per date) - except when
        # a side is a German / Swiss / Austrian / Nordic club whose own source already reports the
        # game (del2, deb, sihf, ...): a second strong match would turn their Found into Check.
        # (Club Data's country field is too often empty or wrong to use here.)
        if FOREIGN_CLUB.search(strip_accents(parsed["t1"] + " | " + parsed["t2"])):
            return False
        return is_friendly(parsed["comp"]) or any(w in c for w in ("cup", "pohar", "turnaj", "memorial", "tournament"))

    async def find(self, parsed, tm, kb):
        d = datetime.date.fromisoformat(parsed["date"])
        days = [d.isoformat(), (d - datetime.timedelta(days=1)).isoformat()]
        pages = await asyncio.gather(*[get_text(DAY.format(date=x), ttl=1800) for x in days])
        out, seen = [], set()
        for page in pages:
            for g in parse(page):
                if SKIP_LEAGUES.search(g["league"]):
                    continue
                if g["gid"] in seen or days_apart(parsed["date"], g["date"]) not in (0, 1):
                    continue
                seen.add(g["gid"])
                age = league_age(g["league"])
                ctx = f"{g['league']} {age}" if age else ""
                home, away = g["home"], g["away"]
                if age:
                    home, away = f"{home} {age}", f"{away} {age}"
                cand = dict(source=self.name, site=self.site, league=g["league"], home=home, away=away, date=g["date"], score=g["score"],
                            status=None, kind="protocol", adapter=self.name, url=REPORT.format(date=g["date"], id=g["gid"]),
                            start_utc=local_to_utc(g["date"], g["time"], "Europe/Prague"))
                score_candidate(cand, parsed, ctx)
                if min(cand["team_scores"]) >= 0.6:
                    out.append(cand)
        return out

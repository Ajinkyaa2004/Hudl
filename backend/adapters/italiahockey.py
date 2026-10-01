"""Italy (italia.hockey, FISG): the calendar page's table comes from
/wp-includes/sportform/ajax/getContent.php (POST cmd=calendarMatchTable). With only a season
and a date range it returns every competition's games for those days (IHL, IHL Division 1,
Supercoppa, friendlies, youth), each row with date, time, teams and the /game/<slug>/?id=<n>
report link. The season id is read from the calendar page (hidden input season_id)."""
from __future__ import annotations
import asyncio, datetime, html, json, re, time
import httpx
from .. import fetch
from ..fetch import get_text
from .base import Adapter, score_candidate, days_apart, local_to_utc

CAL = "https://italia.hockey/calendar/"
AJAX = "https://italia.hockey//wp-includes/sportform/ajax/getContent.php"
BASE = "https://italia.hockey/"
_mem: dict = {}

IT_WORDS = ("italian", "italia", "ihl", "serie a", "supercoppa", "super cup", "coppa italia", "fisg")
IT_TOWNS = ("asiago", "varese", "aosta", "fassa", "alleghe", "caldaro", "kaltern", "eppan", "appiano", "cortina", "bolzano", "bozen",
            "merano", "meran", "pustertal", "brunico", "gherdeina", "gardena", "ritten", "renon", "vipiteno", "wipptal", "milano",
            "como", "pergine", "feltre", "valpellice", "pinerolo", "torino", "unterland", "egna", "neumarkt", "dobbiaco", "toblach",
            "bressanone", "brixen", "chiavenna", "trento", "cavalese")


async def post(data: dict, ttl: int = 1800):
    key = AJAX + "?" + "&".join(f"{k}={v}" for k, v in sorted(data.items()))
    hit = _mem.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(headers={"User-Agent": fetch.UA, "Referer": CAL}, timeout=25) as c:
                r = await c.post(AJAX, data=data)
            if r.status_code == 200:
                val = r.json()
                _mem[key] = (time.time(), val)
                return val
        except Exception:
            pass
        await asyncio.sleep(1.5)
    return None


def parse(content: str):
    """Rows of the calendar table: (competition title, date, time, home, away, score, url)."""
    games = []
    parts = re.split(r'(<h2[^>]*class="table__data_title[^"]*"[^>]*>.*?</h2>)', content or "", flags=re.S)
    title = ""
    for part in parts:
        if part.startswith("<h2"):
            title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", part))).strip()
            continue
        for r in re.findall(r"<tr[^>]*>(.*?)</tr>", part, re.S):
            m = re.search(r'href="(?:https://italia\.hockey/)?/?(game/[^"]*?\?id=(\d+))"', r)
            if not m:
                continue
            cells = [c.strip() for c in html.unescape(re.sub(r"<[^>]+>", "|", r)).split("|") if c.strip()]
            d = next((re.search(r"(\d{2})/(\d{2})/(\d{4})", c) for c in cells if re.search(r"\d{2}/\d{2}/\d{4}", c)), None)
            if not d:
                continue
            i = next(k for k, c in enumerate(cells) if re.search(r"\d{2}/\d{2}/\d{4}", c))
            rest = cells[i + 1:]
            tm = rest[0] if rest and re.fullmatch(r"\d{1,2}:\d{2}", rest[0]) else None
            after = rest[1:] if tm else rest
            # cells after the time: arena, town, home, away, score
            k = next((j for j, c in enumerate(after) if re.fullmatch(r"\d+\s*:\s*\d+(?:\s*\w+)?|-\s*:\s*-|vs", c)), None)
            sc = after[k] if k is not None else None
            names = after[:k] if k is not None else after
            if len(names) < 2:
                continue
            home, away = names[-2], names[-1]
            s2 = re.match(r"(\d+)\s*:\s*(\d+)", sc or "")
            games.append(dict(league=title, date=f"{d.group(3)}-{d.group(2)}-{d.group(1)}", time=tm, home=home, away=away,
                              score=f"{s2.group(1)}-{s2.group(2)}" if s2 else None, url=BASE + m.group(1).lstrip("/"), gid=m.group(2)))
    return games


class ItaliaHockey(Adapter):
    name = "italiahockey"
    site = "italia.hockey"

    def applies(self, parsed, tm, kb):
        from ..lookup import is_friendly, strip_accents
        c = strip_accents(parsed["comp"]).lower()
        if any(re.search(r"\b" + re.escape(w) + r"\b", c) for w in IT_WORDS):
            return True
        if is_friendly(parsed["comp"]) or "cup" in c or "torneo" in c or "memorial" in c:
            names = strip_accents(parsed["t1"] + " | " + parsed["t2"]).lower()
            return any(re.search(r"\b" + t, names) for t in IT_TOWNS)
        return False

    async def find(self, parsed, tm, kb):
        cal = await get_text(CAL, ttl=24 * 3600)
        m = re.search(r'id="season_id"\s+value="(\d+)"', cal or "")
        if not m:
            return []
        d = datetime.date.fromisoformat(parsed["date"])
        val = await post({"cmd": "calendarMatchTable", "season_id": m.group(1), "level_group": "", "level_id": "", "phase_id": "",
                          "team_id": "", "date_from": (d - datetime.timedelta(days=1)).isoformat(), "date_to": (d + datetime.timedelta(days=1)).isoformat(), "site_id": ""})
        data = (val or {}).get("data") or {}
        content = data.get("content") if isinstance(data, dict) else data
        out, seen = [], set()
        for g in parse(content if isinstance(content, str) else ""):
            if g["gid"] in seen or days_apart(parsed["date"], g["date"]) not in (0, 1):
                continue
            seen.add(g["gid"])
            ctx = g["league"] if re.search(r"under\s?\d\d|\bu\d\d\b", g["league"], re.I) else ""
            ctx = re.sub(r"under\s?(\d\d)", r"U\1", ctx, flags=re.I)
            cand = dict(source=self.name, site=self.site, league=g["league"] or "Italia", home=g["home"], away=g["away"], date=g["date"],
                        score=g["score"], status=None, kind="protocol", adapter=self.name, url=g["url"],
                        start_utc=local_to_utc(g["date"], g["time"], "Europe/Rome"))
            score_candidate(cand, parsed, ctx)
            if min(cand["team_scores"]) >= 0.6:
                out.append(cand)
        return out

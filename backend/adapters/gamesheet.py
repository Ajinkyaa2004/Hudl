"""GameSheet: one season games page per search, filtered to the game date, in
headless Chromium. Needs a season id: from a gamesheetstats.com link on the
teams or tournament on file, from saved games, or from harvested seasons whose
name matches the competition. Loads are serialised and paced, because the site
challenges rapid automation."""
from __future__ import annotations
import re, datetime, asyncio, difflib
from ..fetch import browser_page
from ..lookup import norm, tokens
from .base import Adapter, score_candidate, days_apart

SEASON_RE = re.compile(r"gamesheetstats\.com/seasons/(\d+)")
PAGE = "https://gamesheetstats.com/seasons/{sid}/games?filter%5Bquery%5D={q}&filter%5Bstart_time_from%5D={d0}&filter%5Bstart_time_to%5D={d1}"
ROWS_JS = """async () => {
  // the games table scrolls inside its own box and only renders the rows in view:
  // scroll that box step by step and collect rows until nothing new appears
  const seen = new Map();
  const grab = () => document.querySelectorAll('[role=row]').forEach(tr => {
    const a = tr.querySelector('a[href*="/games/"]');
    if (a && !seen.has(a.getAttribute('href')))
      seen.set(a.getAttribute('href'), [...tr.querySelectorAll('[role=cell]')].map(td => td.innerText.replace(/\\s+/g,' ').trim()));
  });
  grab();
  const body = document.querySelector('[data-slot=virtual-table-body]');
  let box = body; while (box && box !== document.body && box.scrollHeight <= box.clientHeight + 2) box = box.parentElement;
  for (let i = 0; i < 40 && box; i++) {
    const before = seen.size;
    box.scrollTop += Math.max(200, box.clientHeight * 0.8); window.scrollBy(0, 600);
    await new Promise(r => setTimeout(r, 250)); grab();
    if (seen.size === before && box.scrollTop + box.clientHeight >= box.scrollHeight - 2) break;
  }
  return {title: document.title, rows: [...seen].map(([href, cells]) => ({href, cells}))};
}"""
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
from ..fetch import _LoopSem
_gate = _LoopSem(1)
_TEAM_BASES: dict = {}   # season id -> team name bases, computed once
_last_load = 0.0
NAME_RANGE = re.compile(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})\s*[-–]\s*(?:(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+)?(\d{1,2}),?\s+(\d{4})", re.I)


def name_dates(name: str):
    """'CarShield St Louis Showcase - Aug 28-30, 2026' -> ('2026-08-28', '2026-08-30'). None for league seasons."""
    m = NAME_RANGE.search(name or "")
    if not m:
        return None
    try:
        y = int(m.group(5))
        m1 = MONTHS[m.group(1).lower()[:3]]
        m2 = MONTHS[m.group(3).lower()[:3]] if m.group(3) else m1
        d1, d2 = datetime.date(y, m1, int(m.group(2))), datetime.date(y, m2, int(m.group(4)))
        if d2 < d1:
            d1, d2 = d2, d1   # names with typos such as "August 28-20"
        return d1.isoformat(), d2.isoformat()
    except Exception:
        return None


GENERIC = {"team", "hockey", "club", "academy", "elite", "junior", "juniors", "city", "lady", "ladies", "girls", "boys", "women", "womens",
           "youth", "association", "prep", "school", "high", "varsity", "national", "select", "selects", "white", "black", "blue", "gold",
           "green", "navy", "red", "major", "minor", "united", "north", "south", "east", "west", "saint", "fort", "new"}


def keyword(name: str) -> str:
    """The team's nickname for GameSheet's search box: the last distinctive word of the club
    name ("Phoenix Jr. Coyotes 11U AAA" -> "Coyotes"). Place names are poor search words,
    since GameSheet's division labels contain them too. Ages and levels are left out,
    because GameSheet writes them its own way (U14 vs 14U)."""
    base = re.sub(r"\([^)]*\)", " ", name or "")
    base = re.split(r"\b(?:\d{1,2}U|U\d{1,2}|20[01]\d|AAA|AA|A|B|Tier)\b", base, maxsplit=1, flags=re.I)[0]
    words = [w for w in re.findall(r"[A-Za-z']{3,}", base) if w.lower().strip("'") not in GENERIC]
    if words:
        return words[-1].replace("'", "")
    words = [w for w in re.findall(r"[A-Za-z]{4,}", name or "") if w.lower() not in GENERIC and not re.fullmatch(r"(?i)aaa|aa", w)]
    return max(words, key=len) if words else (re.findall(r"[A-Za-z]{3,}", name or "") or [""])[0]


def parse_date(s: str):
    m = re.search(r"([A-Za-z]{3})[a-z]*\.? (\d{1,2}), (\d{4})", s or "")
    try:
        return datetime.date(int(m.group(3)), MONTHS[m.group(1).lower()[:3]], int(m.group(2))).isoformat() if m else None
    except Exception:
        return None


def clean(n: str) -> str:
    """GameSheet repeats the age group ("Cleveland Barons 16U 16U") and appends the division
    ("Jr Sun Devils 11U Elite 12U" is an 11U team in the 12U division)."""
    n = re.sub(r"\s+", " ", n or "").strip()
    n = re.sub(r"\b(\d{1,2}U)\s+\1\b", r"\1", n)
    n = re.sub(r"\s*-?\s*\b(Pool|Group)\s+\w+\s*$", "", n).strip()
    if len(re.findall(r"\b\d{1,2}U\b|\bU\d{1,2}\b", n, re.I)) >= 2:
        n = re.sub(r"\s+(?:\d{1,2}U|U\d{1,2})\s*$", "", n, flags=re.I)
    return n


def parse_rows(rows):
    out = []
    for r in rows:
        cells = r["cells"]
        d = parse_date(cells[0] if cells else "")
        if not d:
            continue
        # columns: Date, Visitor, Score, Home, Location, #, Type, Watch
        score_i = next((i for i, c in enumerate(cells[1:4], 1) if re.search(r"\d+\s*-\s*\d+", c)), None)
        away = cells[1] if len(cells) > 1 else ""
        home = cells[score_i + 1] if score_i is not None and len(cells) > score_i + 1 else (cells[2] if len(cells) > 2 else "")
        sc = re.search(r"(\d+)\s*-\s*(\d+)", cells[score_i]) if score_i is not None else None
        out.append(dict(date=d, away=clean(away), home=clean(home), score=f"{sc.group(1)}-{sc.group(2)}" if sc else None, href=r["href"]))
    return out


class GameSheet(Adapter):
    name = "GameSheet"
    site = "gamesheetstats.com"
    budget_s = 75   # up to four paced browser loads

    def _seasons(self, parsed, tm, kb, limit: int = 3, weights: dict | None = None):
        """Rank candidate seasons. Evidence, strongest first: a tournament whose name dates
        cover the game date; both header clubs in the season's team list; the season's known
        game dates near the game date; the season name resembling the competition hint.
        Season ids from links on file that are not harvested (usually last season) are only a
        last resort. Club matching ignores age groups (GameSheet writes them differently)."""
        w = dict(both=6, one=4, tour=2, near=1, comp=3, linked=1)   # tuned on the 369 GameSheet games of Aug-Sept 2026
        w.update(weights or {})
        gs = kb.get("gamesheet_seasons") or {}
        year = parsed["date"][:4]
        cb, _, _ = tokens(parsed["comp"])
        clubs = [tokens(parsed["t1"])[0], tokens(parsed["t2"])[0]]

        def club_in_season(club, sid, s):
            bases = _TEAM_BASES.get(sid)
            if bases is None or len(bases) != len(s.get("teams") or {}):
                bases = _TEAM_BASES[sid] = [tokens(t)[0] for t in (s.get("teams") or {})]
            return bool(club and len(club) >= 4 and any(tb and (club in tb or tb in club) for tb in bases))

        linked = []
        for e in [tm["t1n"]["best"], tm["t2n"]["best"], kb["tournaments"].get(parsed["compn"])]:
            for f in ("schedule", "site", "roster", "mhr", "ep"):
                for m in SEASON_RE.finditer((e or {}).get(f) or ""):
                    linked.append(m.group(1))
        for h in kb.get("history", []):
            if h.get("comp") and norm(h["comp"]) == parsed["compn"]:
                for m in SEASON_RE.finditer(h.get("protocol") or ""):
                    linked.append(m.group(1))

        ranked = []
        for sid, s in gs.items():
            if not s.get("name") or s.get("failed") or s.get("old"):
                continue
            if year not in s["name"] and str(int(year) + 1) not in s["name"] and str(int(year) - 1) + "-" not in s["name"]:
                continue
            nd = name_dates(s["name"])
            if nd and not (days_apart(parsed["date"], nd[0]) in (0, 1) or days_apart(parsed["date"], nd[1]) in (0, 1) or nd[0] <= parsed["date"] <= nd[1]):
                continue   # a tournament on other dates
            hits = sum(club_in_season(c, sid, s) for c in clubs)
            near = 0
            if s.get("first") and s.get("last"):
                if s["first"] <= parsed["date"] <= s["last"] or min(days_apart(parsed["date"], s["first"]) or 99, days_apart(parsed["date"], s["last"]) or 99) <= 3:
                    near = 1
            sb, _, _ = tokens(s["name"].split("»")[0])
            comp = 1 if cb and sb and (cb in sb or difflib.SequenceMatcher(None, cb, sb).ratio() >= 0.7) else 0
            if not (hits or comp or (nd and near)):
                continue
            score = (w["both"] if hits == 2 else w["one"] if hits == 1 else 0) + (w["tour"] if nd else 0) + w["near"] * near + w["comp"] * comp + (w["linked"] if sid in linked else 0)
            ranked.append((score, sid))
        ranked.sort(key=lambda x: (-x[0], -int(x[1])))
        ids = [sid for _, sid in ranked[:limit]]
        if not ids:
            ids = [sid for sid in dict.fromkeys(linked) if sid not in gs][:1]
        return ids

    def applies(self, parsed, tm, kb):
        # GameSheet is North American; skip it when a team is known to be from elsewhere
        from ..lookup import comp_country
        from . import team_countries
        na = {"usa", "united states", "us", "canada"}
        cc = str(comp_country(parsed["comp"]) or "").lower()
        if cc and cc not in na:
            return False
        known = team_countries(tm["t1n"]["best"]) | team_countries(tm["t2n"]["best"])
        if known and not (known & na):
            return False
        return bool(self._seasons(parsed, tm, kb))

    async def find(self, parsed, tm, kb):
        """The games page shows only the last day of a date range, so load single days:
        the likely local day first (header dates are UTC), then the header day.
        Search by one club keyword; the age group is checked by the scoring."""
        global _last_load
        import time
        from urllib.parse import quote
        out = []
        days = list(dict.fromkeys([self.preferred_date(parsed), parsed["date"]]))
        kws = list(dict.fromkeys(k for k in (keyword(parsed["t1"]), keyword(parsed["t2"])) if k))
        plan = []
        seasons = self._seasons(parsed, tm, kb, limit=3)
        for sid in seasons[:1]:
            plan += [(sid, d, kws[0]) for d in days]
        for sid in seasons[1:3]:
            plan += [(sid, days[0], kws[0])]
        for sid, day, kw in plan[:4]:   # at most four paced loads within the budget
            async with _gate:
                wait = 6 - (time.time() - _last_load)
                if wait > 0:
                    await asyncio.sleep(wait)
                data = await browser_page(PAGE.format(sid=sid, q=quote(kw), d0=day, d1=day), wait_ms=6000, js=ROWS_JS, ttl=1800, retries=0)
                _last_load = time.time()
            if not data:
                continue
            league = re.sub(r"^Games \| ", "", data.get("title") or "")
            for g in parse_rows(data["rows"]):
                if days_apart(parsed["date"], g["date"]) not in (0, 1):
                    continue
                cand = dict(source=self.name, site=self.site, league=league, home=g["home"], away=g["away"], date=g["date"], score=g["score"],
                            status=None, kind="protocol", adapter=self.name, url="https://gamesheetstats.com" + g["href"].split("?")[0] + "?tab=box-score", rosters="in box score")
                score_candidate(cand, parsed, league)
                if min(cand["team_scores"]) >= 0.6:
                    out.append(cand)
            if any(c["confidence"] >= 0.85 and min(c["team_scores"]) >= 0.8 for c in out):
                return out
        return out

    def preferred_date(self, parsed):
        d = datetime.date.fromisoformat(parsed["date"])
        t = parsed.get("time")
        if not t or t < "12:00":
            return (d - datetime.timedelta(days=1)).isoformat()
        return parsed["date"]

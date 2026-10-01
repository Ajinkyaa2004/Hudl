"""RAMP InterActive tournament sites (<name>.tournamentsgurus.com), e.g. the Eastern Alliance
Kickoff ('EAK Tournament'). Pages are Blazor-rendered, so each division's master schedule is
read in headless Chromium. Division names carry the age group (U16 Red, U15 Blue); the game
page /division/0/<div>/game/view/<id> is the report. Tournaments: data/rampt_tournaments.json."""
from __future__ import annotations
import asyncio, datetime, json, re
from pathlib import Path
from ..fetch import get_text, browser_page
from .base import Adapter, score_candidate, days_apart, local_to_utc

DATA = Path(__file__).resolve().parent.parent.parent / "data" / "rampt_tournaments.json"
HOME = "https://{sub}.tournamentsgurus.com/"
SCHED = "https://{sub}.tournamentsgurus.com/division/0/{div}/masterschedule"
GAME = "https://{sub}.tournamentsgurus.com{href}"
DIV_RE = re.compile(r'aria-controls="assoc-menu-text-(\d+)"><span>([^<]+)</span>')
JS = r'''() => { const out = []; const seen = new Set();
  for (const a of document.querySelectorAll("a[href*='/game/view/']")) {
    const href = a.getAttribute('href'); if (seen.has(href)) continue;
    let el = a, txt = '';
    for (let i = 0; i < 7 && el; i++) { el = el.parentElement; if (!el) break; txt = el.innerText || '';
      if (/\d{1,2}:\d{2}\s*[AP]M/i.test(txt) && txt.split('\n').filter(s => s.trim()).length >= 5) break; }
    seen.add(href); out.push([href, txt.slice(0, 600)]); }
  return out; }'''
DATE_RE = re.compile(r"(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,\s+([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})\s+(\d{1,2}):(\d{2})\s*([ap])\.?m\.?", re.I)
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september",
                                        "october", "november", "december"], 1)}


def load():
    try:
        return {k: v for k, v in json.loads(DATA.read_text()).items() if not k.startswith("_")}
    except Exception:
        return {}


def parse_row(href: str, text: str):
    m = DATE_RE.search(text or "")
    if not m:
        return None
    mon = MONTHS.get(m.group(1).lower())
    if not mon:
        return None
    h = int(m.group(4)) % 12 + (12 if m.group(6).lower() == "p" else 0)
    date = f"{m.group(3)}-{mon:02d}-{int(m.group(2)):02d}"
    rest = [l.strip() for l in text[m.end():].split("\n") if l.strip()]
    # home is the line just before the score (or 'vs'), away the first team line after it;
    # lines such as 'Consolation Game' or 'Semi Final' come before the home team
    k = next((i for i, l in enumerate(rest) if re.fullmatch(r"\d+\s*-\s*\d+|vs\.?|@", l, re.I)), None)
    if not k:
        return None
    status = re.compile(r"final.*|f/(?:ot|so)|scheduled|in progress|results|.*\bperiod\b.*|\d{1,2}:\d{2}.*", re.I)
    after = [l for l in rest[k + 1:] if not status.fullmatch(l)]
    if not after:
        return None
    sc = re.fullmatch(r"(\d+)\s*-\s*(\d+)", rest[k])
    return dict(href=href, date=date, time=f"{h:02d}:{m.group(5)}", home=_expand(rest[k - 1]), away=_expand(after[0]),
                score=f"{sc.group(1)}-{sc.group(2)}" if sc else None)


def _expand(name: str) -> str:
    """Site short forms the headers spell out: 'NJ Rockets' -> 'New Jersey Rockets NJ'."""
    for short, full in (("NJ", "New Jersey"), ("NY", "New York"), ("LI", "Long Island"), ("CT", "Connecticut"), ("HA", "Hockey Academy")):
        if re.search(rf"\b{short}\b", name):
            name = re.sub(rf"\b{short}\b", full, name) + f" {short}"
    return name


class RampTournament(Adapter):
    name = "rampt"
    site = "tournamentsgurus.com"
    budget_s = 75

    def _sub(self, parsed):
        c = parsed["comp"].lower()
        for words, sub in load().items():
            if all(re.search(r"\b" + re.escape(w) + r"\b", c) for w in words.split()):
                return sub
        return None

    def applies(self, parsed, tm, kb):
        return self._sub(parsed) is not None

    async def find(self, parsed, tm, kb):
        from ..lookup import tokens
        sub = self._sub(parsed)
        home = await get_text(HOME.format(sub=sub), ttl=24 * 3600)
        divs = dict(DIV_RE.findall(home or ""))
        ages = {str(a) for a in tokens(parsed["t1"])[1] | tokens(parsed["t2"])[1]}
        pick = {d: n for d, n in divs.items() if not ages or any(re.search(rf"\bU{a}\b|\b{a}U\b", n, re.I) for a in ages)}
        # one page at a time: fetch.py's browser semaphore is created at import time and, on
        # Python 3.9 under asyncio.run, fails when two renders wait on it concurrently
        rows = []
        for d in pick:
            rows.append(await browser_page(SCHED.format(sub=sub, div=d), wait_ms=5000, ttl=3 * 3600, js=JS))
        out, seen = [], set()
        for d, rs in zip(pick, rows):
            dname = pick[d]
            age = re.search(r"\bU(\d{2})\b", dname, re.I)
            for href, text in rs or []:
                if f"/division/0/{d}/" not in href or href in seen:
                    continue
                g = parse_row(href, text)
                if not g or days_apart(parsed["date"], g["date"]) not in (0, 1):
                    continue
                seen.add(href)
                suffix = f" U{age.group(1)}" if age else ""
                cand = dict(source=self.name, site=f"{sub}.tournamentsgurus.com", league=f"{sub} {dname}", home=g["home"] + suffix,
                            away=g["away"] + suffix, date=g["date"], score=g["score"], status=None, kind="protocol", adapter=self.name,
                            url=GAME.format(sub=sub, href=href), start_utc=local_to_utc(g["date"], g["time"], "America/New_York"))
                score_candidate(cand, parsed, f"U{age.group(1)}" if age else "")
                if min(cand["team_scores"]) >= 0.6:
                    out.append(cand)
        return out

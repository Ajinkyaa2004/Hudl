"""stats.swehockey.se: games by date across all districts, plus any swehockey
schedule pages linked from the teams or tournament on file."""
from __future__ import annotations
import re, html
from ..fetch import get_text
from .base import Adapter, score_candidate, days_apart, local_to_utc

BY_DATE = "https://stats.swehockey.se/GamesByDate/{date}/ByTime/null"
ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
SCHED_RE = re.compile(r"stats\.swehockey\.se/ScheduleAndResults/(?:Schedule|Overview)/(\d+)")


def _clean(fragment: str) -> str:
    t = re.sub(r"<[^>]+>", " | ", fragment)
    t = html.unescape(t)
    t = re.sub(r"\s*\|\s*(\|\s*)+", " | ", t)
    return re.sub(r"\s+", " ", t).strip(" |")


def parse_rows(page: str, want_date: str, league: str = "", by_date_page: bool = False):
    """Rows of a games list. On the games-by-date page every row is on want_date.
    On a schedule page the date may sit in the row or in a header row above it,
    so the last date seen is carried forward; a row with no date at all is skipped."""
    out = []
    last_date = None
    for r in ROW_RE.findall(page):
        t = _clean(r)
        d = re.search(r"(\d{4}-\d{2}-\d{2})", t)
        if d:
            last_date = d.group(1)
        if "Game/Events/" not in r:
            continue
        gid = re.search(r"Game/Events/(\d+)", r).group(1)
        # "2024-11-23 14:15 | Home - Away | 2 - 5 | ..."  or "19:00 | Home - Away | 3 - 0 | Arena"
        date = want_date if by_date_page else last_date
        if not date:
            continue
        m = re.search(r"\|\s*([^|]+?)\s+-\s+([^|]+?)\s*\|\s*(\d+)\s*-\s*(\d+)", t) or re.search(r"\|\s*([^|]+?)\s+-\s+([^|]+?)\s*\|", t)
        if not m:
            continue
        home, away = m.group(1).strip(), m.group(2).strip()
        score = f"{m.group(3)}-{m.group(4)}" if m.lastindex and m.lastindex >= 4 else None
        tm_ = re.search(r"(?:^|\|)\s*(?:\d{4}-\d{2}-\d{2}\s+)?(\d{1,2}:\d{2})\s*\|", t)
        out.append(dict(home=home, away=away, date=date, score=score, gid=gid, league=league, time=tm_.group(1) if tm_ else None))
    return out


class SweHockey(Adapter):
    name = "swehockey"
    site = "stats.swehockey.se"

    def _sched_ids(self, parsed, tm, kb):
        ids = []
        entries = [tm["t1n"]["best"], tm["t2n"]["best"], kb["tournaments"].get(parsed["compn"])]
        for e in entries:
            if not e:
                continue
            for f in ("schedule", "site", "roster", "mhr", "ep"):
                for m in SCHED_RE.finditer(e.get(f) or ""):
                    if m.group(1) not in ids:
                        ids.append(m.group(1))
        return ids

    def applies(self, parsed, tm, kb):
        for key in ("t1n", "t2n"):
            b = tm[key]["best"]
            if b and (str(b.get("country") or "").lower() == "sweden" or any("swehockey" in (b.get(f) or "") or "laget.se" in (b.get(f) or "") for f in ("site", "schedule", "roster"))):
                return True
        c = parsed["comp"].lower()
        return any(k in c for k in ("shl", "hockeyallsvenskan", "hockeyettan", "j20 nationell", "j18", "u16 region", "swedish", "sweden")) or bool(self._sched_ids(parsed, tm, kb))

    async def find(self, parsed, tm, kb):
        cands = []
        page = await get_text(BY_DATE.format(date=parsed["date"]), ttl=1800)
        if page:
            for g in parse_rows(page, parsed["date"], by_date_page=True):
                cands.append(g)
        for sid in self._sched_ids(parsed, tm, kb)[:3]:
            page = await get_text(f"https://stats.swehockey.se/ScheduleAndResults/Schedule/{sid}", ttl=1800)
            if not page:
                continue
            title = re.search(r"<title>([^<|]+)", page)
            league = html.unescape(title.group(1)).strip() if title else ""
            for g in parse_rows(page, parsed["date"], league):
                if days_apart(parsed["date"], g["date"]) in (0, 1):
                    cands.append(g)
        out, seen = [], set()
        pending_age = []
        for g in cands:
            if g["gid"] in seen:
                continue
            seen.add(g["gid"])
            cand = dict(source=self.name, site=self.site, league=g["league"], home=g["home"], away=g["away"], date=g["date"],
                        score=g["score"], status=None, url=f"https://stats.swehockey.se/Game/Events/{g['gid']}",
                        rosters="in report (Line Up tab)", kind="protocol", start_utc=local_to_utc(g["date"], g.get("time"), "Europe/Stockholm"))
            score_candidate(cand, parsed, g["league"])
            if min(cand["team_scores"]) >= 0.6:
                out.append(cand)
        # the by-date page shows club names without age groups: read the league line on the
        # game page (e.g. "U18 Regional Syd", "J20 Nationell") before trusting such a match
        import asyncio as _a
        need = [c for c in out if any("age group not shown on site" in r for r in c["reasons"])][:6]
        pages = await _a.gather(*[get_text(c["url"], ttl=6 * 3600) for c in need])
        for c, page in zip(need, pages):
            if not page:
                continue
            txt = _clean(page)
            m = re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*\|?\s*([^|]{3,80})", txt)
            if m:
                c["league"] = m.group(1).strip()
                score_candidate(c, parsed, c["league"])
        return [c for c in out if min(c["team_scores"]) >= 0.6]

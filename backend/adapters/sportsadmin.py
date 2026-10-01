"""stats.sportsadmin.dk (Denmark): per-tournament schedule pages with date, teams and a
game-sheet link. Current tournaments and their game dates are kept in
data/sportsadmin_tournaments.json (refresh with: python -m backend.adapters.sportsadmin)."""
from __future__ import annotations
import re, html, json, asyncio, datetime
from pathlib import Path
from ..fetch import get_text
from .base import Adapter, score_candidate, days_apart, local_to_utc

DATA = Path(__file__).resolve().parent.parent.parent / "data" / "sportsadmin_tournaments.json"
SCHED = "https://stats.sportsadmin.dk/schedule.aspx?TournamentID={tid}"
SHEET = "https://stats.sportsadmin.dk/gamesheet2.aspx?GameId={gid}"


def parse_schedule(page: str):
    name = re.search(r'<(?:h1|h2|h3|span)[^>]*id="[^"]*[Tt]ourn[^"]*"[^>]*>([^<]+)', page)
    games = []
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        m = re.search(r"GameId=(\d+)", r)
        if not m:
            continue
        cells = [c.strip() for c in html.unescape(re.sub(r"<[^>]+>", "|", r)).split("|") if c.strip()]
        d = next((c for c in cells if re.fullmatch(r"\d{2}-\d{2}-\d{4}", c)), None)
        if not d:
            continue
        i = cells.index(d)
        rest = [c for c in cells[i + 1:] if not re.fullmatch(r"\d{1,2}:\d{2}|View|Open|\d+\s*-\s*\d+", c)]
        # rest: arena, home, away
        if len(rest) < 3:
            continue
        sc = next((c for c in cells if re.fullmatch(r"\d+\s*-\s*\d+", c)), None)
        tm_ = next((c for c in cells[i + 1:] if re.fullmatch(r"\d{1,2}:\d{2}", c)), None)
        games.append(dict(gid=m.group(1), date=f"{d[6:]}-{d[3:5]}-{d[:2]}", home=rest[1], away=rest[2], score=sc.replace(" ", "") if sc else None, time=tm_))
    return (name.group(1).strip() if name else ""), games


def load():
    try:
        return json.loads(DATA.read_text())
    except Exception:
        return {}


class SportsAdmin(Adapter):
    name = "sportsadmin"
    site = "stats.sportsadmin.dk"

    def applies(self, parsed, tm, kb):
        c = parsed["comp"].lower()
        if any(k in c for k in ("denmark", "danish", "metal", "u19", "u17", "u16", "friendl", "division")):
            return bool(load())
        for key in ("t1n", "t2n"):
            b = tm[key]["best"] or (tm[key]["candidates"][0] if tm[key]["candidates"] else None)
            if b and str(b.get("country") or "").lower() == "denmark":
                return True
        return False

    async def find(self, parsed, tm, kb):
        tours = load()
        near = [tid for tid, t in tours.items() if any(days_apart(parsed["date"], d) in (0, 1) for d in t.get("dates", []))]
        pages = await asyncio.gather(*[get_text(SCHED.format(tid=tid), ttl=1800) for tid in near[:20]])
        out = []
        for tid, page in zip(near, pages):
            if not page:
                continue
            tname, games = parse_schedule(page)
            for g in games:
                if days_apart(parsed["date"], g["date"]) not in (0, 1):
                    continue
                cand = dict(source=self.name, site=self.site, league=tname or tours[tid].get("name", ""), home=g["home"], away=g["away"], date=g["date"],
                            score=g["score"], status=None, kind="protocol", adapter=self.name, url=SHEET.format(gid=g["gid"]),
                            start_utc=local_to_utc(g["date"], g.get("time"), "Europe/Copenhagen"))
                score_candidate(cand, parsed, cand["league"])
                if min(cand["team_scores"]) >= 0.6:
                    out.append(cand)
        return out


async def harvest(lo=2280, hi=2480, season="2026-2027"):
    import httpx
    from ..fetch import UA
    found = {}
    sem = asyncio.Semaphore(8)
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=25) as c:
        async def one(tid):
            async with sem:
                try:
                    page = (await c.get(SCHED.format(tid=tid))).text
                except Exception:
                    return
                name, games = parse_schedule(page)
                if games and (season in name or any(g["date"] >= season[:4] + "-07-01" for g in games)):
                    found[str(tid)] = dict(name=name, dates=sorted({g["date"] for g in games}), games=len(games))
        await asyncio.gather(*[one(t) for t in range(lo, hi)])
    DATA.write_text(json.dumps(found, ensure_ascii=False, indent=1))
    print(f"{len(found)} current Danish tournaments saved")


if __name__ == "__main__":
    asyncio.run(harvest())

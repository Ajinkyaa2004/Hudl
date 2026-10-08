"""NWAHL, Northwest Amateur Hockey League (nwahl.net): youth league of Washington, Oregon, Idaho,
Montana and Utah. Its public feed lists the whole season, weekend by weekend:
  https://nwahl-portal.onrender.com/api/nwahl/public/schedule  -> entries (team, opponent, home or away,
  age group) each with its games (id, date, local start time, rink, result).
Report: https://www.nwahl.net/game.html?id={id} (box score and line-ups).
Headers name these games "USA Hockey 18U" and the like, so the league is recognised by its clubs: the
team list is kept in data/nwahl_teams.json and refreshed whenever the feed is read."""
from __future__ import annotations
import json, re
from pathlib import Path
from ..fetch import get_json
from .base import Adapter, score_candidate, days_apart, local_to_utc, team_similarity

FEED = "https://nwahl-portal.onrender.com/api/nwahl/public/schedule"
REPORT = "https://www.nwahl.net/game.html?id={id}"
TEAMS = Path(__file__).resolve().parent.parent.parent / "data" / "nwahl_teams.json"
STATE_TZ = {"WA": "America/Los_Angeles", "OR": "America/Los_Angeles", "ID": "America/Boise", "MT": "America/Denver",
            "UT": "America/Denver", "CO": "America/Denver", "WY": "America/Denver", "AK": "America/Anchorage"}


def teams() -> list[str]:
    try:
        return json.loads(TEAMS.read_text())
    except Exception:
        return []


class Nwahl(Adapter):
    name = "nwahl"
    site = "nwahl.net"

    def applies(self, parsed, tm, kb):
        if re.search(r"nwahl|northwest amateur", parsed["comp"], re.I):
            return True
        return any(team_similarity(h, t)[0] >= 0.85 for t in teams() for h in (parsed["t1"], parsed["t2"]))

    async def find(self, parsed, tm, kb):
        data = await get_json(FEED, ttl=3 * 3600, timeout=60)
        entries = (data or {}).get("entries") or []
        names = sorted({e.get(k) for e in entries for k in ("team_name", "opponent_team_name") if e.get(k)})
        if names and names != teams():
            try:
                TEAMS.write_text(json.dumps(names, ensure_ascii=False, indent=0))
            except Exception:
                pass
        out, seen = [], set()
        for e in entries:
            age = (e.get("age_group") or "").upper()
            with_age = lambda n: n if re.search(rf"\b{re.escape(age)}\b", n, re.I) or not age else f"{n} {age}"
            team, opp = with_age(e.get("team_name") or ""), with_age(e.get("opponent_team_name") or "")
            home, away = (team, opp) if e.get("direction") == "home" else (opp, team)
            for g in e.get("games") or []:
                gid, date = str(g.get("id") or ""), g.get("game_date") or ""
                if not gid or gid in seen or days_apart(parsed["date"], date) not in (0, 1):
                    continue
                seen.add(gid)
                tz = g.get("rink_timezone") or e.get("rink_timezone") or STATE_TZ.get(g.get("rink_state") or "", "America/Los_Angeles")
                fin = g.get("final") or {}
                score = f"{fin.get('home_score')}-{fin.get('away_score')}" if fin else None
                cand = dict(source=self.name, site=self.site, league=f"NWAHL {age}", home=home, away=away, date=date,
                            start_utc=local_to_utc(date, (g.get("start_time") or "")[:5] or None, tz), score=score,
                            status="final" if fin else None, kind="protocol", adapter=self.name, url=REPORT.format(id=gid))
                score_candidate(cand, parsed, f"NWAHL {age}")
                if min(cand["team_scores"]) >= 0.6 or max(cand["team_scores"]) >= 0.9:
                    out.append(cand)
        return out

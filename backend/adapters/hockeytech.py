"""HockeyTech leagues (AHL, OHL, WHL, USHL, PWHL): public JSON schedule feed
plus the official game report page."""
from __future__ import annotations
from ..fetch import get_json
from ..lookup import norm
from .base import Adapter, score_candidate, days_apart

LEAGUES = {
    "ahl": dict(key="50c2cd9b5e18e390", name="American Hockey League"),
    "ohl": dict(key="2976319eb44abe94", name="Ontario Hockey League"),
    "whl": dict(key="41b145a848f4bd67", name="Western Hockey League"),
    "ushl": dict(key="e828f89b243dc43f", name="United States Hockey League"),
    "pwhl": dict(key="446521baf8c38984", name="Professional Women's Hockey League"),
    "lhjmq": dict(key="f322673b6bcae299", name="Quebec Maritimes Junior Hockey League"),
    "bchl": dict(key="ca4e9e599d4dae55", name="British Columbia Hockey League"),
    "ajhl": dict(key="cbe60a1d91c44ade", name="Alberta Junior Hockey League"),
    "sjhl": dict(key="2fb5c2e84bf3e4a8", name="Saskatchewan Junior Hockey League"),
    "mjhl": dict(key="f894c324fe5fd8f0", name="Manitoba Junior Hockey League"),
    "ojhl": dict(key="77a0bd73d9d363d3", name="Ontario Junior Hockey League"),
    "gojhl": dict(key="34b10d4d34d7b59a", name="Greater Ontario Hockey League"),
    "cchl": dict(key="b370f3e6c805baf3", name="Central Canada Hockey League"),
    "nojhl": dict(key="c1375ff55168bd71", name="Northern Ontario Junior Hockey League"),
    "sijhl": dict(key="80099326014ed837", name="Superior International Junior Hockey League"),
    "kijhl": dict(key="2589e0f644b1bb71", name="Kootenay International Junior Hockey League"),
    "vijhl": dict(key="4f1a61df18906b61", name="Vancouver Island Junior Hockey League"),
    "mhl": dict(key="4a948e7faf5ee58d", name="Maritime Hockey League"),
}
import json as _json
from pathlib import Path as _Path
_CLIENTS = _Path(__file__).resolve().parent.parent.parent / "data" / "hockeytech_clients.json"
try:
    for _code, _v in _json.loads(_CLIENTS.read_text()).items():
        LEAGUES.setdefault(_code, dict(key=_v["key"], name=_v.get("name") or _code.upper()))
        for _lid, _site in (_v.get("league_ids") or {}).items():   # one client can run several leagues (Hockey Alberta)
            _nice = _site.split(".")[0].upper() if not _site.startswith("www.") else _site.split(".")[1].upper()
            import re as _re
            _nice = _re.sub(r"(U\d{1,2})", r" \1 ", _nice).replace("  ", " ").strip()
            LEAGUES[f"{_code}#{_lid}"] = dict(key=_v["key"], name=_nice, client=_code, league_id=_lid)
except Exception:
    pass
# sites whose /stats/game-center/{id} pages are HockeyTech games, for tests and learning
SITES = {"chl.ca": None, "ushl.com": "ushl", "theahl.com": "ahl"}
try:
    for _code, _v in _json.loads(_CLIENTS.read_text()).items():
        for _s in _v.get("sites", []):
            SITES[_s] = _code
except Exception:
    pass

FEED = "https://lscluster.hockeytech.com/feed/index.php?feed=modulekit&view={view}&key={key}&client_code={code}&lang=en&season_id=&team_id=0&league_code=&fmt=json"
REPORT = "https://lscluster.hockeytech.com/game_reports/official-game-report.php?lang_id=1&client_code={code}&game_id={gid}"


def _utc(iso):
    import datetime
    try:
        return datetime.datetime.fromisoformat(iso).astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M")
    except Exception:
        return None


class HockeyTech(Adapter):
    name = "HockeyTech feed"
    site = "lscluster.hockeytech.com"

    def applies(self, parsed, tm, kb):
        c = parsed["comp"].lower()
        if any(k in c for k in ("ahl", "ohl", "whl", "ushl", "pwhl", "ontario hockey", "western hockey", "american hockey", "united states hockey", "women's hockey")):
            return True
        for key in ("t1n", "t2n"):
            b = tm[key]["best"]
            if b and any("hockeytech" in (b.get(f) or "") or "chl.ca" in (b.get(f) or "") or "theahl.com" in (b.get(f) or "") or "ushl.com" in (b.get(f) or "") for f in ("site", "schedule", "roster")):
                return True
        # cheap: try anyway, the feed is cached per league
        return True

    def preferred_date(self, parsed):
        """Header dates are UTC. A North American evening game lands on the next UTC day,
        so with no time, or a time before noon UTC, the local date is the day before."""
        import datetime
        d = datetime.date.fromisoformat(parsed["date"])
        t = parsed.get("time")
        if not t or t < "12:00":
            return (d - datetime.timedelta(days=1)).isoformat()
        return parsed["date"]

    def _feed(self, view, code, cfg):
        client = cfg.get("client", code)
        url = FEED.format(view=view, key=cfg["key"], code=client)
        return url + (f"&league_id={cfg['league_id']}" if cfg.get("league_id") else "")

    async def _seasons(self, code, cfg, year):
        """Season ids that belong to the current year: regular season, pre-season, playoffs."""
        data = await get_json(self._feed("seasons", code, cfg), ttl=24 * 3600, timeout=30)
        seasons = ((data or {}).get("SiteKit") or {}).get("Seasons") or []
        ids = [(s["season_id"], s.get("season_name", "")) for s in seasons if str(year) in s.get("season_name", "") or f"{year-1}-{str(year)[2:]}" in s.get("season_name", "")]
        return ids[:14] or [("", "")]

    async def _league_games(self, code, cfg, year):
        games = []
        for sid, sname in await self._seasons(code, cfg, year):
            data = await get_json(self._feed("schedule", code, cfg).replace("season_id=&", f"season_id={sid}&"), ttl=6 * 3600, timeout=40)
            for g in ((data or {}).get("SiteKit") or {}).get("Schedule") or []:
                g["_season"] = sname
                games.append(g)
        return code, cfg, games

    async def find(self, parsed, tm, kb):
        import asyncio
        out = []
        year = int(parsed["date"][:4])
        results = await asyncio.gather(*[self._league_games(code, cfg, year) for code, cfg in LEAGUES.items()], return_exceptions=True)
        for res in results:
            if isinstance(res, Exception):
                continue
            code, cfg, games = res
            for g in games:
                if days_apart(parsed["date"], g.get("date_played", "")) not in (0, 1):
                    continue
                import re as _re
                league = _re.sub(r"(U\d{1,2})(?=[A-Za-z])", r"\1 ", f"{cfg['name']} {g.get('_season', '')}".strip())   # U18AA -> U18 AA
                cand = dict(source=self.name, site=self.site, league=league,
                            home=g.get("home_team_name", ""), away=g.get("visiting_team_name", ""),
                            date=g.get("date_played"), score=f"{g.get('home_goal_count')}-{g.get('visiting_goal_count')}" if g.get("final") == "1" or g.get("game_status") == "Final" else None,
                            status=g.get("game_status"), url=REPORT.format(code=cfg.get("client", code), gid=g["game_id"]),
                            rosters="in report", kind="protocol", adapter=self.name, start_utc=_utc(g.get("GameDateISO8601")))
                score_candidate(cand, parsed, league)
                if min(cand["team_scores"]) >= 0.6:
                    out.append(cand)
        return out

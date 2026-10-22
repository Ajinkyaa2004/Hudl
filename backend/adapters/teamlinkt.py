"""TeamLinkt league and tournament sites (leagues.teamlinkt.com: the Thanksgiving Hockey Classic,
Alberta female leagues, ...). A site's games come from its open schedule list
(POST /leagues/getAllEvents/{association}, the same call its Scores page makes): date, teams, score
and the start as a timestamp. Report: https://leagues.teamlinkt.com/leagues/event/{association}/{game}.

Sites are learned from analysts' TeamLinkt links in the RTT sheet (discover(), run by learn_sheet):
data/teamlinkt.json keeps each site's season, its game dates and its teams' divisions."""
from __future__ import annotations
import datetime, html, json, re
from pathlib import Path
from ..fetch import get_text
from .base import Adapter, score_candidate

DATA = Path(__file__).resolve().parent.parent.parent / "data" / "teamlinkt.json"
BASE = "https://leagues.teamlinkt.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0.0.0 Safari/537.36")
LINK = re.compile(r"teamlinkt\.com/[Ll]eagues/event/(\d+)/(\d+)")


def _cfg() -> list[dict]:
    try:
        return json.loads(DATA.read_text()).get("sites", [])
    except Exception:
        return []


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _team(cell: str, divisions: dict) -> tuple[str, str | None]:
    """'<a href="/leagues/team/25869/907675">East Kootenay Avalanche</a> (5)' -> ('East Kootenay
    Avalanche U13 AA', '5'): the division's age and level are added, as headers carry them."""
    tid = re.search(r"/leagues/team/\d+/(\d+)", cell or "")
    score = re.search(r"\((\d+)\)\s*$", _text(cell))
    name = re.sub(r"\s*\(\d+\)\s*$", "", _text(cell))
    div = divisions.get(tid.group(1)) if tid else None
    if div:
        m = re.match(r"\s*U(\d{1,2})\s*(AAA|AA|A|B)?", div, re.I)
        if m:
            name = f"{name} U{m.group(1)} {(m.group(2) or '').upper()}".strip()
        if re.search(r"female|girls", div, re.I) and not re.search(r"female|girls|women", name, re.I):
            name += " Female"
    return name, score.group(1) if score else None


async def _events(client, site: dict) -> list[dict]:
    from .. import fetch
    key = ("teamlinkt", site["assoc"], site["season"])
    hit = fetch._cached(key, 1800)
    if hit is not None:
        return hit
    r = await client.post(f"{BASE}/leagues/getAllEvents/{site['assoc']}", data=dict(
        draw=1, start=0, length=2000, season_id=site["season"], status="all", type="scores", is_league_site=1,
        show_team_links=1, show_games_only=1, team_id="all"), headers={"X-Requested-With": "XMLHttpRequest", "User-Agent": UA})
    rows = (r.json() or {}).get("data") or [] if r.status_code == 200 else []
    fetch._store(key, rows)
    return rows


class TeamLinkt(Adapter):
    name = "teamlinkt"
    site = "leagues.teamlinkt.com"

    def _sites(self, parsed):
        d = datetime.date.fromisoformat(parsed["date"])
        out = []
        for s in _cfg():
            try:
                first = datetime.date.fromisoformat(s["first"]) - datetime.timedelta(days=1)
                last = datetime.date.fromisoformat(s["last"]) + datetime.timedelta(days=1)
            except Exception:
                continue
            if first <= d <= last:
                out.append(s)
        return out

    def applies(self, parsed, tm, kb):
        return bool(self._sites(parsed))

    async def find(self, parsed, tm, kb):
        import httpx
        out = []
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            for s in self._sites(parsed):
                try:
                    rows = await _events(client, s)
                except Exception:
                    continue
                for r in rows:
                    gid = LINK.search(str(r.get("2") or ""))
                    ts = r.get("6")
                    if not gid or not isinstance(ts, (int, float)):
                        continue
                    start = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
                    local = start + datetime.timedelta(hours=s.get("utc_offset", -6))
                    home, hs = _team(r.get("3"), s.get("divisions") or {})
                    away, as_ = _team(r.get("4"), s.get("divisions") or {})
                    cand = dict(source=self.name, site=self.site, league=s.get("name") or "", home=home, away=away,
                                date=local.date().isoformat(), score=f"{hs}-{as_}" if hs and as_ else None,
                                status="final" if hs and as_ else None, kind="protocol", adapter=self.name,
                                url=f"{BASE}/leagues/event/{s['assoc']}/{gid.group(2)}", start_utc=start.strftime("%Y-%m-%dT%H:%M"))
                    score_candidate(cand, parsed, cand["league"])
                    if min(cand["team_scores"]) >= 0.6:
                        out.append(cand)
        return out


def discover(url: str, log=print) -> dict | None:
    """A TeamLinkt game link -> its site's entry (association, season, dates, divisions), saved to
    data/teamlinkt.json. Known sites are refreshed (new games, dates)."""
    import httpx
    m = LINK.search(url or "")
    if not m:
        return None
    assoc, game = m.group(1), m.group(2)
    with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": UA}) as c:
        page = c.get(f"{BASE}/leagues/event/{assoc}/{game}").text
        slug = re.search(r'href="/([a-z0-9-]+)/Scores"', page)
        if not slug:
            log(f"teamlinkt {assoc}: no Scores page")
            return None
        scores = c.get(f"{BASE}/{slug.group(1)}/Scores").text
        season = re.search(r'<option value="(\d+)" selected="selected">([^<]*)', scores)
        if not season:
            return None
        # the team filter names each team with its division: 'Bow River Bruins (U13AA Co-Ed)'
        divisions = dict(re.findall(r'<option value="(\d+)">[^<(]+\(([^)]+)\)', scores))
        r = c.post(f"{BASE}/leagues/getAllEvents/{assoc}", data=dict(
            draw=1, start=0, length=2000, season_id=season.group(1), status="all", type="scores", is_league_site=1,
            show_team_links=1, show_games_only=1, team_id="all"), headers={"X-Requested-With": "XMLHttpRequest"})
        rows = (r.json() or {}).get("data") or []
    stamps = [x["6"] for x in rows if isinstance(x.get("6"), (int, float))]
    if not stamps:
        return None
    # the site shows local clock times beside UTC timestamps: their difference is the time zone
    offset = -6
    for x in rows:
        t = re.match(r"\s*(\d{1,2}):(\d{2})\s*([AP]M)", str(x.get("1") or ""))
        if t and isinstance(x.get("6"), (int, float)):
            h = int(t.group(1)) % 12 + (12 if t.group(3) == "PM" else 0)
            utc = datetime.datetime.fromtimestamp(x["6"], datetime.timezone.utc)
            offset = round(((h * 60 + int(t.group(2))) - (utc.hour * 60 + utc.minute)) / 60)
            offset = offset - 24 if offset > 12 else offset + 24 if offset < -12 else offset
            break
    days = sorted(datetime.datetime.fromtimestamp(t + offset * 3600, datetime.timezone.utc).date().isoformat() for t in stamps)
    entry = dict(assoc=assoc, slug=slug.group(1), season=season.group(1), name=html.unescape(season.group(2)).strip(),
                 utc_offset=offset, first=days[0], last=days[-1], games=len(stamps), divisions=divisions)
    try:
        cfg = json.loads(DATA.read_text())
    except Exception:
        cfg = {"_about": "TeamLinkt sites, learned from analysts' TeamLinkt links (backend.adapters.teamlinkt.discover).", "sites": []}
    cfg["sites"] = [s for s in cfg["sites"] if not (s["assoc"] == assoc and s["season"] == entry["season"])] + [entry]
    DATA.write_text(json.dumps(cfg, indent=1, ensure_ascii=False))
    log(f"teamlinkt {assoc} {entry['name']}: {entry['games']} games {entry['first']}..{entry['last']}, UTC{offset:+d}")
    return entry

"""Current-season (2026-27) rosters for one team, from sources that answer plain HTTP.

When no match report exists the analyst attaches both teams' current rosters. This module
finds them, best first:

  EliteProspects   gql.eliteprospects.com, the public GraphQL API the EP web app itself calls.
                   The www pages and api.eliteprospects.com sit behind a Cloudflare challenge;
                   the GraphQL host does not. A GET with `apollo-require-preflight: true`
                   (Apollo's CSRF rule for GET queries) answers:
                     team(id){name slug league{name} ...}
                     teamRoster(id, season:"2026-2027", limit){totalCount edges{jerseyNumber player{name yearOfBirth position}}}
                     teams(q:"...", limit){edges{id name fullName status league{name}}}
                   Team ids come from Club Data EP links, else from a name search.
  GameSheet        gamesheetstats.com/api/teams/{season_id}/{team_id}/roster  (players, goalies, coaches,
                   jersey, birthdate) and /api/teams/{season_id}/{team_id} (team, season dates).
                   Team ids: the game hint, a Club Data link in a current season, or a name match
                   against data/gamesheet_games.json (every game of every current season).
  HockeyTech       lscluster feed: tab=gamesummary (season_id, team ids of a game), view=roster
                   (season_id, team_id). Team found from the game hint or games.db HockeyTech games.
  TimeToScore      stats.<host>.timetoscore.com/display-stats.php?league=N (standings with team
                   links) -> display-schedule?team=&season=&league=&stat_class=1 (players who have
                   played this season, with numbers). Leagues from kb["tts_team_leagues"].
  Fallback         the existing check (backend/rosters.py) of the Club Data roster / MHR / site links,
                   run only when none of the above gave a verified current roster.

    rosters = await find_rosters("Long Island Royals 13U AAA", "2026-09-20", {"team": club_data_entry})
    -> [dict(url, source, season, players=[{number, name, pos, birth_year}], note,
             status, team, league, age_fit, match), ...]

status: "verified" (this season, 10+ players, ages fit or unknown), "wrong age group",
"few players" (this season, under 10), "old season", "probable"/"check" (fallback pages).
"""
from __future__ import annotations
import asyncio, datetime, json, re, statistics, time
from pathlib import Path
from urllib.parse import quote

from .fetch import get_text, get_json
from .lookup import tokens, norm, domain, load_kb
from .adapters.base import team_similarity

DATA = Path(__file__).resolve().parent.parent / "data"
UA_JSON = {"Accept": "application/json", "Referer": "https://gamesheetstats.com/"}
MIN_MATCH = 0.85          # name match needed for a team found by name (not by id)
TTL = 6 * 3600

SOURCE_RANK = {"GameSheet": 0, "HockeyTech": 0, "TimeToScore": 0, "EliteProspects": 1}


# ---------------------------------------------------------------- season and age helpers

def season_start(date: str) -> int:
    d = datetime.date.fromisoformat(date)
    return d.year if d.month >= 7 else d.year - 1


def season_label(y: int) -> str:
    return f"{y}-{str(y + 1)[2:]}"


def age_fit(players: list[dict], team_name: str, date: str) -> str:
    """'fits', 'does not fit (players born around 2012)' or 'unknown', from birth years."""
    cur = season_start(date)
    births = [p["birth_year"] for p in players if p.get("birth_year") and cur - 45 <= p["birth_year"] <= cur - 4]
    ages = tokens(team_name)[1]
    if len(births) < 5 or not ages:
        return "unknown"
    want = sorted(ages)[0]
    med = statistics.median(births)
    if len(want) <= 2:
        age = cur + 1 - med
        ok = int(want) - 3 <= age <= int(want) + 1
    else:
        ok = abs(med - int(want)) <= 1
    return "fits" if ok else f"does not fit (players born around {int(med)})"


def finish(r: dict, team_name: str, date: str) -> dict:
    """Status from season, player count and age fit."""
    cur = season_label(season_start(date))
    n = len(r.get("players") or [])
    r.setdefault("age_fit", age_fit(r.get("players") or [], team_name, date))
    if r.get("season") != cur:
        r["status"] = "old season"
    elif r["age_fit"].startswith("does not"):
        r["status"] = "wrong age group"
    elif n > 40:
        r["status"] = "check"
        r["note"] += "; too many players for one team (camp, preseason or a whole club)"
    elif n >= 10:
        r["status"] = "verified"
    else:
        r["status"] = "few players"
    return r


STATUS_ORDER = {"verified": 0, "probable": 1, "wrong age group": 2, "few players": 3, "check": 4, "old season": 5}


BY_ORDER = {"game": 0, "confirmed": 1, "id": 2, "name": 3}


def rank(r: dict) -> tuple:
    """Status first; then how the team was identified: from the game itself, confirmed by two sources,
    by an id on file (Club Data link), by name only; then name score and source."""
    by = "confirmed" if r.get("confirmed") and r.get("by") != "game" else r.get("by")
    return (STATUS_ORDER.get(r.get("status"), 6), BY_ORDER.get(by, 4), -round(r.get("match", 0), 1),
            SOURCE_RANK.get(r.get("source"), 2), -len(r.get("players") or []))


def _pkey(name: str) -> str:
    from .lookup import strip_accents
    parts = re.findall(r"[a-z]+", strip_accents(str(name or "")).lower())
    return (parts[-1] + parts[0][:1]) if len(parts) > 1 else (parts[0] if parts else "")


def overlap(a: dict, b: dict) -> float:
    """Share of a's players also on b (last name + first initial)."""
    pa = [_pkey(p.get("name")) for p in a.get("players") or []]
    pb = {_pkey(p.get("name")) for p in b.get("players") or []}
    return sum(1 for x in pa if x in pb) / max(1, len(pa))


def cross_check(out: list[dict]) -> None:
    """Teams found by name are checked against teams found by id (Club Data link) or from the game:
    the same players confirm both; different players mean another team of the same club. Without such a
    reference, two name matches with different players are flagged as ambiguous."""
    refs = [r for r in out if r.get("by") in ("id", "game") and r["status"] == "verified"]
    named = [r for r in out if r.get("by") == "name" and r["status"] == "verified"]
    for r in named:
        if refs:
            ov = max(overlap(r, x) for x in refs)
            if ov >= 0.5:
                r["confirmed"] = True
                for x in refs:
                    if overlap(r, x) >= 0.5:
                        x["confirmed"] = True
                r["note"] += f"; {ov:.0%} of its players are on the {refs[0]['source']} roster found by id"
            elif ov < 0.2:
                r["status"] = "check"
                r["note"] += f"; players differ from the {refs[0]['source']} roster found by id: probably another team of the club"
    if not refs and len(named) >= 2:
        top = max(named, key=lambda r: r.get("match", 0))
        for r in named:
            if r is not top and r.get("match", 0) >= top.get("match", 0) - 0.05 and overlap(top, r) < 0.3 and overlap(r, top) < 0.3:
                for x in (top, r):
                    x["status"] = "check"
                    if "ambiguous" not in x["note"]:
                        x["note"] += "; ambiguous: another team of the same name has different players"


def _pos(p: str | None) -> str | None:
    p = (p or "").lower()
    if not p:
        return None
    if p.startswith("g"):
        return "G"
    if p.startswith("d"):
        return "D"
    return "F" if p[0] in "fclrw" else p.upper()[:2]


def _int(x) -> int | None:
    try:
        return int(str(x).strip())
    except Exception:
        return None


def ctx(text: str) -> str:
    """League or division text in a form the age parser reads: 'U16AA' -> 'U16 AA', '16UAAA' -> '16U AAA'."""
    t = re.sub(r"(?i)\b(u\d{1,2}|\d{1,2}u)(?=[a-z])", r"\1 ", text or "")
    return re.sub(r"(?i)\b(\d{1,2})(?=(aaa|aa|a|bb|b)\b)", r"\1U ", t)


LEVEL_WORD = re.compile(r"(?i)(?<![\w-])(AAA|AA|A|BB|B|AE)(?:\d)?(?![\w-])")


def levels(text: str) -> set:
    return {x.upper() for x in LEVEL_WORD.findall(text or "")}


def pick_context(division: str, season_name: str = "") -> str:
    """The division names the team's own age group; the season name only when the division has none
    ('Early Bird U16/U18' would confirm both ages)."""
    d = ctx(division)
    return d if tokens(d)[1] or not season_name else f"{d} {ctx(season_name)}"


ADULT = re.compile(r"(?i)adult|\brec\b|recreational|beer|\bover\s?[2-9]\d|\b[2-9]\d\s?(\+|&\s?over|and over)|masters|old.?timers|"
                   r"men'?s league|co-?ed|drop.?in|travell?ers|senior men|\bmen\b|women'?s league|\bnhl\b|\bahl\b|test\b")
FEMALE = re.compile(r"(?i)girl|female|women|\blad(y|ies)\b|\(g\)|\(w\)|ettes\b|\bqueens\b|\bwild\s?cats?\b|\b(19u|u19|u22|22u)\b")


def name_score(team_name: str, site_name: str, division: str = "", season_name: str = "") -> float:
    """team_similarity plus rules for teams found by name only:
    - level: 'AA' in the header and 'A5' in the division are different teams;
    - age: a youth header needs an age on the site (name, division or season); a header without an age
      does not take a youth team;
    - adult and pro leagues ('NHL Preseason', '40&Over', 'Adult League') are never a header team's roster;
    - a girls' league for a header that shows no sign of being a girls' team is another team;
    - a one-word site name ('Titans') is not enough."""
    context = pick_context(division, season_name)
    site_name = ctx(site_name)                          # 'Waterloo Ravens U18AA' -> 'Waterloo Ravens U18 AA'
    s, _ = team_similarity(team_name, site_name, context)
    season_name = (season_name or "").split("»")[0]      # the association after » is not the league ("... » Independent Adult")
    if ADULT.search(f"{division} {season_name} {site_name}") and not ADULT.search(team_name):
        return 0.0
    ha = tokens(team_name)[1]
    sa = tokens(ctx(site_name))[1] | tokens(context)[1]
    if ha and not sa:
        s *= 0.9
    elif sa and not ha:
        s = min(s, 0.6)
    if FEMALE.search(f"{site_name} {division} {season_name}") and not FEMALE.search(team_name):
        s *= 0.8
    hw, sw = _words(team_name), _words(site_name)
    if len(hw) >= 2 and len(hw & sw) < 2 and s < 0.95:
        s = min(s, 0.8)
    hl = levels(re.sub(r"(?i)\b(u|j)?\d{1,2}u?\b|\b20[01]\d\b", " ", team_name))
    sl = levels(re.sub(r"(?i)\b(group|pool|div(ision)?|flight|bracket|conf(erence)?|section)\s+\w\b", " ", f"{site_name} {division}"))
    if hl and sl and not (hl & sl):
        return s * 0.5
    return s


def _best_name(team_name: str, names: list[tuple], context_idx: int | None = None) -> list[tuple]:
    """names: tuples whose [0] is a site team name (and [context_idx] a league/division context).
    Returns [(score, tuple)] with score >= MIN_MATCH, best first."""
    out = []
    for t in names:
        s = name_score(team_name, t[0], t[context_idx] if context_idx is not None else "")
        if s >= MIN_MATCH:
            out.append((s, t))
    out.sort(key=lambda x: -x[0])
    return out


_WORD = re.compile(r"[a-z]{3,}")
_SKIP_WORDS = {"hockey", "club", "team", "junior", "the", "elite", "academy", "jr", "aaa", "girls", "boys", "white", "black",
               "blue", "gold", "red", "navy", "green", "silver", "orange", "grey", "gray", "select", "premier"}


def _words(name: str) -> set:
    from .lookup import strip_accents
    return set(_WORD.findall(strip_accents(str(name or "")).lower())) - _SKIP_WORDS


# ---------------------------------------------------------------- EliteProspects (GraphQL)

EP_GQL = "https://gql.eliteprospects.com/"
EP_HEADERS = {"apollo-require-preflight": "true", "Accept": "application/json",
              "Origin": "https://www.eliteprospects.com", "Referer": "https://www.eliteprospects.com/"}
EP_TEAM = re.compile(r"eliteprospects\.com/team/(\d+)")
_ep_sem = None
_ep_loop = None


async def ep_query(q: str, ttl: int = TTL) -> dict | None:
    global _ep_sem, _ep_loop
    loop = asyncio.get_running_loop()
    if _ep_sem is None or _ep_loop is not loop:
        _ep_sem, _ep_loop = asyncio.Semaphore(2), loop
    async with _ep_sem:
        t = await get_text(EP_GQL + "?query=" + quote(q), ttl=ttl, timeout=25, headers=EP_HEADERS)
    try:
        d = json.loads(t) if t else None
    except ValueError:
        return None
    return (d or {}).get("data")


async def ep_roster(team_id: str, date: str) -> dict | None:
    y = season_start(date)
    season = f"{y}-{y + 1}"
    q = ('{ team(id: %s) { name fullName slug status league { name } country { name } } '
         'teamRoster(id: %s, season: "%s", limit: 150) { totalCount edges { jerseyNumber player { name yearOfBirth position } } } }'
         % (team_id, team_id, season))
    d = await ep_query(q)
    if not d or not d.get("team"):
        return None
    t = d["team"]
    edges = ((d.get("teamRoster") or {}).get("edges")) or []
    players = [dict(number=e.get("jerseyNumber"), name=(e.get("player") or {}).get("name"), pos=_pos((e.get("player") or {}).get("position")),
                    birth_year=(e.get("player") or {}).get("yearOfBirth")) for e in edges if (e.get("player") or {}).get("name")]
    url = f"https://www.eliteprospects.com/team/{team_id}/{t.get('slug') or 'team'}/{season}"
    league = (t.get("league") or {}).get("name") or ""
    return dict(url=url, source="EliteProspects", season=season_label(y), players=players, team=t.get("name"),
                league=league, ep_id=str(team_id),
                note=f"EliteProspects {season_label(y)} roster, {len(players)} players" + (f", {league}" if league else ""))


def _ep_query_words(team_name: str) -> str:
    s = re.sub(r"\b(?:u|j)\s?\d{1,2}\b|\b\d{1,2}\s?u\b|\b20[01]\d\b|\b(aaa|aa|a|jr\.?|tier\s?\d)\b", " ", team_name, flags=re.I)
    return re.sub(r"[^\w\s'\-]", " ", s).strip()


async def ep_search(team_name: str, limit: int = 2) -> list[tuple[float, str, str]]:
    """(score, team id, EP name) of EP teams whose name matches, best first."""
    q = _ep_query_words(team_name)
    if len(q) < 3:
        return []
    d = await ep_query('{ teams(q: %s, limit: 40) { edges { id name fullName status league { name } } } }' % json.dumps(q), ttl=24 * 3600)
    out = []
    for e in ((d or {}).get("teams") or {}).get("edges") or []:
        if e.get("status") == "not-active":
            continue
        c = ctx((e.get("league") or {}).get("name") or "")
        s = max(team_similarity(team_name, e.get("name") or "", c)[0], team_similarity(team_name, e.get("fullName") or "", c)[0])
        if s >= MIN_MATCH:
            out.append((s, str(e["id"]), e.get("name")))
    out.sort(key=lambda x: -x[0])
    return out[:limit]


async def from_ep(team_name: str, date: str, team: dict) -> list[dict]:
    ids = []
    for k in ("ep", "roster", "site", "mhr"):
        m = EP_TEAM.search(team.get(k) or "")
        if m and m.group(1) not in [i for i, _ in ids]:
            ids.append((m.group(1), 1.0))
    how = "EliteProspects link in Club Data"
    if not ids:
        ids = [(i, s) for s, i, _ in await ep_search(team_name)]
        how = "found by name on EliteProspects"
    res = await asyncio.gather(*[ep_roster(i, date) for i, _ in ids[:2]], return_exceptions=True)
    out = []
    for (i, s), r in zip(ids, res):
        if isinstance(r, dict):
            r["match"] = s
            r["by"] = "id" if how.startswith("EliteProspects link") else "name"
            r["note"] += f" ({how})"
            out.append(r)
    return out


# ---------------------------------------------------------------- GameSheet

GS = "https://gamesheetstats.com"
GS_TEAM_LINK = re.compile(r"gamesheetstats\.com/seasons/(\d+)/teams/(\d+)")
GS_GAME_LINK = re.compile(r"gamesheetstats\.com/seasons/(\d+)/games/(\d+)")
_gs_index = None


def gs_index() -> dict:
    """From data/gamesheet_games.json: teams {(sid, tid): (name, division, season_name)},
    games {game_id: game}, seasons, and a word index for name matching."""
    global _gs_index
    if _gs_index is None:
        try:
            d = json.loads((DATA / "gamesheet_games.json").read_text())
        except Exception:
            d = dict(games=[], seasons={})
        teams, games, words = {}, {}, {}
        for g in d.get("games") or []:
            games[g["game_id"]] = g
            for side, div in (("home", g.get("division")), ("away", g.get("away_division") or g.get("division"))):
                tid = g.get(f"{side}_id")
                if tid and (g["sid"], tid) not in teams:
                    teams[(g["sid"], tid)] = (g[side], div or "", g.get("season_name") or "")
                    for w in _words(g[side]):
                        words.setdefault(w, set()).add((g["sid"], tid))
        _gs_index = dict(teams=teams, games=games, words=words, seasons=d.get("seasons") or {})
    return _gs_index


async def gs_json(path: str, ttl: int = TTL):
    t = await get_text(GS + path, ttl=ttl, timeout=30, headers=UA_JSON)
    try:
        return json.loads(t) if t else None
    except ValueError:
        return None       # an HTML challenge page instead of JSON


async def gs_roster(sid: str, tid: str, date: str) -> dict | None:
    info, ros = await asyncio.gather(gs_json(f"/api/teams/{sid}/{tid}"), gs_json(f"/api/teams/{sid}/{tid}/roster"))
    if not ros:
        return None
    t = ((info or {}).get("data") or [{}])[0] if (info or {}).get("data") else {}
    season = t.get("season") or {}
    players = []
    for key in ("players", "goalies"):
        for p in ((ros.get(key) or {}).get("data")) or []:
            name = " ".join(x for x in (p.get("firstName"), p.get("lastName")) if x).title()
            b = (p.get("birthdate") or "")[:4]
            pos = "G" if key == "goalies" else _pos((p.get("positions") or [None])[0])
            players.append(dict(number=p.get("jersey"), name=name, pos=pos, birth_year=int(b) if b.isdigit() else None))
    cur = season_start(date)
    s0, s1 = season.get("startDate") or "", season.get("endDate") or ""
    lo, hi = f"{cur}-07-01", f"{cur + 1}-06-30"
    idx = gs_index()["seasons"].get(str(sid)) or {}
    if not s0:
        s0, s1 = idx.get("start") or "", idx.get("end") or ""
    # a spring or summer 2026 league is last season; showcases from June on already use the new teams
    current = bool(s0 and f"{cur}-06-01" <= s0 <= hi) or bool(not s0 and s1 and s1 >= f"{cur}-08-01")
    title = season.get("title") or idx.get("name") or ""
    div = (t.get("division") or {}).get("title") or ""
    return dict(url=f"{GS}/seasons/{sid}/teams/{tid}/roster", source="GameSheet", season=season_label(cur) if current else (s0[:4] and season_label(int(s0[:4]))),
                players=players, team=t.get("title"), league=" | ".join(x for x in (title, div) if x),
                note=f"GameSheet roster, {len(players)} players, {title}" + (f" ({div})" if div else ""), gs=(str(sid), str(tid)))


async def gs_game_teams(sid: str, gid: str) -> list[tuple[str, str, str]]:
    """[(team name, sid, team id)] of both sides of a GameSheet game."""
    g = gs_index()["games"].get(str(gid))
    if g:
        return [(g["home"], g["sid"], g["home_id"]), (g["away"], g["sid"], g["away_id"])]
    d = await gs_json(f"/api/unified-games/{sid}", ttl=1800)
    for x in (d or {}).get("data") or []:
        if str(x.get("gameId") or x.get("id")) == str(gid):
            h, v = x.get("home") or {}, x.get("visitor") or {}
            return [(h.get("title") or "", str(sid), str(h.get("id"))), (v.get("title") or "", str(sid), str(v.get("id")))]
    return []


def gs_name_matches(team_name: str, limit: int = 3) -> list[tuple[float, str, str]]:
    ix = gs_index()
    ws = _words(team_name)
    pool = set()
    for w in ws:
        pool |= ix["words"].get(w, set())
    cands = []
    for key in pool:
        name, div, sname = ix["teams"][key]
        s = name_score(team_name, name, div, sname)
        if s >= MIN_MATCH:
            cands.append((s, key[0], key[1]))
    cands.sort(key=lambda x: -x[0])
    return cands[:limit]


async def from_gamesheet(team_name: str, date: str, team: dict, game: dict) -> list[dict]:
    targets = []          # (match, sid, tid, by: game | id | name)
    m = GS_GAME_LINK.search(game.get("url") or "")
    if m:
        sides = await gs_game_teams(m.group(1), m.group(2))
        scored = sorted(((team_similarity(team_name, n, game.get("league") or "")[0], s, t) for n, s, t in sides), reverse=True)
        if scored and scored[0][0] >= 0.5:
            targets.append((max(scored[0][0], 0.9), scored[0][1], scored[0][2], "game"))
    cur_sids = set(gs_index()["seasons"])
    for k in ("roster", "site", "schedule"):
        m = GS_TEAM_LINK.search(team.get(k) or "")
        if m and m.group(1) in cur_sids:
            targets.append((1.0, m.group(1), m.group(2), "id"))
    if not targets:
        targets += [(s, sid, tid, "name") for s, sid, tid in gs_name_matches(team_name)]
    seen, uniq = set(), []
    for t in targets:
        if (t[1], t[2]) not in seen:
            seen.add((t[1], t[2]))
            uniq.append(t)
    res = await asyncio.gather(*[gs_roster(sid, tid, date) for _, sid, tid, _ in uniq[:3]], return_exceptions=True)
    out = []
    for (s, sid, tid, by), r in zip(uniq, res):
        if isinstance(r, dict):
            r.update(match=s, by=by, from_game=by == "game")
            out.append(r)
    return out


# ---------------------------------------------------------------- HockeyTech

HT_FEED = "https://lscluster.hockeytech.com/feed/index.php"
HT_GAME = re.compile(r"client_code=([\w-]+).*?game_id=(\d+)|game_id=(\d+).*?client_code=([\w-]+)")
HT_SITE_ROSTER = re.compile(r"https?://(?:www\.)?([^/]+)/stats/roster/(\d+)/(\d+)")
_ht_keys = None


def ht_keys() -> dict:
    """client_code -> (feed key, [league sites])."""
    global _ht_keys
    if _ht_keys is None:
        _ht_keys = {}
        try:
            from .adapters.hockeytech import LEAGUES
            for code, cfg in LEAGUES.items():
                _ht_keys.setdefault(cfg.get("client", code), (cfg["key"], []))
        except Exception:
            pass
        try:
            for code, v in json.loads((DATA / "hockeytech_clients.json").read_text()).items():
                _ht_keys[code] = (v["key"], v.get("sites") or [])
        except Exception:
            pass
    return _ht_keys


def _jsonp(t: str | None):
    if not t:
        return None
    t = t.strip()
    if t.startswith("("):
        t = t[1:-1]
    try:
        return json.loads(t)
    except ValueError:
        return None


async def ht_game(code: str, gid: str) -> dict | None:
    key = ht_keys().get(code, (None,))[0]
    if not key:
        return None
    t = await get_text(f"{HT_FEED}?feed=gc&tab=gamesummary&key={key}&client_code={code}&game_id={gid}&lang_code=en&fmt=json", ttl=TTL, timeout=30)
    return (((_jsonp(t) or {}).get("GC") or {}).get("Gamesummary")) or None


async def ht_roster(code: str, season_id: str, team_id: str, date: str, team_site: str | None = None) -> dict | None:
    key, sites = ht_keys().get(code, (None, []))
    if not key:
        return None
    base = f"{HT_FEED}?feed=modulekit&view={{v}}&key={key}&client_code={code}&lang=en&fmt=json"
    ros, seasons = await asyncio.gather(get_text(base.format(v="roster") + f"&season_id={season_id}&team_id={team_id}", ttl=TTL, timeout=30),
                                        get_text(base.format(v="seasons"), ttl=24 * 3600, timeout=30))
    rows = (((_jsonp(ros) or {}).get("SiteKit") or {}).get("Roster")) or []
    rows = [y for x in rows for y in (x if isinstance(x, list) else [x])]     # some clients nest the list (OUA)
    sinfo = next((s for s in (((_jsonp(seasons) or {}).get("SiteKit") or {}).get("Seasons") or []) if str(s.get("season_id")) == str(season_id)), {})
    players, tname = [], None
    for p in rows:
        if not isinstance(p, dict) or not p.get("name") and not p.get("last_name"):
            continue
        pos = p.get("position") or ""
        if pos and not re.match(r"^(G|D|F|C|LW|RW|W|RD|LD)$", pos, re.I):
            continue          # staff
        b = (p.get("rawbirthdate") or p.get("birthdate") or "")[:4]
        players.append(dict(number=p.get("tp_jersey_number") or p.get("jersey_number"), name=p.get("name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
                            pos=_pos(pos), birth_year=int(b) if b.isdigit() and b != "0000" else None))
        tname = tname or p.get("team_name")
    cur = season_start(date)
    s0, s1 = sinfo.get("start_date") or "", sinfo.get("end_date") or ""
    sname = sinfo.get("season_name") or ""
    yy = f"{str(cur)[2:]}-{str(cur + 1)[2:]}"
    if s0 and s1:
        current = s0 <= f"{cur + 1}-06-30" and s1 >= f"{cur}-07-01"
    else:
        current = any(x in sname.replace(" ", "") for x in (f"{cur}-{cur + 1}", f"{cur}-{str(cur + 1)[2:]}", yy, f"{str(cur)[2:]}/{str(cur + 1)[2:]}"))
    site = team_site if team_site and team_site in sites else (sites[0] if sites else None)
    url = f"https://{site}/stats/roster/{team_id}/{season_id}" if site else base.format(v="roster") + f"&season_id={season_id}&team_id={team_id}"
    return dict(url=url, source="HockeyTech", season=season_label(cur) if current else None, players=players, team=tname,
                league=f"{code.upper()} {sname}".strip(), feed=base.format(v="roster") + f"&season_id={season_id}&team_id={team_id}",
                note=f"HockeyTech ({code}) {sname or 'season ' + str(season_id)} roster, {len(players)} players")


async def ht_regular_season(code: str, date: str, lid: str | None = None) -> str | None:
    """The client's regular season (career=1, not playoffs) for the date's season."""
    key = ht_keys().get(code, (None,))[0]
    if not key:
        return None
    t = await get_text(f"{HT_FEED}?feed=modulekit&view=seasons&key={key}&client_code={code}&lang=en&fmt=json" + (f"&league_id={lid}" if lid else ""),
                       ttl=24 * 3600, timeout=30)
    cur = season_start(date)
    for s in ((_jsonp(t) or {}).get("SiteKit") or {}).get("Seasons") or []:
        s0, s1 = s.get("start_date") or "", s.get("end_date") or ""
        if s.get("career") == "1" and s.get("playoff") == "0" and s0 <= f"{cur + 1}-06-30" and s1 >= f"{cur}-07-01" \
                and not re.search(r"pre.?season|exhibition|combine|camp|showcase", s.get("season_name") or "", re.I):
            return s.get("season_id")
    return None


_ht_names = None


def ht_names() -> list[tuple]:
    """(team name, league, client code, game id, date) for this season's HockeyTech games in games.db, latest game per team."""
    global _ht_names
    if _ht_names is None:
        from . import gameindex
        best = {}
        try:
            rows = gameindex.conn().execute("select home, away, league, url, date from games where source='HockeyTech feed' and date>='2026-08-01'").fetchall()
        except Exception:
            rows = []
        today = datetime.date.today().isoformat()
        for home, away, league, url, d in rows:
            m = HT_GAME.search(url or "")
            if not m:
                continue
            code, gid = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
            for n in (home, away):
                k = (n, code)
                prev = best.get(k)
                # prefer the latest game already played (its summary has the final season and ids)
                score = (d <= today, d if d <= today else "")
                if prev is None or score > prev[0]:
                    best[k] = (score, (n, league or "", code, gid, d))
        _ht_names = [v[1] for v in best.values()]
    return _ht_names


_ht_teams = None


def _ht_current(s: dict, date: str) -> bool:
    cur = season_start(date)
    s0, s1 = s.get("start_date") or "", s.get("end_date") or ""
    return bool(s0 and s1 and f"{cur}-06-01" <= s0 <= f"{cur + 1}-06-30") and s.get("playoff") != "1"


async def ht_team_list(date: str) -> list[tuple]:
    """(team name, season name, client code, season id, team id, league id) for every team of this season's
    regular seasons and preseasons of every known HockeyTech client (teamsbyseason; cached a day)."""
    global _ht_teams
    if _ht_teams is not None:
        return _ht_teams
    feeds = []
    for code, (key, _) in ht_keys().items():
        feeds.append((code, key, None))
    try:
        for code, v in json.loads((DATA / "hockeytech_clients.json").read_text()).items():
            for lid in (v.get("league_ids") or {}):
                feeds.append((code, v["key"], lid))
    except Exception:
        pass
    sem = asyncio.Semaphore(4)

    async def one(code, key, lid):
        base = f"{HT_FEED}?feed=modulekit&key={key}&client_code={code}&lang=en&fmt=json" + (f"&league_id={lid}" if lid else "")
        async with sem:
            t = await get_text(base + "&view=seasons", ttl=24 * 3600, timeout=30)
        seasons = [x for x in ((_jsonp(t) or {}).get("SiteKit") or {}).get("Seasons") or [] if _ht_current(x, date)]
        rows = []
        for x in seasons[:4]:
            async with sem:
                t2 = await get_text(base + f"&view=teamsbyseason&season_id={x['season_id']}", ttl=24 * 3600, timeout=30)
            for tm in ((_jsonp(t2) or {}).get("SiteKit") or {}).get("Teamsbyseason") or []:
                if not tm.get("name"):
                    continue
                names = [tm["name"]]
                nick = (tm.get("nickname") or "").strip()
                if nick and nick.lower() not in tm["name"].lower():
                    names.append(nick if tm["name"].lower() in nick.lower() else f"{tm['name']} {nick}")   # OUA: 'Ottawa' + 'Gee-Gees'
                for n in names:
                    rows.append((n, f"{x.get('season_name', '')} {tm.get('division_long_name') or ''}".strip(), code,
                                 str(x["season_id"]), str(tm["id"]), lid))
        return rows

    res = await asyncio.gather(*[one(*f) for f in feeds], return_exceptions=True)
    _ht_teams = [r for x in res if isinstance(x, list) for r in x]
    return _ht_teams


async def _ht_best_roster(code: str, season_id: str | None, team_id: str, date: str, site: str | None, lid: str | None = None) -> dict | None:
    """The team's roster in this season's regular season when it has 10+ players, else in the given season
    (a preseason roster lists every camp invitee, and junior teams cut down in September)."""
    reg = await ht_regular_season(code, date, lid)
    r = await ht_roster(code, reg, team_id, date, site) if reg else None
    if r and len(r["players"]) >= 10:
        return r
    if season_id and str(season_id) != str(reg):
        r2 = await ht_roster(code, season_id, team_id, date, site)
        if r2 and r2["players"]:
            return r2
    return r


async def from_hockeytech(team_name: str, date: str, team: dict, game: dict) -> list[dict]:
    targets = []    # (match, code, season id, team id, by, league id)
    url = game.get("url") or ""
    m = HT_GAME.search(url) if ("hockeytech" in url or "client_code" in url) else None
    if m:
        code, gid = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        g = await ht_game(code, gid)
        if g:
            meta = g.get("meta") or {}
            sides = [((g.get("home") or {}).get("name") or "", meta.get("home_team")), ((g.get("visitor") or {}).get("name") or "", meta.get("visiting_team"))]
            sc = sorted(((team_similarity(team_name, n, "")[0], tid, n) for n, tid in sides if tid), reverse=True)
            if sc and sc[0][0] >= 0.5:
                targets.append((max(0.9, sc[0][0]), code, meta.get("season_id"), sc[0][1], "game", None))
    # a Club Data link to a HockeyTech league site's roster page: /stats/roster/{team}/{season}
    site_link = None
    for k in ("roster", "schedule", "site"):
        sm = HT_SITE_ROSTER.search(team.get(k) or "")
        if sm:
            site_link = sm
            code = next((c for c, (_, sites) in ht_keys().items() if sm.group(1) in sites), None)
            lm = re.search(r"[?&]league=(\d+)", team.get(k) or "")
            if code and not targets:
                targets.append((1.0, code, None, sm.group(2), "id", lm.group(1) if lm else None))
            break
    if not targets:
        ws = _words(team_name)
        pool = [t for t in await ht_team_list(date) if _words(t[0]) & ws]
        for s, t in _best_name(team_name, pool, 1)[:2]:
            targets.append((s, t[2], t[3], t[4], "name", t[5]))
    site = site_link.group(1) if site_link else None
    seen, out = set(), []
    for s, code, sid, tid, by, lid in targets:
        if (code, tid) in seen:
            continue
        seen.add((code, tid))
        r = await _ht_best_roster(code, sid, tid, date, site, lid)
        if r:
            r.update(match=s, from_game=by == "game", by=by)
            out.append(r)
    return out


# ---------------------------------------------------------------- TimeToScore

TTS_TEAM = re.compile(r"""href=['"]?display-schedule\?team=(\d+)&(?:amp;)?season=(\d+)&(?:amp;)?league=(\d+)(?:&(?:amp;)?stat_class=(\d+))?[^'">]*['"]?>\s*([^<]+?)\s*</a>""", re.I)
TTS_PAGE = "https://stats.{host}.timetoscore.com/display-schedule?team={team}&season={season}&league={league}&stat_class={sc}"
TTS_OPTION = re.compile(r"""<option value=["']?(\d+)["']?[^>]*>([^<]*)""", re.I)
FIRST_HALF = re.compile(r"(?i)\b(jan|feb|mar|apr|may|jun)")
SECOND_HALF = re.compile(r"(?i)\b(jul|aug|sep|oct|nov|dec)")
_tts_names = None


def tts_names() -> list[tuple]:
    """(site team name, host) for this season's TimeToScore games in games.db."""
    global _tts_names
    if _tts_names is None:
        from . import gameindex
        seen = set()
        try:
            rows = gameindex.conn().execute("select home, away, site, league from games where source='timetoscore' and date>='2026-08-01'").fetchall()
        except Exception:
            rows = []
        for home, away, site, league in rows:
            host = (site or "").split(".")[1] if (site or "").count(".") >= 2 else None
            for n in (home, away):
                if host:
                    seen.add((n, league or "", host))
        _tts_names = sorted(seen)
    return _tts_names


def _tts_season_current(label: str, cur: int) -> bool:
    """A season option of this hockey season: 'NGHL Labor Day Challenge, Sept 4-6, 2026', '2026-27 ...'."""
    t = label or ""
    if f"{cur}-{str(cur + 1)[2:]}" in t or f"{cur}-{cur + 1}" in t or f"{cur}/{str(cur + 1)[2:]}" in t:
        return True
    if str(cur + 1) in t:
        return bool(FIRST_HALF.search(t)) or not SECOND_HALF.search(t)
    if str(cur) in t:
        return bool(SECOND_HALF.search(t)) or not FIRST_HALF.search(t)
    return False


async def tts_standings(host: str, league: int, date: str) -> list[tuple[str, str, str, str, str]]:
    """[(team name as header style, team id, season id, league id, stat class)] from the league's standings,
    for the current season and every event season of this hockey season in the season menu (NGHL and
    showcase leagues open one "season" per event)."""
    import html as _html
    from .adapters.timetoscore import fix_name
    base = f"https://stats.{host}.timetoscore.com/display-stats.php?league={league}"
    page = await get_text(base, ttl=TTL, timeout=60)
    cur = season_start(date)
    extra = [v for v, label in TTS_OPTION.findall(page or "") if v != "0" and _tts_season_current(_html.unescape(label), cur)]
    pages = [page] + list(await asyncio.gather(*[get_text(f"{base}&season={v}", ttl=TTL, timeout=60) for v in extra[:8]]))
    out = []
    for pg in pages:
        for tid, sid, lid, sc, name in TTS_TEAM.findall(pg or ""):
            out.append((fix_name(_html.unescape(name).strip()), tid, sid, lid, sc or "1"))
    return list(dict.fromkeys(out))


async def tts_team(host: str, tid: str, sid: str, lid: str, date: str, name: str, sc: str = "1") -> dict | None:
    from .adapters.timetoscore import season_date
    url = TTS_PAGE.format(host=host, team=tid, season=sid, league=lid, sc=sc)
    page = await get_text(url, ttl=TTL, timeout=40)
    if not page:
        return None
    players, section = [], None
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S | re.I):
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).replace("&nbsp;", " ").strip() for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S | re.I)]
        if not cells:
            continue
        if cells[0] in ("Player Stats", "Goalie Stats") or (len(cells) == 1 and cells[0].endswith("Stats")):
            section = cells[0]
            continue
        if cells[0] == "Name":
            continue
        if section in ("Player Stats", "Goalie Stats") and len(cells) >= 3 and re.fullmatch(r"\d{1,3}", cells[1] or "") and re.search(r"[A-Za-z]", cells[0]):
            if not any(p["name"] == cells[0] for p in players):
                players.append(dict(number=cells[1], name=cells[0], pos="G" if section == "Goalie Stats" else None, birth_year=None))
        if cells[0] in ("Team Stats", "Special Teams"):
            section = None
    dates = [season_date(m) for m in re.findall(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \w{3} \d{1,2}\b", page)]
    cur = season_start(date)
    current = any(dates)
    return dict(url=url, source="TimeToScore", season=season_label(cur) if current else None, players=players, team=name,
                league=f"TimeToScore {host} league {lid}", note=f"TimeToScore team stats page ({host}, league {lid}): {len(players)} players who have played this season")


async def from_timetoscore(team_name: str, date: str, team: dict, game: dict, kb: dict) -> list[dict]:
    learned = kb.get("tts_team_leagues") or {}
    leagues = []            # (host, league)
    url = game.get("url") or ""
    hm = re.search(r"stats\.(\w+)\.timetoscore\.com", url)
    ws = _words(team_name)
    pool = [t for t in tts_names() if _words(t[0]) & ws and (not hm or t[2] == hm.group(1))]
    from .adapters.timetoscore import COMP_HINTS, load_leagues
    known = load_leagues()
    for s, t in _best_name(team_name, pool, 1)[:3]:
        name, league_text, host = t
        found = [tuple(hl.split("#")) for hl in learned.get(norm(name), [])]
        # the team -> league map is rebuilt by each harvest and can miss leagues: also map the game's
        # league text ('AHF 10U AA', 'NGHL - Blue 19U') to league ids by the adapter's hints and the league list
        for pat, h, ids in COMP_HINTS:
            if h == host and re.search(pat, league_text.lower()):
                found += [(h, str(i)) for i in ids]
        first = (league_text.split() or [""])[0].lower()
        found += [(x["host"], str(x["league"])) for x in known if x["host"] == host and str(x.get("name", "")).lower() == first]
        for h, l in found:
            if (h, int(l)) not in leagues:
                leagues.append((h, int(l)))
    for hl in learned.get(norm(team_name), []):
        h, l = hl.split("#")
        if (h, int(l)) not in leagues:
            leagues.append((h, int(l)))
    if not leagues:
        return []
    tables = await asyncio.gather(*[tts_standings(h, l, date) for h, l in leagues[:3]])
    targets = []
    for (h, l), rows in zip(leagues, tables):
        for s, (name, tid, sid, lid, sc) in _best_name(team_name, rows)[:2]:
            targets.append((s, h, tid, sid, lid, name, sc))
    res = await asyncio.gather(*[tts_team(h, tid, sid, lid, date, name, sc) for s, h, tid, sid, lid, name, sc in targets])
    out = []
    for t, r in zip(targets, res):
        if r:
            r.update(match=t[0], from_game=False, by="name")
            out.append(r)
    return out


# ---------------------------------------------------------------- The Hockey Observer (Ontario women's hockey)

THO = "https://www.thehockeyobserver.com"
THO_PLAYER = re.compile(r"(?m)^(G|D|F|F/D|D/F|LW|RW|C)\s+(\d{1,2})\n([^\n]{3,60})\n((?:19|20)\d\d)\s*·")


async def from_observer(team_name: str, date: str, team: dict) -> list[dict]:
    """thehockeyobserver.com: one WordPress page per OWHA team with the season's roster (number, position,
    birth year). Pages are found through the WordPress search API (/wp-json/wp/v2/pages?search=)."""
    import html as _html
    from .rosters import page_text
    country = str(team.get("country") or "").lower()
    if country and country not in ("canada", "ca"):
        return []
    q = _ep_query_words(team_name)
    if len(q) < 3:
        return []
    t = await get_text(f"{THO}/wp-json/wp/v2/pages?search={quote(q)}&per_page=30&_fields=id,link,title,modified", ttl=24 * 3600, timeout=25,
                       headers={"Accept": "application/json"})
    try:
        pages = json.loads(t) if t else []
    except ValueError:
        return []
    cands = []
    for p in pages if isinstance(pages, list) else []:
        title = _html.unescape(((p.get("title") or {}).get("rendered")) or "")
        sc = name_score(team_name, title)
        if sc >= MIN_MATCH:
            cands.append((sc, title, p.get("link"), p.get("modified")))
    cands.sort(key=lambda x: -x[0])
    out = []
    y = season_start(date)
    for sc, title, link, modified in cands[:1]:
        raw = await get_text(link, ttl=TTL, timeout=25)
        if not raw:
            continue
        text = page_text(raw)
        players = [dict(number=m.group(2), name=m.group(3).strip(), pos=_pos(m.group(1).split("/")[0]), birth_year=int(m.group(4)))
                   for m in THO_PLAYER.finditer(text)]
        m = re.search(r"(20\d\d)\s*[–-]\s*(\d\d)\s+Roster", text)
        season = season_label(int(m.group(1))) if m else None
        out.append(dict(url=link, source="The Hockey Observer", season=season, players=players, team=title, league="OWHA", match=sc, by="name",
                        note=f"The Hockey Observer (Ontario women's hockey) {m.group(0) if m else 'roster'}, {len(players)} players, page updated {str(modified)[:10]}"))
    return out


# ---------------------------------------------------------------- fallback: Club Data pages (backend/rosters.py)

async def from_pages(team_name: str, date: str, team: dict) -> list[dict]:
    from .rosters import candidates, check
    cands = [(lab, u) for lab, u in candidates(team, date) if "eliteprospects" not in domain(u)]
    res = await asyncio.gather(*[check(u, date, team_name) for _, u in cands], return_exceptions=True)
    out = []
    cur = season_label(season_start(date))
    for (lab, u), r in zip(cands, res):
        if isinstance(r, Exception) or r.get("status") in (None, "blocked", "unreachable", "not found", "no roster", "not a roster"):
            continue
        players = [dict(number=x.get("n"), name=x.get("name"), pos=None, birth_year=int(x["born"]) if x.get("born") else None) for x in r.get("listed") or []]
        season = cur if r.get("season") == "current" else None
        status = {"verified": "verified", "probable": "probable", "check": "check"}.get(r["status"], "check")
        src = "MyHockeyRankings" if "myhockeyrankings" in domain(u) else ("GameSheet" if "gamesheet" in domain(u) else lab)
        out.append(dict(url=u, source=src, season=season, players=players, team=None, league="", match=1.0, by="id", status=status, age_fit=r.get("age_fit") or "unknown",
                        note=f"{lab}: {r.get('players')} players, season {r.get('season')}, ages {r.get('age_fit')}"))
    return out


# ---------------------------------------------------------------- entry point

async def find_rosters(team_name: str, date: str, hints: dict | None = None) -> list[dict]:
    """Current-season roster sources for one team, best first.

    hints (all optional):
      team      Club Data entry (keys site, roster, mhr, ep, schedule, country); the same keys may also sit
                directly in hints
      game      a game this team played: dict(url, source, league) e.g. a GameSheet / HockeyTech / TimeToScore game
      comp      the header's competition; its age group is used when the team name has none
      fallback  False to skip the slower page checks of Club Data links (MyHockeyRankings in a browser, team sites)
      kb        the knowledge base (loaded when missing)
    """
    hints = hints or {}
    team = dict(hints.get("team") or {k: hints[k] for k in ("site", "roster", "mhr", "ep", "schedule", "country") if hints.get(k)})
    game = dict(hints.get("game") or {})
    kb = hints.get("kb") or load_kb()
    comp_ages = sorted(a for a in tokens(ctx(hints.get("comp") or ""))[1] if len(a) <= 2)
    if not tokens(team_name)[1] and len(comp_ages) == 1:
        team_name = f"{team_name} U{comp_ages[0]}"      # "Toronto Jr. Aeros" in "GTHL U12 AA": the U12 team
    jobs = [from_ep(team_name, date, team), from_gamesheet(team_name, date, team, game),
            from_hockeytech(team_name, date, team, game), from_timetoscore(team_name, date, team, game, kb),
            from_observer(team_name, date, team)]
    res = await asyncio.gather(*jobs, return_exceptions=True)
    out = []
    for r in res:
        if isinstance(r, list):
            out += [finish(x, team_name, date) for x in r if x.get("players")]   # an empty roster is no source
    cross_check(out)
    if hints.get("fallback", True) and not any(x["status"] == "verified" for x in out):
        try:
            out += await from_pages(team_name, date, team)
        except Exception:
            pass
    seen, uniq = set(), []
    for x in sorted(out, key=rank):
        if x["url"] not in seen:
            seen.add(x["url"])
            uniq.append(x)
    return uniq


if __name__ == "__main__":
    import sys
    name, date = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else datetime.date.today().isoformat()
    from .lookup import match_team
    kb = load_kb()
    best = next(iter(match_team(name, kb)), None)
    rs = asyncio.run(find_rosters(name, date, dict(team=best or {}, kb=kb)))
    for r in rs:
        print(r["status"], r["source"], r["season"], len(r["players"]), r["url"], "|", r["note"])

"""Adapter registry and runner. Each adapter fetches a source and returns
candidate games; the runner scores them and decides Found / Check."""
from __future__ import annotations
import asyncio, time, re
from .base import strong, header_utc, anchored
from .hockeytech import HockeyTech
from .gamesheet import GameSheet
from .gamesheet_live import GameSheetLive
from .swehockey import SweHockey
from .hockeyno import HockeyNo
from .sihf import Sihf
from .leijonat import Leijonat
from .ceskyhokej import CeskyHokej
from .deb import Deb
from .liiga import Liiga
from .nhl import Nhl
from .del2 import Del2
from .sportsadmin import SportsAdmin
from .ligasy import Ligasy
from .timetoscore import TimeToScore
from .index import Index
from .websearch import WebSearch
from .khl import Khl
from .hockeyby import HockeyBy
from .chl import Chl
from .esportscz import EsportsCz
from .hockeydata import HockeyData
from .pennydel import PennyDel
from .ukhockey import UkHockey
from .hockeyfrance import HockeyFrance
from .onlajny import Onlajny
from .italiahockey import ItaliaHockey
from .lhf import Lhf
from .rampt import RampTournament
from .hockeyslovakia import HockeySlovakia
from .ramp import Ramp
from .regystra import Regystra
from .kreezee import Kreezee
from .ayhl import Ayhl
from .digitalshift import DigitalShift

INDEX = Index()
ADAPTERS = [SweHockey(), HockeyTech(), GameSheetLive(), HockeyNo(), Sihf(), Leijonat(), CeskyHokej(), Deb(), Liiga(), Nhl(), Del2(), SportsAdmin(), Ligasy(),
            TimeToScore(), Khl(), HockeyBy(), Chl(), EsportsCz(), HockeyData(), PennyDel(), UkHockey(), HockeyFrance(), Onlajny(),
            ItaliaHockey(), Lhf(), RampTournament(), HockeySlovakia(), Ramp(), Regystra(), Kreezee(), Ayhl(), DigitalShift()]
BUDGET_S = 30
WEB = WebSearch()
WEB_SEARCH = True     # last resort when no source knows the game; the replay turns it off


def learned_sites(parsed: dict, kb: dict) -> set:
    """Report sites that earlier results (saved or found automatically) used for these teams
    or this competition."""
    out = set()
    for key in ("t1n", "t2n"):
        out |= set((kb.get("team_sources") or {}).get(parsed[key], {}))
    out |= set((kb.get("comp_sources") or {}).get(parsed["compn"], {}))
    try:
        from ..learning import learned_sites as _ls
        out |= _ls(parsed)
    except Exception:
        pass
    return out

# which countries' teams each source can have; a source is skipped when both teams are
# known to be from elsewhere (country from Club Data, else the team site's domain)
COVERS = {"swehockey": {"sweden"}, "HockeyTech feed": {"canada", "usa"}, "GameSheet": {"canada", "usa"}, "hockey.no": {"norway"},
          "sihf": {"switzerland"}, "leijonat": {"finland"}, "liiga": {"finland"}, "ceskyhokej": {"czech republic"},
          "deb-online": {"germany"}, "del2": {"germany"}, "nhl": {"canada", "usa"}, "sportsadmin": {"denmark"}, "ligasy": {"kazakhstan"}}
TLD = {"se": "sweden", "fi": "finland", "de": "germany", "ch": "switzerland", "no": "norway", "dk": "denmark", "cz": "czech republic",
       "ca": "canada", "kz": "kazakhstan", "sk": "slovakia", "at": "austria", "fr": "france", "it": "italy", "lv": "latvia", "hu": "hungary",
       "pl": "poland", "si": "slovenia", "uk": "england", "ru": "russia", "by": "belarus", "us": "usa", "au": "australia", "nl": "netherlands"}
CANON = {"n/c america": "usa", "united states": "usa", "us": "usa", "czechia": "czech republic", "united kingdom": "england",
         "wales": "england", "scotland": "england"}
SHARED = ("eliteprospects", "myhockeyrankings", "gamesheet", "hockeytech", "google.", "hudl.")


def team_countries(b: dict | None) -> set:
    """Every country a team is known by: the Club Data field (sometimes wrong, e.g. Swedish
    clubs marked USA) plus the country of its own site's domain. .com/.org say nothing."""
    out = set()
    if not b:
        return out
    c = str(b.get("country") or "").strip().lower()
    if c and c not in ("none", "world"):
        out.add(CANON.get(c, c))
    from urllib.parse import urlparse
    for f in ("site", "schedule", "roster"):
        u = b.get(f) or ""
        host = urlparse(u).netloc.lower() if u.startswith("http") else ""
        if host and not any(x in host for x in SHARED):
            tld = host.rsplit(".", 1)[-1]
            if tld in TLD:
                out.add(TLD[tld])
    return out


def team_country(b: dict | None) -> str | None:
    cs = team_countries(b)
    return next(iter(cs)) if len(cs) == 1 else None


def country_gate(a, tm) -> bool:
    """False only when every known country of both teams is outside what this source covers;
    a team with no known country never blocks a source."""
    cov = COVERS.get(a.name)
    if not cov:
        return True
    cs = [team_countries(tm[k]["best"] or (tm[k]["candidates"][0] if tm[k]["candidates"] else None)) for k in ("t1n", "t2n")]
    if any(not c for c in cs):
        return True
    return bool((cs[0] | cs[1]) & cov)


EARLY_GRACE_S = 2.5


async def run_all(parsed: dict, tm: dict, kb: dict) -> dict:
    """Local game index first (instant); when it has no single strong match, every applicable
    live source runs concurrently within the time budget."""
    started = time.time()
    try:
        idx = await INDEX.find(parsed, tm, kb)
    except Exception:
        idx = []
    first = decide(parsed, idx)
    if first["verdict"] == "found" and _exact_enough(parsed, first["best"]):
        first.update(adapters=[dict(name="index", site=INDEX.site, ok=True, error=None, n=len(idx))], seconds=round(time.time() - started, 2), fast=True)
        return first
    sites = learned_sites(parsed, kb)
    tried, tasks = [], []
    for a in ADAPTERS:
        try:
            ok = country_gate(a, tm) and (a.applies(parsed, tm, kb) or any(a.site and a.site in s for s in sites))
        except Exception:
            ok = False
        if ok:
            tried.append(a)
            tasks.append(asyncio.create_task(a.find(parsed, tm, kb)))
    results = []
    if tasks:
        budget = max([BUDGET_S] + [getattr(a, "budget_s", BUDGET_S) for a in tried])
        deadline = started + budget
        pending = set(tasks)
        # stop early once a finished source has a clear match: slow sources (GameSheet)
        # get a short grace period to report a competing game, then are cancelled
        while pending:
            timeout = deadline - time.time()
            if timeout <= 0:
                break
            _, pending = await asyncio.wait(pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            got = [c for t in tasks if t.done() and not t.cancelled() and t.exception() is None for c in t.result()]
            if any(strong(c) for c in got):
                deadline = min(deadline, time.time() + EARLY_GRACE_S)
        for t in pending:
            t.cancel()
        for a, t in zip(tried, tasks):
            if t.done() and not t.cancelled() and t.exception() is None:
                results.append(dict(adapter=a.name, site=a.site, ok=True, candidates=t.result()))
            elif t.done() and not t.cancelled() and t.exception() is not None:
                results.append(dict(adapter=a.name, site=a.site, ok=False, error=str(t.exception())[:120], candidates=[]))
            else:
                results.append(dict(adapter=a.name, site=a.site, ok=False, error="timed out", candidates=[]))
    live = [c for r in results for c in r["candidates"]]
    try:                                  # every game seen live goes into the index for next time
        from .. import gameindex
        gameindex.add_games([dict(c, source=c.get("adapter") or c.get("source")) for c in live if c.get("kind") == "protocol"])
    except Exception:
        pass
    out = decide(parsed, idx + live)
    if out["verdict"] == "none" and WEB_SEARCH:
        try:
            web = await asyncio.wait_for(WEB.find(parsed, tm, kb), WEB.budget_s)
        except Exception:
            web = []
        if web:
            out = dict(verdict="check", best=None, candidates=web)
        results.append(dict(adapter=WEB.name, site=WEB.site, ok=True, candidates=web))
    out.update(adapters=[dict(name="index", site=INDEX.site, ok=True, error=None, n=len(idx))] +
               [dict(name=r["adapter"], site=r["site"], ok=r["ok"], error=r.get("error"), n=len(r["candidates"])) for r in results],
               seconds=round(time.time() - started, 1))
    return out


def _exact_enough(parsed: dict, c: dict) -> bool:
    """The index may hold only some games of a source (those seen in earlier searches), so it
    answers alone only when the game's start time matches the header, or, with no usable
    header time, the game is on the date its source expects. Otherwise the live sources run too."""
    from .base import TIME_MATCH
    if TIME_MATCH in c.get("reasons", []):
        return True
    if header_utc(parsed) and c.get("start_utc"):
        return False
    a = {x.name: x for x in ADAPTERS}.get(c.get("adapter") or "")
    want = a.preferred_date(parsed) if a is not None and hasattr(a, "preferred_date") else parsed["date"]
    return c.get("date") == want and not (a is not None and getattr(a, "ambiguous_dates", False))


def decide(parsed: dict, cands: list) -> dict:
    """Score-ranked candidates -> Found (one strong game) / Check / None."""
    # the same game from the index and from its live source: keep the live one, since the
    # source may mark it uncertain (e.g. a game on the day before the header's)
    live_urls = {c["url"] for c in cands if c.get("via") != "index"}
    cands = [c for c in cands if not (c.get("via") == "index" and c["url"] in live_urls)]
    cands = sorted(cands, key=lambda c: -c["confidence"])
    seen_urls, unique = set(), []
    for c in cands:                      # the same game can appear under two phases of one league
        key = re.sub(r"&divisionId=\d+|\?tab=.*$", "", c["url"])
        if key not in seen_urls:
            seen_urls.add(key)
            unique.append(c)
    cands = unique
    strong_ones = [c for c in cands if strong(c) and c.get("kind", "protocol") == "protocol"]   # a results page is never a Found
    # one game listed by two sources (hockeydata via two league sites, onlajny and hockeydata):
    # the same hockeydata game id, or both teams strong and the same start time to 5 minutes
    if len(strong_ones) > 1:
        import datetime as _dt
        ids = {(re.search(r"gameId=([0-9a-f-]{36})", c["url"]) or [None, None])[1] for c in strong_ones}
        starts = [c.get("start_utc") for c in strong_ones]
        same_id = None not in ids and len(ids) == 1
        same_time = False
        if all(starts):
            try:
                ts = [_dt.datetime.fromisoformat(x) for x in starts]
                same_time = (max(ts) - min(ts)).total_seconds() <= 300
            except Exception:
                pass
        if same_id or same_time:
            # keep the league's own page (a live source) over the bulk copy in the game list
            strong_ones = sorted(strong_ones, key=lambda c: (c.get("via") == "index", -c["confidence"]))[:1]
    # a game of these teams far from the header's time loses to another game of one of the
    # teams that starts right at the header's time (the pair's other game of a weekend)
    from .base import TIME_MATCH
    at_time = [c for c in cands if TIME_MATCH in c.get("reasons", []) and max(c["team_scores"]) >= 0.9]
    if at_time:
        strong_ones = [c for c in strong_ones if not any("h away from the header time" in r for r in c.get("reasons", []))]
    # Back-to-back games of the same pair: the header's time (Moscow time) decides when the
    # sources give start times; keep the one game starting within 100 minutes of it.
    hu = header_utc(parsed)
    if hu and len(strong_ones) > 1:
        import datetime as _dt
        def close(c):
            try:
                return abs((_dt.datetime.fromisoformat(c["start_utc"]) - hu).total_seconds()) <= 6000
            except Exception:
                return False
        timed = [c for c in strong_ones if c.get("start_utc")]
        near = [c for c in timed if close(c)]
        if len(timed) >= 2 and len(near) == 1:
            strong_ones = near
    # Home-and-home series: the same two teams on consecutive days. Tie-break by
    # 1) home/away orientation matching the header, 2) the adapter's preferred date
    # (HockeyTech: the day before, since header dates are UTC). Still ambiguous -> Check.
    if len(strong_ones) > 1:
        same_way = [c for c in strong_ones if "home and away are swapped on the site" not in c["reasons"]]
        if len(same_way) >= 1:
            strong_ones = same_way
    if len(strong_ones) > 1:
        # prefer candidates whose league name matches the competition hint
        from ..lookup import tokens, is_friendly
        import difflib
        cb, _, _ = tokens(parsed["comp"])
        if cb and not is_friendly(parsed["comp"]):
            def league_sim(c):
                lb, _, _ = tokens(c.get("league") or "")
                return difflib.SequenceMatcher(None, cb, lb).ratio() if lb else 0.0
            sims = [(league_sim(c), c) for c in strong_ones]
            best_sim = max(x[0] for x in sims)
            if best_sim >= 0.6:
                matching = [c for sm, c in sims if sm >= best_sim - 0.15]
                if len(matching) < len(strong_ones):
                    strong_ones = matching
    if len(strong_ones) > 1:
        # only an adapter that knows its date convention may pick between days;
        # everything else stays ambiguous and goes to Check
        by_name = {a.name: a for a in ADAPTERS}
        adapters_here = {c.get("adapter") or c.get("source") for c in strong_ones}
        if len(adapters_here) == 1:
            a = by_name.get(next(iter(adapters_here)))
            # sources without their own time-zone rule (European leagues) use the header date
            pd = a.preferred_date(parsed) if a and hasattr(a, "preferred_date") else parsed["date"]
            if a and getattr(a, "ambiguous_dates", False):
                pd = None
            pref = [c for c in strong_ones if c.get("date") == pd]
            strong_ones = pref if len(pref) == 1 else []
        else:
            strong_ones = []
    if not strong_ones and not any(strong(c) and not any("h away from the header time" in r for r in c.get("reasons", [])) for c in cands):
        # no game where both names match: one clearly matching team at the header's exact
        # start time is the game, provided exactly one such game exists
        anch = [c for c in cands if anchored(c) and c.get("kind", "protocol") == "protocol"]
        if len(anch) == 1:
            anch[0]["reasons"] = anch[0]["reasons"] + ["one team and the exact start time match; the other team's name is written differently"]
            strong_ones = anch
    if len(strong_ones) == 1:
        verdict = "found"
    elif cands:
        verdict = "check"
    else:
        verdict = "none"
    return dict(verdict=verdict, best=strong_ones[0] if verdict == "found" else None, candidates=cands[:8])

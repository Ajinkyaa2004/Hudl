"""RTT Finder: parse a match header and list every place to find the protocol
or the rosters, in the team's search order. Reads only data/knowledge.json."""
from __future__ import annotations
import functools, re, json, unicodedata, difflib, threading
from pathlib import Path
from urllib.parse import urlparse, quote_plus

DATA = Path(__file__).resolve().parent.parent / "data"
KNOWLEDGE = DATA / "knowledge.json"
VPN_FILE = DATA / "vpn_sites.json"
UPLOAD_SESSIONS = "https://www.hudl.com/admin/externalingest/upload-sessions"
_lock = threading.Lock()

FEDERATION = {
    "sweden": "https://stats.swehockey.se/", "switzerland": "https://www.sihf.ch/de/game-center/",
    "czech republic": "https://www.ceskyhokej.cz/", "czechia": "https://www.ceskyhokej.cz/",
    "germany": "https://www.deb-online.live/", "finland": "https://tulospalvelu.leijonat.fi/",
    "slovakia": "https://www.hockeyslovakia.sk/", "norway": "https://live.hockey.no/",
    "denmark": "https://stats.sportsadmin.dk/", "russia": "https://www.khl.ru/",
    "kazakhstan": "https://pro.ligasy.kz/", "belarus": "https://hockey.by/",
    "france": "https://www.hockeyfrance.com/", "austria": "https://www.eishockey.at/",
    "latvia": "https://lhf.lv/", "estonia": "https://ehs.eestihoki.ee/", "poland": "https://www.pzhl.org.pl/",
}
# Competition-name keywords -> country, used to pick a federation site when the
# teams on file have no country. Lower-case substrings.
COMP_COUNTRY = [
    (("extraliga", "dorostu", "trid", "chance liga", "czech", "cesk"), "czech republic"),
    (("del ", "del2", "dnl", "oberliga", "regionalliga", "german", "deutsch"), "germany"),
    (("shl", "hockeyallsvenskan", "hockeyettan", "j20 nationell", "j18", "j20", "u16 region", "swed"), "sweden"),
    (("liiga", "mestis", "u20 sm", "u18 sm", "u16 sm", "finnish", "finland", "suomi"), "finland"),
    (("national league", "swiss league", "u20-elit", "u17-elit", "u15-elit", "swiss", "mysports"), "switzerland"),
    (("tipos", "slovak", "slovensk"), "slovakia"),
    (("khl", "vhl", "mhl", "molodyozhnaya", "supreme hockey league", "kontinental", "russia", "vysshaya"), "russia"),
    (("metal ligaen", "denmark", "danish", "u20 liga", "u17 liga"), "denmark"),
    (("eliteserien", "norway", "norweg", "1. divisjon"), "norway"),
    (("optibet", "latvia"), "latvia"),
    (("estonia", "eesti"), "estonia"),
    (("belarus", "extraleague"), "belarus"),
    (("kazakh",), "kazakhstan"),
    (("ligue magnus", "france", "french"), "france"),
    (("austria", "ice hockey league", "alps hockey"), "austria"),
]


def comp_country(comp: str):
    c = " " + comp.lower() + " "
    for keys, country in COMP_COUNTRY:
        if any(k in c for k in keys):
            return country
    return None


AGE_RE = re.compile(r"\b(?:u|j)\s?(\d{1,2})\b|\b(\d{1,2})\s?u\b|\b(20[01]\d)\b|\b(\d{1,2})\s?o\b", re.I)
LEVEL_RE = re.compile(r"\b(aaa|aa|elite|major|minor|female|women|girls?|\(g\)|\(w\))\b", re.I)
FULL_RE = re.compile(
    r"^\s*(?:\[(?P<id>\d+)\]\s*)?(?P<t1>.+?)\s+(?:\d+\s*-\s*\d+|vs\.?|v\.?|-|–)\s+(?P<t2>.+?)\s*\|\s*(?P<comp>[^|]+?)\s*\|\s*(?P<date>\d{4}-\d{2}-\d{2})(?:[T ](?P<time>\d{2}:\d{2}))?", re.I)


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm(s):
    return re.sub(r"[^a-z0-9]", "", strip_accents(str(s or "")).lower())


def tokens(name):
    """(base name, age tokens, level tokens); cached, since matching calls it for every team on file."""
    return _tokens(str(name or ""))


@functools.lru_cache(maxsize=300000)
def _tokens(name):
    s = strip_accents(name).lower()
    s = re.sub(r",?\s*\b(a\.\s?s\.|z\.\s?s\.|s\.\s?r\.\s?o\.|o\.\s?s\.|e\.\s?v\.|gmbh|ry)\s*$", "", s)   # company suffixes
    ages = {next(g for g in m.groups() if g) for m in AGE_RE.finditer(s)}
    if any(len(a) <= 2 for a in ages):          # "San Jose Jr Sharks 2014 12U": the age group wins
        ages = {a for a in ages if len(a) <= 2}
    levels = {x.strip("()") for x in LEVEL_RE.findall(s)}
    base = LEVEL_RE.sub(" ", AGE_RE.sub(" ", s))
    base = re.sub(r"\b(hc|hk|sk|if|ifk|ik|bk|hf|ec|ehc|ev|hcm|mhk|hkm|jr\.?|hockey|club|team|ishockeyklubb|ishockeyklub|ishockeylag|ishockey|eishockey|hockeyklubb|hockeyclub|jaakiekko|idrettslag|sportsklubb)\b", " ", base)
    return re.sub(r"[^a-z0-9]", "", base), frozenset(ages), frozenset(levels)


def domain(url):
    if not isinstance(url, str) or "http" not in url:
        return ""
    m = re.search(r"https?://[^\s]+", url)
    return urlparse(m.group(0)).netloc.lower().replace("www.", "") if m else ""


def first_url(text):
    m = re.search(r"https?://[^\s]+", text) if isinstance(text, str) else None
    return m.group(0) if m else None


def parse_header(text):
    m = FULL_RE.match((text or "").strip())
    if not m:
        return None
    t1, t2, comp = m.group("t1").strip(), m.group("t2").strip(), m.group("comp").strip()
    return dict(match_id=m.group("id"), t1=t1, t2=t2, comp=comp, date=m.group("date"), time=m.group("time"),
                t1n=norm(t1), t2n=norm(t2), compn=norm(comp), short=f"{t1} vs {t2} | {comp} | {m.group('date')}")


def is_friendly(comp):
    c = comp.lower()
    return any(w in c for w in ("friendl", "pre-season", "preseason", "exhibition", "test tag"))


# ------------------------------------------------------------ knowledge base

def load_kb():
    if not KNOWLEDGE.exists():
        return dict(teams={}, tournaments={}, comp_sources={}, team_sources={}, history=[])
    return json.loads(KNOWLEDGE.read_text())


def save_kb(kb):
    with _lock:
        KNOWLEDGE.write_text(json.dumps(kb, ensure_ascii=False))


def load_vpn():
    try:
        return [d.lower().replace("www.", "") for d in json.loads(VPN_FILE.read_text()).get("sites", [])]
    except Exception:
        return []


def save_vpn(sites):
    VPN_FILE.write_text(json.dumps(dict(sites=sorted(set(s.strip().lower().replace("www.", "") for s in sites if s.strip()))), indent=1))


# ------------------------------------------------------------ matching

def match_team(name, kb, limit=3):
    teams = kb["teams"]
    k = norm(name)
    if k in teams:
        return [dict(score=1.0, exact=True, reason="exact name", key=k, **teams[k])]
    base, ages, levels = tokens(name)
    out = []
    for tk, t in teams.items():
        tb, ta, tl = tokens(t["name"])
        if not tb or not base or abs(len(tb) - len(base)) > max(4, len(base) // 2):
            continue
        sm = difflib.SequenceMatcher(None, base, tb)
        if sm.real_quick_ratio() < 0.82 or sm.quick_ratio() < 0.82:
            continue
        s = sm.ratio()
        if s < 0.82:
            continue
        if ages != ta:
            out.append(dict(score=round(s * 0.5, 2), exact=False, blocked=True, key=tk,
                            reason=f"age group differs: header {sorted(ages) or 'none'} vs {sorted(ta) or 'none'}", **t))
        elif levels and tl and levels != tl:
            out.append(dict(score=round(s * 0.7, 2), exact=False, blocked=True, key=tk,
                            reason=f"level differs: header {sorted(levels)} vs {sorted(tl)}", **t))
        elif levels != tl:
            out.append(dict(score=round(s * 0.9, 2), exact=False, key=tk,
                            reason=f"age exact, level not stated on {'file' if not tl else 'header'}", **t))
        else:
            out.append(dict(score=round(s, 2), exact=False, key=tk, reason="fuzzy on base name, age and level exact", **t))
    out.sort(key=lambda c: (-c["score"], c["name"]))
    return out[:limit]


def match_tournament(comp, kb):
    tours = kb["tournaments"]
    k = norm(comp)
    if k in tours:
        return dict(score=1.0, key=k, **tours[k])
    best = difflib.get_close_matches(k, tours.keys(), n=1, cutoff=0.88)
    if best:
        return dict(score=round(difflib.SequenceMatcher(None, k, best[0]).ratio(), 2), key=best[0], **tours[best[0]])
    return None


# ------------------------------------------------------------ search

def _link(label, url, kind, vpn, note=None):
    d = domain(url)
    return dict(label=label, url=url, domain=d, kind=kind, note=note, vpn=d in vpn)


def search(header, kb):
    vpn = load_vpn()
    p = parse_header(header)
    if not p:
        return dict(ok=False, error="Could not parse. Use: Team 1 vs Team 2 | Competition | YYYY-MM-DD")
    friendly = is_friendly(p["comp"])
    steps = []

    # 0. own history: same pair found before in this tool
    prev = [h for h in kb.get("history", []) if {h["t1n"], h["t2n"]} == {p["t1n"], p["t2n"]}]
    prev.sort(key=lambda h: h["date"], reverse=True)
    same_day = next((h for h in prev if h["date"] == p["date"]), None)
    hl = []
    if same_day and same_day.get("protocol"):
        hl.append(_link("Protocol saved for this game", same_day["protocol"], "protocol", vpn, f"saved {same_day['saved_at'][:10]}"))
    for h in prev[:2]:
        if h is not same_day and h.get("protocol"):
            hl.append(_link(f"Same teams on {h['date']}: report was on {domain(h['protocol'])}", h["protocol"], "history", vpn))
    steps.append(dict(step="Saved before", hit=bool(hl), links=hl, note="Games you saved in this tool" if hl else "Not saved before"))

    # 1. upload sessions, manual
    steps.append(dict(step="Hudl upload sessions", hit=None, manual=True, note="Manual: check coach uploads for either team on this date",
                      links=[_link("Open upload sessions (logged-in Chrome)", UPLOAD_SESSIONS, "manual", vpn)]))

    # 2. league / tournament
    tour = match_tournament(p["comp"], kb)
    links = []
    if tour and not friendly:
        for lab, key in (("Schedule", "schedule"), ("Official site", "site"), ("MyHockeyRankings", "mhr"), ("EliteProspects", "ep")):
            if tour.get(key):
                links.append(_link(f"{tour['name']}: {lab}", tour[key], "league", vpn))
        note = f"Matched competition \"{tour['name']}\" ({tour['score']:.0%})"
    elif friendly:
        note = "Friendly: competition hint gives no source, team and federation sites first"
    else:
        note = "Competition not in the knowledge base. Add it under Sources once found"
    if not friendly:
        for d, n in sorted(kb["comp_sources"].get(p["compn"], {}).items(), key=lambda x: -x[1])[:3]:
            links.append(_link(f"Reports for this competition were on {d} ({n} games)", f"https://{d}/", "history", vpn))
    steps.append(dict(step="League / tournament site", hit=bool(links), links=links, note=note))

    # 3, 4. team sites
    tm = {}
    for label, name, key in (("Home team site", p["t1"], "t1n"), ("Away team site", p["t2"], "t2n")):
        cands = match_team(name, kb)
        ok = [c for c in cands if not c.get("blocked")]
        best = ok[0] if ok else None
        tm[key] = dict(query=name, candidates=cands, best=best)
        links = []
        if best:
            for lab, k2 in (("Schedule", "schedule"), ("Official site", "site")):
                if best.get(k2):
                    links.append(_link(f"{best['name']}: {lab}", best[k2], "team", vpn))
        for d, n in sorted(kb["team_sources"].get(p[key], {}).items(), key=lambda x: -x[1])[:2]:
            links.append(_link(f"Their reports were on {d} ({n} games)", f"https://{d}/", "history", vpn))
        if not best and cands:
            # same club, other age group: the club site usually lists every team
            club = cands[0]
            for lab, k2 in (("Schedule", "schedule"), ("Official site", "site")):
                if club.get(k2):
                    links.append(_link(f"Same club, other team on file ({club['name']}): {lab}", club[k2], "club", vpn, "find this age group there"))
        if best:
            note = f"Matched \"{best['name']}\" ({best['score']:.0%}, {best['reason']})"
        elif cands:
            note = "Only other age groups on file: " + "; ".join(f"{c['name']} ({c['reason']})" for c in cands[:2])
        else:
            note = "Team not in the knowledge base. Add it under Sources once found"
        steps.append(dict(step=label, hit=bool(links), links=links, note=note))

    # 5. federation
    fl, seen = [], set()
    countries = []
    for key in ("t1n", "t2n"):
        b = tm[key]["best"] or (tm[key]["candidates"][0] if tm[key]["candidates"] else None)
        c = (b or {}).get("country")
        if c:
            countries.append((str(c).strip().lower(), "team on file"))
    cc = comp_country(p["comp"])
    if cc:
        countries.append((cc, "from competition name"))
    for c, how in countries:
        if c in FEDERATION and c not in seen:
            seen.add(c)
            fl.append(_link(f"{c.title()} federation stats", FEDERATION[c], "federation", vpn, how))
    steps.append(dict(step="Federation site (home country)", hit=bool(fl), links=fl,
                      note="For friendlies and unknown competitions" if (friendly or not tour) else "Optional for league games"))

    # 6. GameSheet / MHR
    q1, q2 = quote_plus(p["t1"]), quote_plus(p["t2"])
    gs = []
    for key in ("t1n", "t2n"):
        b = tm[key]["best"]
        if b and b.get("mhr"):
            gs.append(_link(f"{b['name']}: MyHockeyRankings page", b["mhr"], "team", vpn))
    gs += [_link(f"GameSheet: search {p['t1']}", f"https://www.google.com/search?q=site%3Agamesheetstats.com+%22{q1}%22", "search", vpn),
           _link(f"GameSheet: search {p['t2']}", f"https://www.google.com/search?q=site%3Agamesheetstats.com+%22{q2}%22", "search", vpn),
           _link(f"MyHockeyRankings: search {p['t1']}", f"https://www.google.com/search?q=site%3Amyhockeyrankings.com+%22{q1}%22", "search", vpn),
           _link(f"MyHockeyRankings: search {p['t2']}", f"https://www.google.com/search?q=site%3Amyhockeyrankings.com+%22{q2}%22", "search", vpn)]
    steps.append(dict(step="GameSheet / MyHockeyRankings", hit=True, links=gs, note="Direct pages where known, else site-restricted search"))

    # 7. rosters
    ro = []
    for lab, key, name in (("Team 1", "t1n", p["t1"]), ("Team 2", "t2n", p["t2"])):
        b = tm[key]["best"]
        if b and b.get("roster"):
            ro.append(_link(f"{lab} roster: {b['name']}", b["roster"], "roster", vpn))
        if b and b.get("ep"):
            ro.append(_link(f"{lab} EliteProspects: {b['name']}", b["ep"], "roster", vpn))
        ro.append(_link(f"{lab} EliteProspects search: {name}", f"https://www.eliteprospects.com/search/team?q={quote_plus(name)}", "search", vpn))
    steps.append(dict(step="Rosters", hit=any(l["kind"] == "roster" for l in ro), links=ro, note="Only if no report anywhere. Check the roster's last-updated date"))

    both = all(tm[k]["best"] for k in ("t1n", "t2n"))
    if same_day and same_day.get("protocol"):
        outcome, why = "found", "You saved this game's report before"
    elif both and (tour or friendly or kb["comp_sources"].get(p["compn"])):
        outcome, why = "check", "Sources ready for both teams, open them in order"
    elif both:
        outcome, why = "check", "Teams known, competition unknown, start from team schedules"
    else:
        missing = [n for n, k in ((p["t1"], "t1n"), (p["t2"], "t2n")) if not tm[k]["best"]]
        outcome, why = "not_found", "Unknown team: " + ", ".join(missing)
    return dict(ok=True, parsed=p, outcome=outcome, why=why, steps=steps, teams=tm,
                tournament=None if friendly else tour, friendly=friendly)


CHECK_ROSTERS = True   # the report replay turns this off; rosters have their own test
USE_MEMORY = True      # remembered results; the replay turns this off so every game is searched for real


async def search_full(header, kb, rosters=True):
    """Manual source list plus the automatic adapters."""
    r = search(header, kb)
    if not r["ok"]:
        return r
    from .adapters import run_all
    from . import learning
    p = r["parsed"]
    mem = None
    try:
        mem = learning.remembered(p) if USE_MEMORY else None
    except Exception:
        pass
    if mem and mem["confirmed"]:
        # you saved this game's report before: answer at once
        auto = dict(verdict="found", candidates=[], adapters=[], seconds=0, fast=True, remembered=True,
                    best=dict(url=mem["url"], site=domain(mem["url"]), source=mem["source"] or "saved", home=p["t1"], away=p["t2"], date=p["date"],
                              confidence=1.0, team_scores=[1.0, 1.0], kind="protocol", reasons=["you saved this report for this game earlier"]))
    else:
        try:
            auto = await run_all(p, r["teams"], kb)
        except Exception as e:
            auto = dict(verdict="none", best=None, candidates=[], adapters=[], seconds=0, error=str(e)[:200])
        if auto["verdict"] == "found" and USE_MEMORY:
            try:
                learning.remember(p, header, auto["best"]["url"], auto["best"].get("adapter") or auto["best"].get("source"), confirmed=False)
            except Exception:
                pass
    r["auto"] = auto
    if rosters and CHECK_ROSTERS and auto["verdict"] != "found" and not (r["outcome"] == "found"):
        from .rosters import check_rosters
        try:
            r["roster_check"] = await check_rosters(r["parsed"], r["teams"])
        except Exception:
            r["roster_check"] = None
    if auto["verdict"] == "found" and r["outcome"] != "found":
        r["outcome"] = "found"
        r["why"] = f"Report found automatically on {auto['best']['site']} ({auto['best']['confidence']:.0%} confidence)"
    elif auto["verdict"] == "check" and r["outcome"] == "not_found":
        r["outcome"] = "check"
        r["why"] = "Possible games found automatically, pick the right one"
    return r


# ------------------------------------------------------------ learning

def save_result(kb, parsed, protocol, t1link, t2link, comp_site=None):
    """Remember what the analyst found so the next search is faster."""
    hist = [h for h in kb.setdefault("history", []) if not (h["t1n"] == parsed["t1n"] and h["t2n"] == parsed["t2n"] and h["date"] == parsed["date"])]
    import datetime
    hist.append(dict(t1=parsed["t1"], t2=parsed["t2"], t1n=parsed["t1n"], t2n=parsed["t2n"], comp=parsed["comp"], date=parsed["date"],
                     protocol=protocol or None, t1link=t1link or None, t2link=t2link or None,
                     saved_at=datetime.datetime.now().isoformat(timespec="seconds")))
    kb["history"] = hist[-5000:]
    d = domain(protocol)
    if d:
        kb["comp_sources"].setdefault(parsed["compn"], {})
        kb["comp_sources"][parsed["compn"]][d] = kb["comp_sources"][parsed["compn"]].get(d, 0) + 1
        for k in ("t1n", "t2n"):
            kb["team_sources"].setdefault(parsed[k], {})
            kb["team_sources"][parsed[k]][d] = kb["team_sources"][parsed[k]].get(d, 0) + 1
    for k, name, link in (("t1n", parsed["t1"], t1link), ("t2n", parsed["t2"], t2link)):
        if link:
            t = kb["teams"].setdefault(parsed[k], dict(name=name, site=None, schedule=None, mhr=None, ep=None, roster=None, country=None))
            field = "ep" if "eliteprospects" in link else "mhr" if "myhockeyrankings" in link else "roster"
            t[field] = link
    if comp_site and not is_friendly(parsed["comp"]):
        t = kb["tournaments"].setdefault(parsed["compn"], dict(name=parsed["comp"], site=None, schedule=None, mhr=None, ep=None))
        t["schedule"] = t["schedule"] or comp_site
    m = re.search(r"gamesheetstats\.com/seasons/(\d+)", protocol or "")
    if m:
        kb.setdefault("gamesheet_seasons", {}).setdefault(m.group(1), dict(name=parsed["comp"], first=parsed["date"], last=parsed["date"], harvested=None))
        s = kb["gamesheet_seasons"][m.group(1)]
        s["first"] = min(s.get("first") or parsed["date"], parsed["date"]); s["last"] = max(s.get("last") or parsed["date"], parsed["date"])
    save_kb(kb)


async def learn_from_link(protocol: str, parsed: dict | None = None):
    """Site-specific facts worth keeping from a saved report link."""
    if parsed and "ligasy.kz" in (protocol or ""):
        from .adapters.ligasy import learn
        learn(protocol, parsed["t1"], parsed["t2"])
    m = re.search(r"deb-online\.live/.*divisionId=(\d+)", protocol or "")
    if m:
        from .adapters.deb import remember_division
        await remember_division(m.group(1))
    m = re.search(r"ceskyhokej\.cz/game/detail/(\d+)", protocol or "")
    if m:
        from .fetch import get_text
        from .adapters.ceskyhokej import remember_competition
        page = await get_text(f"https://ceskyhokej.cz/game/detail/{m.group(1)}", ttl=3600)
        if page:
            cid = re.search(r"competition/games/(\d+)", page)
            h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, flags=re.S)
            if cid:
                import html as _html
                remember_competition(cid.group(1), re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", _html.unescape(h1.group(1)))).strip() if h1 else None)

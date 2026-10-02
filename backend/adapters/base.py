"""Adapter contract and the shared scoring of a candidate game."""
from __future__ import annotations
import datetime, difflib, re
from ..lookup import tokens, norm


def local_to_utc(date: str, hhmm: str | None, tz: str) -> str | None:
    """'2026-09-12', '19:30', 'Europe/Stockholm' -> '2026-09-12T17:30' (UTC)."""
    if not date or not hhmm:
        return None
    try:
        from zoneinfo import ZoneInfo
        h, m = int(hhmm[:-2] if ":" not in hhmm else hhmm.split(":")[0]), int(hhmm[-2:])
        local = datetime.datetime.fromisoformat(date).replace(hour=h, minute=m, tzinfo=ZoneInfo(tz))
        return local.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M")
    except Exception:
        return None


def header_utc(parsed: dict) -> datetime.datetime | None:
    """HokReg header times are Moscow time (UTC+3), measured on German and HockeyTech games."""
    if not parsed.get("time") or parsed["time"] == "00:00":    # midnight is often a placeholder
        return None
    try:
        return datetime.datetime.fromisoformat(f"{parsed['date']}T{parsed['time']}") - datetime.timedelta(hours=3)
    except Exception:
        return None


def days_apart(a: str, b: str) -> int | None:
    try:
        return abs((datetime.date.fromisoformat(a) - datetime.date.fromisoformat(b)).days)
    except Exception:
        return None


import json as _json
from pathlib import Path as _Path
_ALIAS_FILE = _Path(__file__).resolve().parent.parent.parent / "data" / "aliases.json"
_ALIASES = None


def aliases() -> dict:
    """norm(short) -> [norm(full), ...] in both directions, loaded once."""
    global _ALIASES
    if _ALIASES is None:
        _ALIASES = {}
        for f in (_ALIAS_FILE, _ALIAS_FILE.with_name("aliases_learned.json")):   # hand-made, then learned from saves
            try:
                for k, v in _json.loads(f.read_text()).items():
                    if k.startswith("_"):
                        continue
                    kb = tokens(k)[0]
                    for full in v:
                        fb = tokens(full)[0]
                        _ALIASES.setdefault(kb, set()).add(fb)
                        _ALIASES.setdefault(fb, set()).add(kb)
            except Exception:
                pass
    return _ALIASES


STATES = {"ct": "connecticut", "nj": "new jersey", "ny": "new york", "nyc": "new york city", "pa": "pennsylvania", "ma": "massachusetts",
          "mn": "minnesota", "mi": "michigan", "il": "illinois", "oh": "ohio", "va": "virginia", "md": "maryland", "nh": "new hampshire",
          "ri": "rhode island", "vt": "vermont", "me": "maine", "de": "delaware", "dc": "washington", "la": "los angeles", "sj": "san jose",
          "ne": "new england", "li": "long island", "wny": "western new york", "stl": "st louis", "kc": "kansas city", "ab": "alberta",
          "bc": "british columbia", "on": "ontario", "qc": "quebec", "sk": "saskatchewan", "mb": "manitoba", "ns": "nova scotia"}


WORD_SYN = {"indy": "Indiana", "philly": "Philadelphia", "nova": "Northern Virginia", "socal": "Southern California",
            "norcal": "Northern California", "vegas": "Las Vegas", "jrs": "Juniors", "mtl": "Montreal", "tor": "Toronto"}


def expand_acronyms(short: str, full: str) -> str:
    """'PTL Black 14U' against 'Princeton Tiger Lilies Black 14U' -> 'Princeton Tiger Lilies Black 14U':
    an upper-case word of 2-4 letters (dots allowed) becomes the run of words in the other name
    whose initials spell it; known state/city codes are expanded too."""
    fw = re.findall(r"[A-Za-z][A-Za-z'.]*", full or "")
    out = []
    for w in (short or "").split():
        bare = w.replace(".", "")
        rep = None
        if re.fullmatch(r"[A-Z]{2,4}", bare):
            n = len(bare)
            for i in range(len(fw) - n + 1):
                if "".join(x[0] for x in fw[i:i + n]).upper() == bare and all(len(x) > 1 for x in fw[i:i + n]):
                    rep = " ".join(fw[i:i + n])
                    break
            if not rep and bare.lower() in STATES:
                rep = STATES[bare.lower()].title()
        if not rep and bare.lower() in WORD_SYN and WORD_SYN[bare.lower()].split()[0].lower() in (full or "").lower():
            rep = WORD_SYN[bare.lower()]
        out.append(rep or w)
    return " ".join(out)


import functools as _ft


@_ft.lru_cache(maxsize=200000)
def team_similarity(header_name: str, site_name: str, context: str = "") -> tuple[float, str]:
    return _team_similarity_all(header_name or "", site_name or "", context or "")


def _team_similarity_all(header_name: str, site_name: str, context: str = "") -> tuple[float, str]:
    """Best of the direct comparison, the comparison with acronyms spelled out, and any alias of the site's name."""
    best = _team_similarity(header_name, site_name, context)
    for h2, s2_ in ((header_name, expand_acronyms(site_name, header_name)), (expand_acronyms(header_name, site_name), site_name)):
        if (h2, s2_) != (header_name, site_name):
            sc, rc = _team_similarity(h2, s2_, context)
            if sc > best[0]:
                best = (sc, rc + " (abbreviation spelled out)")
    sb = tokens(site_name)[0]
    hb = tokens(header_name)[0]
    for alt in aliases().get(sb, ()):
        if alt and (alt == hb or alt in hb or hb in alt):
            ages = "".join(f" U{a}" for a in sorted(tokens(site_name)[1]))
            s2, r2 = _team_similarity(header_name, alt + ages, context)
            if s2 > best[0]:
                best = (s2, r2 + " (via alias)")
    return best


COLOURS = re.compile(r"\b(gold|black|white|red|blue|navy|green|silver|orange|purple|grey|gray|maroon|teal|yellow|royal|tier\s?[12i]+|[ab]\d?|elite\s?\d|premier|select)\b", re.I)


def _colours(name: str) -> set:
    return {c.lower().replace(" ", "") for c in COLOURS.findall(name or "")} - {"a", "b"}


def _team_similarity(header_name: str, site_name: str, context: str = "") -> tuple[float, str]:
    s, r = __team_similarity(header_name, site_name, context)
    # colour words that both names share are part of the club name ("Red Bank", "PAL Blue Knights")
    both = _colours(header_name) & _colours(site_name)
    hc, sc = _colours(header_name) - both, _colours(site_name) - both
    if hc and sc:
        return s * 0.3, f"team colour or tier differs: header {sorted(hc)} vs site {sorted(sc)}"
    if hc and not sc and s > 0.75:
        return 0.75, f"{r}; header names {sorted(hc)} but site does not"
    if sc and not hc and s > 0.8:
        return 0.8, f"{r}; site names {sorted(sc)} but header does not"
    return s, r


STOP = {"hc", "hk", "sk", "if", "ifk", "ik", "bk", "hf", "ec", "ehc", "ev", "jr", "junior", "of", "the", "club", "team", "hockey", "de", "la", "le", "aaa", "aa", "a"}


def _words(name: str) -> list:
    from ..lookup import strip_accents
    s = strip_accents(str(name or "")).lower()
    s = re.sub(r"\b(?:u|j)\s?\d{1,2}\b|\b\d{1,2}\s?u\b|\b20[01]\d\b", " ", s)
    return [w for w in re.findall(r"[a-z0-9]+", s) if w not in STOP]


def __team_similarity(header_name: str, site_name: str, context: str = "") -> tuple[float, str]:
    s, r = ___team_similarity(header_name, site_name, context)
    hw, sw = _words(header_name), _words(site_name)
    # sponsor or city words on one side: "Red Deer Chiefs" vs "Red Deer Sutter Fund Chiefs",
    # "University of Korea" vs "Korea University". Needs two words and the same nickname.
    if s < 0.9 and len(hw) >= 2 and len(sw) >= 2 and not r.startswith(("age group differs", "level differs", "league is for")):
        if set(hw) <= set(sw) and hw[-1] in sw:
            return max(s, 0.9 * (0.85 if "not shown on site" in r else 1.0)), r + "; site adds words"
        if set(sw) <= set(hw) and sw[-1] in hw:
            return max(s, 0.85 * (0.85 if "not shown on site" in r else 1.0)), r + "; header adds words"
    # the same words in another order, allowing word endings: "Ice Dragons Herford" / "Herforder Ice Dragons"
    def _wm(a, b):
        return a == b or (len(a) >= 5 and len(b) >= 5 and (a.startswith(b) or b.startswith(a)))
    if s < 0.9 and len(hw) >= 2 and len(sw) >= 2 and not r.startswith(("age group differs", "level differs", "league is for")):
        if all(any(_wm(x, y) for y in sw) for x in hw) and all(any(_wm(y, x) for x in hw) for y in sw):
            return max(s, 0.95 * (0.85 if "not shown on site" in r else 1.0)), r + "; same words in another order"
    # a one-word club name that is the site's last word, after a sponsor: "Plzen" / "HC Skoda Plzen"
    if s < 0.85 and len(hw) == 1 and len(hw[0]) >= 5 and 2 <= len(sw) <= 3 and sw[-1] == hw[0] \
            and not r.startswith(("age group differs", "level differs", "league is for")):
        return 0.85 * (0.85 if "not shown on site" in r else 1.0), r + "; site adds a sponsor word"
    return s, r


def _season_year() -> int:
    t = datetime.date.today()
    return t.year if t.month >= 7 else t.year - 1


def _same_age(a: set, b: set) -> bool:
    """A birth-year team and an age-group team can be the same: in the 2026-27 season a
    '2016' team plays 10U (USA Hockey) or U11 (Hockey Canada)."""
    s = _season_year()
    def ages(x):
        out = set()
        for t in x:
            if len(t) == 4:
                out |= {str(s - int(t)), str(s + 1 - int(t))}
            else:
                out.add(t)
        return out
    years_a, years_b = {t for t in a if len(t) == 4}, {t for t in b if len(t) == 4}
    if years_a and years_b:
        return years_a == years_b
    return bool(ages(a) & ages(b)) and (bool(years_a) != bool(years_b))


def ___team_similarity(header_name: str, site_name: str, context: str = "") -> tuple[float, str]:
    """How well a site's team name matches the header's, with age/level rules.
    Returns (score 0..1, reason). Age tokens must agree when both sides have them;
    if the site name lacks them, the competition/league context may supply them."""
    hb, ha, hl = tokens(header_name)
    sb, sa, sl = tokens(site_name)
    cb, ca, cl = tokens(context) if context else ("", set(), set())
    if not hb or not sb:
        return 0.0, "empty name"
    base = difflib.SequenceMatcher(None, hb, sb).ratio()
    if hb == sb:
        base = 1.0
    elif hb in sb or sb in hb:
        base = max(base, 0.85)
    if ha and sa and ha != sa and not _same_age(ha, sa):
        return base * 0.3, f"age group differs: header {sorted(ha)} vs site {sorted(sa)}"
    if ha and not sa:
        if ha & ca or (ca and _same_age(ha, ca)):
            reason = "age group confirmed by league name"
        elif ca:
            return base * 0.3, f"league is for age group {sorted(ca)}, header {sorted(ha)}"
        else:
            base *= 0.85
            reason = "age group not shown on site"
    elif not ha and (sa or ca):
        # header is a senior team but the site shows a youth game of the same club
        base *= 0.6
        reason = f"site shows age group {sorted(sa or ca)}, header has none"
    else:
        reason = "age group matches" if ha else "no age group"
    if hl and sl and hl != sl:
        conflict = ({"aa", "aaa"} <= (hl | sl) and not ({"aa", "aaa"} <= hl or {"aa", "aaa"} <= sl)) or not (hl <= sl or sl <= hl)
        if conflict:
            return base * 0.5, f"level differs: header {sorted(hl)} vs site {sorted(sl)}"
        base *= 0.95
        reason += f"; level partly shown ({sorted(hl)} vs {sorted(sl)})"
    return base, reason


def _hdr_team(parsed: dict, key: str, site_name: str, ctx: str) -> tuple:
    """(score, reason, overridden). HokReg headers sometimes carry the wrong age for one team
    ("Seacoast 16U AAA" in a "USA Hockey 13U" game). When the header age conflicts with the
    site but the competition hint's age agrees with the site, score the team with the
    competition's age instead and flag it; score_candidate caps such a team below strong
    unless the start time confirms the game."""
    name = parsed[key]
    sc, r = team_similarity(name, site_name, ctx)
    if not r.startswith(("age group differs", "league is for age group")):
        return sc, r, False
    # the reference age: the competition hint's, else (friendlies) the other header team's
    comp_ages = tokens(parsed.get("comp", ""))[1] or tokens(parsed["t2" if key == "t1" else "t1"])[1]
    site_ages = tokens(site_name)[1] or tokens(ctx)[1]
    if not comp_ages or not site_ages or not (comp_ages & site_ages or _same_age(comp_ages, site_ages)):
        return sc, r, False
    head_ages = tokens(name)[1]
    if head_ages & comp_ages or _same_age(head_ages, comp_ages):
        return sc, r, False                       # the header agrees with its reference: a real conflict
    import re as _re
    from ..lookup import AGE_RE
    fixed = AGE_RE.sub(" ", name) + " " + next(iter(sorted(comp_ages))) + ("" if len(next(iter(sorted(comp_ages)))) == 4 else "U")
    s2, r2 = team_similarity(_re.sub(r"\s+", " ", fixed).strip(), site_name, ctx)
    if s2 <= sc:
        return sc, r, False
    u = lambda a: "/".join(x if len(x) == 4 else x + "U" for x in sorted(a))
    return s2, f"{r2}; header says {u(head_ages)}, competition and game say {u(site_ages)}", True


def score_candidate(cand: dict, parsed: dict, context: str = "") -> dict:
    """Attach confidence and reasons. cand needs home, away, date (ISO), url, source."""
    reasons = []
    ctx = context or cand.get("league", "")
    (s1, r1, o1), (s2, r2, o2) = _hdr_team(parsed, "t1", cand["home"], ctx), _hdr_team(parsed, "t2", cand["away"], ctx)
    swapped = False
    (x1, q1, p1), (x2, q2, p2) = _hdr_team(parsed, "t1", cand["away"], ctx), _hdr_team(parsed, "t2", cand["home"], ctx)
    if x1 + x2 > s1 + s2:
        (s1, r1, o1), (s2, r2, o2), swapped = (x1, q1, p1), (x2, q2, p2), True
    reasons.append(f"team 1: {s1:.0%} ({r1})")
    reasons.append(f"team 2: {s2:.0%} ({r2})")
    if swapped:
        reasons.append("home and away are swapped on the site")
    d = days_apart(parsed["date"], cand.get("date") or "")
    hu, cu = header_utc(parsed), None
    try:
        cu = datetime.datetime.fromisoformat(cand["start_utc"]) if cand.get("start_utc") else None
    except Exception:
        pass
    if hu and cu:
        cand["time_diff_min"] = round((cu - hu).total_seconds() / 60)
    if hu and cu and abs((cu - hu).total_seconds()) <= 6000:
        dscore, dr = 1.0, "start time matches the header"
    elif d is None:
        dscore, dr = 0.6, "date not readable"
    elif d == 0:
        dscore, dr = 1.0, "same date"
    elif d == 1:
        dscore, dr = 0.85, "one day off (time zone)"
    else:
        dscore, dr = 0.0, f"{d} days off"
    reasons.append(dr)
    if o1 or o2:
        # a header age that contradicts its own competition is trusted only for one team, when the
        # other team matches by name and age, and the start time is the header's to 15 minutes
        exact = abs(cand.get("time_diff_min", 999)) <= 15
        other_ok = (not o1 and s1 >= 0.9 and r1.startswith("age group")) or (not o2 and s2 >= 0.9 and r2.startswith("age group"))
        cap = 0.8 if (exact and other_ok and not (o1 and o2)) else 0.7
        s1, s2 = (min(s1, cap) if o1 else s1), (min(s2, cap) if o2 else s2)
        reasons.insert(2, "header age is probably wrong: the competition and the game agree on another age"
                       + ("" if exact else "; start time not confirmed, so check it"))
    conf = min(s1, s2) * 0.75 + dscore * 0.25
    if dscore == 0:
        conf = min(conf, 0.3)
    if hu and cu and abs((cu - hu).total_seconds()) > 5 * 3600:
        # noted only: header times are not always the start time (Swedish friendlies), so this
        # counts only when another game of these teams does start at the header's time (decide())
        reasons.append(f"starts {abs((cu - hu).total_seconds()) / 3600:.0f} h away from the header time")
    cand["confidence"] = round(conf, 2)
    cand["reasons"] = reasons
    cand["team_scores"] = [round(s1, 2), round(s2, 2)]
    return cand


def strong(c: dict) -> bool:
    return c["confidence"] >= 0.85 and min(c["team_scores"]) >= 0.8


TIME_MATCH = "start time matches the header"


def anchored(c: dict) -> bool:
    """A team plays one game at a time: one team clearly matching (age and level included) and
    the exact start time identify the game even when the opponent's name is written differently."""
    return (TIME_MATCH in c.get("reasons", []) and abs(c.get("time_diff_min", 999)) <= 20
            and max(c["team_scores"]) >= 0.9 and min(c["team_scores"]) >= 0.3)


def keep(c: dict) -> bool:
    """Worth showing: both teams plausible, or anchored by the start time."""
    return min(c["team_scores"]) >= 0.6 or anchored(c)


class Adapter:
    name = "base"
    site = ""

    def applies(self, parsed: dict, tm: dict, kb: dict) -> bool:
        return False

    async def find(self, parsed: dict, tm: dict, kb: dict) -> list[dict]:
        return []

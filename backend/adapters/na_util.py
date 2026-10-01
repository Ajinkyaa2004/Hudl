"""Small helpers shared by the North American adapters (ramp, regystra, kreezee, ayhl,
digitalshift). Not an adapter itself."""
from __future__ import annotations
import datetime, re
from .base import header_utc


def local_date(parsed: dict, tz: str) -> str:
    """The header's game date in the site's local time zone (header times are Moscow time).
    Without a time: the day before, as North American evening games carry the next date."""
    hu = header_utc(parsed)
    if hu is None:
        return (datetime.date.fromisoformat(parsed["date"]) - datetime.timedelta(days=1)).isoformat()
    try:
        from zoneinfo import ZoneInfo
        return hu.replace(tzinfo=datetime.timezone.utc).astimezone(ZoneInfo(tz)).date().isoformat()
    except Exception:
        return parsed["date"]


def near(parsed: dict, date: str | None, days: int = 1) -> bool:
    """date within `days` of the header date (either side)."""
    try:
        return abs((datetime.date.fromisoformat(date) - datetime.date.fromisoformat(parsed["date"])).days) <= days
    except Exception:
        return False


def clock24(text: str | None) -> str | None:
    """'5:40 PM' / '07:15 AM' / '17:40' / '5:20 p.m.' -> '17:40'."""
    if not text:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})\s*([ap])?\.?\s*m?\.?", text, re.I)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), m.group(2), (m.group(3) or "").lower()
    if ap == "p" and h < 12:
        h += 12
    if ap == "a" and h == 12:
        h = 0
    return f"{h:02d}:{mi}"


AGE_IN_DIV = re.compile(r"\b(?:u\s?(\d{1,2})|(\d{1,2})\s?u|(\d{1,2})\s?o)\b", re.I)
BIRTH_YEAR = re.compile(r"\b(20[01]\d)\b")


def division_age(div: str, season_start_year: int = 2026) -> tuple[str | None, bool]:
    """Age group of a division name as 'NNU', and whether it came from a birth year
    ('2014 Jetspeed' -> ('12U', True) in the 2026-27 season)."""
    m = AGE_IN_DIV.search(div or "")
    if m:
        return f"{next(g for g in m.groups() if g)}U", False
    m = BIRTH_YEAR.search(div or "")
    if m:
        return f"{season_start_year - int(m.group(1))}U", True
    m = re.match(r"\s*(\d{1,2})\b", div or "")     # AYHL '15 Pure 11'
    if m and 6 <= int(m.group(1)) <= 21:
        return f"{m.group(1)}U", False
    return None, False


def header_ages(name: str) -> set:
    from ..lookup import tokens
    return {int(a) for a in tokens(name)[1] if a.isdigit() and int(a) < 30}


def age_close(parsed: dict, age: str | None, slack: int = 1) -> bool:
    """False when a header team carries an age more than `slack` away from `age`
    (teams without an age in the header do not count)."""
    if not age:
        return True
    a = int(age.rstrip("Uu"))
    ha = header_ages(parsed["t1"]) | header_ages(parsed["t2"])
    return all(abs(a - x) <= slack for x in ha)


def swap_words(name: str, table: dict) -> str:
    """Whole-word replacements ({'Little Capitals': 'Little Caps'}), case-insensitive."""
    out = name or ""
    for k, v in table.items():
        out = re.sub(r"(?<![A-Za-z])" + re.escape(k) + r"(?![A-Za-z])", v, out, flags=re.I)
    return re.sub(r"\s+", " ", out.replace("..", ".")).strip()


def score_local(cand: dict, parsed: dict, league: str, tz: str) -> dict:
    """score_candidate against the header's LOCAL game date: a header at 00:40 Moscow time is a
    17:40 game the evening before in New York, and should not favour next day's rematch."""
    from .base import score_candidate
    p = dict(parsed)
    if parsed.get("time"):
        p["date"] = local_date(parsed, tz)
    return score_candidate(cand, p, league)


def age_level(name: str) -> str:
    """'Texas Tigers 14UA' -> 'Texas Tigers 14U A'; '12UAA' -> '12U AA' (the scorer reads the age then)."""
    return re.sub(r"\b(\d{1,2})U(A{1,3}\d?)\b", r"\1U \2", name or "", flags=re.I)

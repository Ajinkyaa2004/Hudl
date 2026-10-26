"""Leagues whose sites block automatic reading (a "verify you are human" check in front of every
page), so PuckTrace can't list their games: RSEQ school hockey (scolaire.rseqhockey.com, a Spordle
page) and Winnipeg AAA (winnipegaaa.com). The answer is the league's schedule to open, with the
game's teams and local start time, so the analyst goes straight to it.

RSEQ: the number in an RSEQ game link (scolaire.rseqhockey.com/fr/schedule/912048) is the Spordle
game number, and Spordle's match sheet opens freely at pdf.play.spordle.com/game/912048: the page
turns the number the analyst reads into that link."""
from __future__ import annotations
import re
from .base import Adapter, header_utc

LEAGUES = [
    dict(match=re.compile(r"\bRSEQ\b", re.I), site="scolaire.rseqhockey.com", name="RSEQ hockey scolaire",
         url="https://scolaire.rseqhockey.com/fr/-rseq/schedule-stats-standings", tz="America/Toronto", tz_name="Québec",
         pdf="https://pdf.play.spordle.com/game/"),
    dict(match=re.compile(r"\bWinnipeg AAA\b", re.I), site="winnipegaaa.com", name="Winnipeg AAA Hockey",
         url="https://winnipegaaa.com/schedule", tz="America/Winnipeg", tz_name="Winnipeg"),
]


def league_for(parsed: dict) -> dict | None:
    return next((x for x in LEAGUES if x["match"].search(parsed.get("comp") or "")), None)


def local_start(parsed: dict, lg: dict) -> tuple[str, str | None]:
    """(local date, 'Fri 23 Oct, 7:30 pm Winnipeg time') from the header's Moscow time."""
    hu = header_utc(parsed)
    if not hu:
        return parsed["date"], None
    import datetime
    from zoneinfo import ZoneInfo
    t = hu.replace(tzinfo=datetime.timezone.utc).astimezone(ZoneInfo(lg["tz"]))
    return t.date().isoformat(), f"{t:%a} {t.day} {t:%b}, {t.hour % 12 or 12}:{t:%M} {'am' if t.hour < 12 else 'pm'} {lg['tz_name']} time"


class Blocked(Adapter):
    name = "blocked league"
    site = "league schedule"

    def applies(self, parsed, tm, kb):
        return league_for(parsed) is not None

    async def find(self, parsed, tm, kb):
        lg = league_for(parsed)
        date, when = local_start(parsed, lg)
        how = (f"find {parsed['t1']} vs {parsed['t2']}" + (f" ({when})" if when else f" on {date}")
               + " on its schedule and open the game")
        if lg.get("pdf"):
            how += "; its match sheet is pdf.play.spordle.com/game/ + the game number on the RSEQ page (type it below)"
        return [dict(source=self.name, adapter=self.name, site=lg["site"], league=lg["name"], home=parsed["t1"], away=parsed["t2"],
                     date=date, when=when, kind="schedule", url=lg["url"], confidence=0.5, team_scores=[0.0, 0.0],
                     blocked=lg["site"], pdf=lg.get("pdf"),
                     reasons=[f"{lg['site']} blocks automatic reading, so PuckTrace can't see its games: {how}"])]

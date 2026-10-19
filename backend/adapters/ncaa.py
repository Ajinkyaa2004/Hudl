"""NCAA college hockey (ncaa.com): the scoreboard page for a sport, division and date embeds every
game of that day, NCAA games and exhibitions against Canadian universities alike: game number,
start time, home and away team, score. Report: https://www.ncaa.com/game/{id} (box score).

Scoreboard: https://www.ncaa.com/scoreboard/icehockey-{men|women}/{d1|d3}/YYYY/MM/DD
The NCAA writes schools short ("Northern Mich.", "Penn St.", "Miami (OH)"); headers write them in
full with the nickname ("Northern Michigan Wildcats"): the short names are spelled out here."""
from __future__ import annotations
import asyncio, datetime, html, re
from ..fetch import get_text
from .base import Adapter, score_candidate, _colours

PAGE = "https://www.ncaa.com/scoreboard/{sport}/{div}/{y}/{m:02d}/{d:02d}"
GAME = "https://www.ncaa.com/game/{gid}"
CONTEST = re.compile(r'"contestId":(\d+),"url":"[^"]*".*?"startTimeEpoch":(\d+|null).*?"startDate":"([^"]+)".*?"teams":\[(.*?)\],"liveVideos"', re.S)
TEAM = re.compile(r'"isHome":(true|false),"seoname":"([^"]*)","nameShort":"([^"]*)".*?"score":(\d+|null)', re.S)

# schools the NCAA writes in a form no general rule spells out
SCHOOLS = {"rit": "Rochester Institute of Technology", "liu": "Long Island University", "uconn": "UConn",
           "omaha": "Nebraska Omaha", "army west point": "Army", "boston u.": "Boston University",
           "colorado col.": "Colorado College", "umass lowell": "UMass Lowell Massachusetts Lowell River Hawks",
           "massachusetts": "UMass Massachusetts Minutemen", "saint michael's": "St. Michael's College", "post": "Post",
           "concordia-montreal": "Concordia", "minnesota st.": "Minnesota State Mankato", "minn. duluth": "Minnesota Duluth",
           "rensselaer": "Rensselaer Polytechnic Institute RPI"}
WORDS = [(r"\bAlas\.", "Alaska"), (r"\bMinn\.", "Minnesota"), (r"\bMich\.", "Michigan"), (r"\bCol\.", "College"),
         (r"\bU\.", "University"), (r"\bNeb\.", "Nebraska"), (r"\bSt\.$", "State"), (r"\bSt\. (?=\S+$)", "St. ")]


def school(short: str) -> str:
    s = html.unescape(short.encode().decode("unicode_escape") if "\\u" in short else short).strip()
    if s.lower() in SCHOOLS:
        return SCHOOLS[s.lower()]
    s = re.sub(r"\s*\((?:[A-Z]{2}|CAN)\)\s*$", "", s)          # "Miami (OH)", "Brock (CAN)"
    if re.search(r"\bSt\.$", s):                               # "Penn St." -> "Penn State"; "St. Lawrence" stays
        s = re.sub(r"\bSt\.$", "State", s)
    for pat, rep in WORDS[:-2]:
        s = re.sub(pat, rep, s)
    return s


def parse(page: str) -> list[dict]:
    out = []
    for m in CONTEST.finditer(page or ""):
        teams = TEAM.findall(m.group(4))
        if len(teams) != 2:
            continue
        home = next((t for t in teams if t[0] == "true"), teams[0])
        away = next((t for t in teams if t is not home), teams[1])
        epoch = None if m.group(2) == "null" else int(m.group(2))
        mm, dd, yy = m.group(3).replace("\\/", "/").split("/")
        out.append(dict(gid=m.group(1), date=f"{yy}-{mm}-{dd}", epoch=epoch, home=school(home[2]), away=school(away[2]),
                        score=None if "null" in (home[3], away[3]) else f"{home[3]}-{away[3]}"))
    return out


class NCAA(Adapter):
    name = "ncaa"
    site = "ncaa.com"

    def applies(self, parsed, tm, kb):
        from ..lookup import is_friendly
        c = parsed["comp"].lower()
        if re.search(r"\bncaa\b|college|universit|u sports|usports", c):
            return True
        # exhibitions: Canadian universities and club teams visit NCAA programmes in the autumn
        names = (parsed["t1"] + " " + parsed["t2"]).lower()
        return (is_friendly(parsed["comp"]) or "friendl" in c) and bool(re.search(r"universit|college|state\b|institute|academy|\bu\b", names)) \
            or bool(re.search(r"\bncaa\b", names))

    async def find(self, parsed, tm, kb):
        d0 = datetime.date.fromisoformat(parsed["date"])
        days = [d0, d0 - datetime.timedelta(days=1)]           # evening games in America are the next day in Moscow
        c = parsed["comp"].lower()
        sports = ["icehockey-women"] if re.search(r"\(w\)|women|woman|female|girls", c) else \
                 ["icehockey-men"] if re.search(r"\bncaa division i\b(?! \(w)", c) and "women" not in c else ["icehockey-men", "icehockey-women"]
        divs = ["d3"] if re.search(r"division iii|\bd3\b", c) else ["d1", "d3"] if re.search(r"friendl|exhibition", c) else ["d1"]
        pages = await asyncio.gather(*[get_text(PAGE.format(sport=s, div=dv, y=d.year, m=d.month, d=d.day), ttl=1800)
                                       for s in sports for dv in divs for d in days])
        out, seen = [], set()
        for page in pages:
            for g in parse(page or ""):
                if g["gid"] in seen:
                    continue
                seen.add(g["gid"])
                start = datetime.datetime.utcfromtimestamp(g["epoch"]).strftime("%Y-%m-%dT%H:%M") if g["epoch"] else None
                cand = dict(source=self.name, site=self.site, league="NCAA", home=g["home"], away=g["away"], date=g["date"],
                            start_utc=start, score=g["score"], status="final" if g["score"] else None, kind="protocol",
                            adapter=self.name, url=GAME.format(gid=g["gid"]))
                # colour words in nicknames (Black Bears, Blue Hens) are not team tiers: the NCAA never
                # shows them, so they are given as context and count as shared
                score_candidate(cand, parsed, "NCAA " + " ".join(sorted(_colours(parsed["t1"]) | _colours(parsed["t2"]))))
                if min(cand["team_scores"]) >= 0.6 or max(cand["team_scores"]) >= 0.9:
                    out.append(cand)
        return out

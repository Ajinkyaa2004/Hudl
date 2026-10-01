"""ceskyhokej.cz (Czechia): per-competition game list for a date, fetched with
the site's calendar widget (POST, XHR). Competition ids live in
data/ceskyhokej_competitions.json and grow when a Czech game link is saved."""
from __future__ import annotations
import re, html, json, asyncio
from pathlib import Path
import httpx
from ..fetch import UA, _cached, _store
from .base import Adapter, score_candidate

DATA = Path(__file__).resolve().parent.parent.parent / "data" / "ceskyhokej_competitions.json"
ITEM_RE = re.compile(r'<a href="/game/detail/(\d+)" class="game-item">(.*?)</a>', re.S)


def competitions() -> dict:
    try:
        return json.loads(DATA.read_text()).get("competitions", {})
    except Exception:
        return {}


def remember_competition(cid: str, name: str | None):
    d = competitions()
    if cid not in d:
        d[cid] = name
        DATA.write_text(json.dumps(dict(competitions=d), ensure_ascii=False, indent=1))


async def games_for(cid: str, date: str, client: httpx.AsyncClient):
    key = ("cz", cid, date)
    hit = _cached(key, 1800)
    if hit is not None:
        return hit
    try:
        if cid not in getattr(client, "_cz_seen", set()):      # the widget keeps per-competition state in the session
            await client.get(f"https://ceskyhokej.cz/competition/games/{cid}")
            client._cz_seen = getattr(client, "_cz_seen", set()) | {cid}
        r = await client.post(f"https://ceskyhokej.cz/competition/games/{cid}?do=gameCalendarWidget-changeDate", data={"gameCalendarWidget-date": date, "competitionPart": "", "round": "", "club": ""}, headers={"X-Requested-With": "XMLHttpRequest"})
        body = " ".join(r.json().get("snippets", {}).values())
    except Exception:
        return []
    # the widget answers with the nearest day that has games; only trust an exact date
    h4 = re.search(r"<h4[^>]*>[^<]*?(\d{1,2})\. (\d{1,2})\. (\d{4})</h4>", body)
    if not h4 or f"{h4.group(3)}-{int(h4.group(2)):02d}-{int(h4.group(1)):02d}" != date:
        _store(key, [])
        return []
    out = []
    for gid, frag in ITEM_RE.findall(body):
        t = html.unescape(re.sub(r"<[^>]+>", "|", frag))
        cells = [c.strip() for c in t.split("|") if c.strip()]
        # "Home - Away | Home | 7 | 2 | Away | 2:0, 2:1 ..." (score cells only when played);
        # a status cell such as "SN" (shootout) or "PP" (overtime) can come first
        pair = next((c for c in cells if re.match(r".+?\s+-\s+.+", c)), None)
        names = re.match(r"(.+?)\s+-\s+(.+)", pair) if pair else None
        if not names:
            continue
        nums = [c for c in cells[cells.index(pair) + 1:] if re.fullmatch(r"\d+", c)]
        out.append(dict(gid=gid, home=names.group(1).strip(), away=names.group(2).strip(), score=f"{nums[0]}-{nums[1]}" if len(nums) >= 2 else None))
    _store(key, out)
    return out


AGES = [(r"junior", "U20"), (r"star\w* dorost", "U18"), (r"mlad\w* dorost", "U16"), (r"9\. t", "U15"), (r"8\. t", "U14"),
        (r"7\. t", "U13"), (r"6\. t", "U12"), (r"5\. t", "U11")]


def czech_age(name: str) -> str:
    """The site shows club names without the age group; the competition gives it
    ("Extraliga staršího dorostu" is U18)."""
    from ..lookup import strip_accents
    n = strip_accents(name).lower()
    for pat, age in AGES:
        if re.search(pat, n):
            return f"{name} ({age})"
    return name


class CeskyHokej(Adapter):
    name = "ceskyhokej"
    site = "ceskyhokej.cz"
    ambiguous_dates = True

    def applies(self, parsed, tm, kb):
        c = parsed["comp"].lower()
        if any(k in c for k in ("extraliga", "dorostu", "trid", "junior", "czech", "cesk", "chance liga", "maxa liga", "tipsport")):
            return True
        for key in ("t1n", "t2n"):
            b = tm[key]["best"] or (tm[key]["candidates"][0] if tm[key]["candidates"] else None)
            if b and (str(b.get("country") or "").lower() in ("czech republic", "czechia") or "ceskyhokej" in (b.get("site") or "") + (b.get("schedule") or "") + (b.get("roster") or "") or "hokej.cz" in (b.get("site") or "") + (b.get("roster") or "")):
                return True
        return False

    async def find(self, parsed, tm, kb):
        import datetime
        out = []
        comps = competitions()
        if not comps:
            return out
        d0 = datetime.date.fromisoformat(parsed["date"])
        dates = [(d0 + datetime.timedelta(days=k)).isoformat() for k in (0, -1, 1)]   # header dates can be a day off

        async def one_comp(cid):
            # one session per competition: the widget keeps its state in the session
            async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=20, follow_redirects=True) as client:
                return [(dt, await games_for(cid, dt, client)) for dt in dates]

        results = await asyncio.gather(*[one_comp(cid) for cid in comps], return_exceptions=True)
        for cid, res in zip(comps, results):
            if isinstance(res, Exception):
                continue
            for dt, games in res:
                for g in games:
                    lg = czech_age(comps[cid] or "")
                    cand = dict(source=self.name, site=self.site, league=lg, home=g["home"], away=g["away"], date=dt,
                                score=g["score"], status=None, kind="protocol", adapter=self.name, url=f"https://ceskyhokej.cz/game/detail/{g['gid']}")
                    score_candidate(cand, parsed, lg)
                    if min(cand["team_scores"]) >= 0.6:
                        out.append(cand)
        return out

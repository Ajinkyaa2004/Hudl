"""UK: Elite League (eliteleague.co.uk: league + Challenge Cup) and NIHL National
(nihlnational.com: league, cup, pre-season). Both run the same site platform: /schedule has
a season select (id_season), the full-season list (id_month=999) shows date, time, both
teams and the /game/<id>-<abc>-<abc> link."""
from __future__ import annotations
import asyncio, html, re
from ..fetch import get_text
from .base import Adapter, score_candidate, days_apart, local_to_utc

SITES = {
    "eliteleague.co.uk": dict(base="https://www.eliteleague.co.uk", league="Elite Ice Hockey League",
                              words=("elite league", "elite ice hockey", "eihl", "challenge cup")),
    "nihlnational.com": dict(base="https://www.nihlnational.com", league="NIHL National",
                             words=("national ice hockey league", "nihl")),
}
SEASON_RE = re.compile(r'value="/schedule\?id_season=(\d+)"[^>]*>\s*([^<]+?)\s*<')
ANCHOR_RE = re.compile(r'<a[^>]*href="/game/(\d+)-([a-z]+)-([a-z]+)"[^>]*>([^<]*)</a>')
DATE_RE = re.compile(r'datetime="(\d{4}-\d{2}-\d{2})|\b(\d{2})\.(\d{2})\.(\d{4})\b')

UK_TEAMS = ("belfast giants", "cardiff devils", "coventry blaze", "dundee stars", "fife flyers", "glasgow clan", "guildford flames",
            "manchester storm", "nottingham panthers", "sheffield steelers", "basingstoke bison", "bristol pitbulls", "hull seahawks",
            "leeds knights", "milton keynes lightning", "peterborough phantoms", "romford raiders", "sheffield steeldogs",
            "solway sharks", "swindon wildcats", "telford tigers", "hull jets")

NAMES = {"Solway IHC": "Solway Sharks"}


def _texts(fragment: str):
    return [t.strip() for t in html.unescape(re.sub(r"<[^>]+>", "\x00", fragment)).split("\x00") if t.strip()]


def parse(page: str):
    """-> list of dict(gid, slug, date, time, home, away, score) from a full schedule page."""
    dates = [(m.start(), m.group(1) or f"{m.group(4)}-{m.group(3)}-{m.group(2)}") for m in DATE_RE.finditer(page)]
    games, prev_end, seen = [], 0, set()
    anchors = [m for m in ANCHOR_RE.finditer(page) if re.fullmatch(r"\s*(?:\d+\s*:\s*\d+(?:\s*\w+)?|-\s*:\s*-)\s*", m.group(4))]
    for i, m in enumerate(anchors):
        gid = m.group(1)
        date = next((d for pos, d in reversed(dates) if pos < m.start()), None)
        before = _texts(page[max(prev_end, m.start() - 3000):m.start()])
        nxt = anchors[i + 1].start() if i + 1 < len(anchors) else len(page)
        after = _texts(page[m.end():min(nxt, m.end() + 3000)])
        prev_end = m.end()
        if gid in seen or not date or not before or not after:
            continue
        seen.add(gid)
        home, away = before[-1], after[0]
        tm = next((t for t in reversed(before) if re.fullmatch(r"\d{1,2}:\d{2}", t)), None)
        sc = re.match(r"\s*(\d+)\s*:\s*(\d+)", m.group(4))
        games.append(dict(gid=gid, slug=f"{gid}-{m.group(2)}-{m.group(3)}", date=date, time=tm, home=home, away=away,
                          score=f"{sc.group(1)}-{sc.group(2)}" if sc else None))
    return games


class UkHockey(Adapter):
    name = "ukhockey"
    site = "eliteleague.co.uk / nihlnational.com"

    def _sites(self, parsed):
        from ..lookup import is_friendly
        c = parsed["comp"].lower()
        out = [s for s, cfg in SITES.items() if any(w in c for w in cfg["words"])]
        if not out and (is_friendly(parsed["comp"]) or "cup" in c):
            names = (parsed["t1"] + " | " + parsed["t2"]).lower()
            if any(t in names for t in UK_TEAMS):
                out = list(SITES)
        return out

    def applies(self, parsed, tm, kb):
        return bool(self._sites(parsed))

    async def _season_pages(self, site, season_label):
        cfg = SITES[site]
        idx = await get_text(cfg["base"] + "/schedule", ttl=24 * 3600)
        seasons = [(sid, name) for sid, name in SEASON_RE.findall(idx or "") if season_label in name]
        pages = await asyncio.gather(*[get_text(f"{cfg['base']}/schedule?id_season={sid}&id_team=0&id_month=999", ttl=1800) for sid, _ in seasons])
        return [(name, p) for (_, name), p in zip(seasons, pages) if p]

    async def find(self, parsed, tm, kb):
        y = int(parsed["date"][:4]) if parsed["date"][5:7] >= "07" else int(parsed["date"][:4]) - 1
        label = f"{y}/{y + 1}"
        sites = self._sites(parsed)
        per_site = await asyncio.gather(*[self._season_pages(s, label) for s in sites])
        out, seen = [], set()
        for site, pages in zip(sites, per_site):
            cfg = SITES[site]
            for season_name, page in pages:
                for g in parse(page):
                    url = f"{cfg['base']}/game/{g['slug']}"
                    if url in seen or days_apart(parsed["date"], g["date"]) not in (0, 1):
                        continue
                    seen.add(url)
                    cand = dict(source=self.name, site=site, league=season_name, home=NAMES.get(g["home"], g["home"]),
                                away=NAMES.get(g["away"], g["away"]), date=g["date"], score=g["score"], status=None,
                                kind="protocol", adapter=self.name, url=url,
                                start_utc=local_to_utc(g["date"], g["time"], "Europe/London"))
                    score_candidate(cand, parsed, "")
                    if min(cand["team_scores"]) >= 0.6:
                        out.append(cand)
        return out

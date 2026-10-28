"""Learn from a new Progress sheet export (data/progress.xlsx): every game an analyst already found
teaches PuckTrace, so the same teams and competitions resolve directly next time.

    .venv/bin/python -m backend.learn_sheet            # writes data/aliases_learned.json and data/knowledge.json
    .venv/bin/python -m backend.learn_sheet --dry-run  # only prints what it would learn

Two things are learned:
- spellings: when the analyst's link is a game in the local index on the header's date (a day either
  side) and one team clearly matches, the other team's site name becomes a spelling of the header
  name ("Pines Golden Wolves" = "South Florida Golden Wolves"). Rows where neither team matches, or
  where the ages disagree, teach nothing: some sheet rows carry another game's link.
- sources: which sites each competition and team uses (the hints the Find screen shows first).
"""
from __future__ import annotations
import argparse, datetime, json
from collections import Counter, defaultdict

import openpyxl

from . import gameindex, lookup
from .lookup import parse_header, first_url, domain, tokens
from .replay import canonical, PROGRESS
from .learning import ALIAS_FILE
from .seed import SKIP

ANCHOR = 0.8          # the team that vouches for the row
LEARN_BELOW = 0.8     # the other team is learned only when it doesn't already match this well
UNLIKE = 0.5          # names less than half alike need two rows that agree
BOTH_CLOSE = 0.6      # with no anchor, both teams at least this alike (and the date right) also vouch for the row
import re as _re
GIRLS = _re.compile(r"\b(?:lady|ladies|girls?|women'?s?|womens|female|fem|dam|damer|naiset|frauen|zeny)\b", _re.I)


def sheet_rows():
    from . import sheet
    for r in sheet.rows(PROGRESS):
        p = parse_header(r["header"].strip())
        if p:
            yield p, first_url(r.get("link"))


def index_by_game():
    """canonical game identity -> (home, away, date) for every game in the local index."""
    out = {}
    with gameindex._lock:
        rows = gameindex.conn().execute("select url, home, away, date from games").fetchall()
    for url, home, away, date in rows:
        c = canonical(url)
        if c:
            out[c] = (home, away, date)
            if len(c) == 3 and c[0] == "hockeytech":
                out.setdefault(("hockeytech-id", c[2]), []).append((home, away, date))
    return out


def learn_aliases(rows, games) -> tuple[dict, Counter]:
    from .adapters.base import team_similarity
    learned, why, weak = defaultdict(Counter), Counter(), {}
    for p, url in rows:
        c = canonical(url) if url else None
        g = games.get(c) if c else None
        if not g and c and len(c) == 3 and c[1] == "?":
            # a HockeyTech link that doesn't say its league: the game id decides when only one league has it
            hits = games.get(("hockeytech-id", c[2])) or []
            g = hits[0] if len(hits) == 1 else None
        if not g:
            why["link not in the index"] += 1
            continue
        home, away, date = g
        try:
            gap = abs((datetime.date.fromisoformat(date) - datetime.date.fromisoformat(p["date"])).days)
        except Exception:
            gap = 9
        if gap > 1:
            why["date differs"] += 1
            continue
        s = lambda a, b: team_similarity(a, b)[0]
        straight = [(p["t1"], home), (p["t2"], away)]
        crossed = [(p["t1"], away), (p["t2"], home)]
        pairs = crossed if s(*crossed[0]) + s(*crossed[1]) > s(*straight[0]) + s(*straight[1]) else straight
        scores = [s(*pr) for pr in pairs]
        # the analyst's link is this game when one team clearly matches, or when both are close
        if max(scores) < ANCHOR and min(scores) < BOTH_CLOSE:
            why["neither team matches"] += 1
            continue
        if min(scores) >= LEARN_BELOW:
            why["both already match"] += 1
            continue
        for (hdr, site), sc in zip(pairs, scores):
            if sc >= LEARN_BELOW:
                continue
            ha, sa = tokens(hdr)[1], tokens(site)[1]
            if ha and sa and not (ha & sa):
                why["ages disagree"] += 1
                continue
            hb, sb = tokens(hdr)[0], tokens(site)[0]
            if not hb or not sb or hb == sb:
                why["nothing to learn"] += 1
                continue
            if bool(GIRLS.search(hdr)) != bool(GIRLS.search(site)):
                why["girls' and boys' names"] += 1
                continue
            learned[site][hdr] += 1
            weak[(site, hdr)] = min(weak.get((site, hdr), 1.0), sc)
            why["learned"] += 1
    # names that look nothing alike are kept only when two rows agree
    for (site, hdr), sc in weak.items():
        if sc < UNLIKE and learned[site][hdr] < 2:
            del learned[site][hdr]
            why["learned"] -= 1
            why["unlike names, one row only"] += 1
    learned = {k: v for k, v in learned.items() if v}
    return learned, why


def _alias_data() -> dict:
    try:
        return json.loads(ALIAS_FILE.read_text())
    except Exception:
        return {}


def new_pairs(learned: dict, data: dict | None = None) -> list[tuple[str, str]]:
    """The (site spelling, header spelling) pairs write_aliases would add: not known yet and not on
    the '_never' list (spellings found wrong by hand: different teams)."""
    from .adapters.base import tokens as _t
    data = _alias_data() if data is None else data
    have = {(_t(k)[0], _t(v)[0]) for k, vs in data.items() if not k.startswith("_") for v in vs}
    have |= {(_t(a)[0], _t(b)[0]) for a, b in data.get("_never", [])}
    out = []
    for site, hdrs in learned.items():
        for hdr in hdrs:
            key = (tokens(site)[0], tokens(hdr)[0])
            if key not in have:
                out.append((site, hdr))
                have.add(key)
    return out


def write_aliases(learned: dict, block=()) -> int:
    """Merge into data/aliases_learned.json, keyed by the site's spelling as saves do. Pairs in
    `block` (an admin unticked them) go on the '_never' list instead and are never learned."""
    data = _alias_data()
    never = data.setdefault("_never", [])
    for a, b in block:
        if [a, b] not in never:
            never.append([a, b])
    pairs = new_pairs(learned, data)
    for site, hdr in pairs:
        data.setdefault(site, []).append(hdr)
    ALIAS_FILE.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    return len(pairs)


def learn_sources(rows) -> tuple[dict, dict]:
    comp, team = defaultdict(Counter), defaultdict(Counter)
    for p, url in rows:
        d = domain(url) if url else None
        if d and d not in SKIP:
            comp[p["compn"]][d] += 1
            team[p["t1n"]][d] += 1
            team[p["t2n"]][d] += 1
    return comp, team


def merge_sources(kb: dict, comp: dict, team: dict) -> tuple[int, int]:
    """The sheet's counts replace the seeded ones (the sheet is the whole season so far); sites
    the knowledge base has that the sheet doesn't (added by hand) are kept."""
    nc = nt = 0
    for key, cnt, top, field in ((comp, None, 5, "comp_sources"), (team, None, 3, "team_sources")):
        table = kb.setdefault(field, {})
        for k, c in key.items():
            old = table.get(k) or {}
            merged = Counter(old)
            for d, n in c.items():
                merged[d] = max(merged[d], n)
            new = dict(merged.most_common(top))
            if new != old:
                table[k] = new
                if field == "comp_sources":
                    nc += 1
                else:
                    nt += 1
    return nc, nt


def plan(log=print) -> dict:
    """Read data/progress.xlsx and work out what it would teach, without writing anything."""
    rows = list(sheet_rows())
    log(f"{len(rows)} sheet rows")
    games = index_by_game()
    log(f"{len(games)} games in the index")
    learned, why = learn_aliases(rows, games)
    log("spellings: " + ", ".join(f"{k} {n}" for k, n in why.most_common()))
    pairs = new_pairs(learned)
    for site, hdr in pairs:
        log(f"   {site}  =  {hdr}")
    return dict(rows=rows, learned=learned, why=dict(why), pairs=pairs)


def apply(pl: dict, block=(), log=print) -> dict:
    """Learn what plan() found, except the spelling pairs in `block`: spellings, competition and team
    websites, TeamLinkt sites and hokej.cz pairs. Writes the spellings file and the knowledge base."""
    rows, learned = pl["rows"], pl["learned"]
    comp, team = learn_sources(rows)
    kb = lookup.load_kb()
    nc, nt = merge_sources(kb, comp, team)
    log(f"sources: {nc} competitions and {nt} teams updated")
    # TeamLinkt sites the analysts used: learned (or refreshed) so their games are read from now on
    import re as _r
    from .adapters.teamlinkt import discover
    seen = {}
    for p, url in rows:
        m = _r.search(r"teamlinkt\.com/[Ll]eagues/event/(\d+)/\d+", url or "")
        if m and p["date"] >= (datetime.date.today() - datetime.timedelta(days=60)).isoformat():
            seen[m.group(1)] = url
    for url in seen.values():
        try:
            discover(url, log=log)
        except Exception as e:
            log(f"teamlinkt: {e}")
    from .hokejcz import anchors_from_sheet
    have = kb.setdefault("hokejcz_anchors", {})
    for cid, pairs in anchors_from_sheet(rows, log=log).items():
        have[cid] = sorted({tuple(x) for x in have.get(cid, [])} | {tuple(x) for x in pairs})
        have[cid] = [list(x) for x in have[cid]]
    n = write_aliases(learned, block)
    log(f"spellings added: {n}" + (f", {len(block)} blocked" if block else ""))
    kb["sheet_learned"] = datetime.datetime.now().isoformat(timespec="seconds")
    lookup.save_kb(kb)
    log("knowledge base saved")
    return dict(spellings=n, blocked=len(block), competitions=nc, teams=nt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    pl = plan()
    if a.dry_run:
        return
    apply(pl)


if __name__ == "__main__":
    main()

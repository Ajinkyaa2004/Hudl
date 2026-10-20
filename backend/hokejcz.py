"""Czech games: answer with hokej.cz, never with ceskyhokej.cz.

hokej.cz only opens from Czechia and nearby (a VPN elsewhere), so the servers can't read it. The
games are still found on ceskyhokej.cz (the same federation data, open everywhere); its game id is
then turned into the hokej.cz match id. The two numberings differ by a gap that is the same for
long runs of games in one competition, so the gap is taken from games whose hokej.cz link an
analyst used (the RTT sheet; kb["hokejcz_anchors"], kept by learn_sheet):
- the known games on both sides have the same gap: the hokej.cz link is the answer (32 of 32 right
  in a leave-one-out test);
- otherwise the worked-out link is offered to check, never chosen on its own;
- no known game in that competition: the game is shown with the competition's hokej.cz schedule.
"""
from __future__ import annotations
import re

from .lookup import strip_accents

ZAPAS = "https://www.hokej.cz/zapas/{id}"
# hokej.cz schedule pages, by ceskyhokej.cz competition id (only the ones known to exist)
SCHEDULE = {"14": "https://www.hokej.cz/tipsport-extraliga/zapasy", "2": "https://www.hokej.cz/maxa-liga/zapasy"}
HOME = "https://www.hokej.cz/"
SURE_SPAN = 60
VPN_NOTE = "hokej.cz opens only with a VPN (Czechia)"


def _comp_id(c: dict) -> str | None:
    if c.get("comp_id"):
        return str(c["comp_id"])
    from .adapters.ceskyhokej import competitions
    league = strip_accents(c.get("league") or "").lower()
    for cid, name in competitions().items():
        n = strip_accents(name or "").lower()
        if n and league.startswith(n):
            return cid
    return None


def links_for(ch_id: int, cid: str | None, kb: dict) -> tuple[list[str], bool]:
    """(hokej.cz match links, sure) for a ceskyhokej.cz game id: one sure link, up to two possible
    ones (the gaps of the known games just before and after), or none."""
    anchors = sorted(tuple(a) for a in ((kb or {}).get("hokejcz_anchors") or {}).get(str(cid), []))
    if not anchors:
        return [], False
    for ch, hk in anchors:
        if ch == ch_id:
            return [ZAPAS.format(id=hk)], True
    lo = [a for a in anchors if a[0] < ch_id]
    hi = [a for a in anchors if a[0] > ch_id]
    gaps = list(dict.fromkeys([a[1] - a[0] for a in (lo[-1:] + hi[:1])]))
    # nearer known game first
    gaps.sort(key=lambda g: min(abs(a[0] - ch_id) for a in anchors if a[1] - a[0] == g))
    # sure only between two close known games with the same gap (tested up to 36 game numbers apart,
    # all right; the numbering changes its gap now and then, so far-apart known games prove less)
    sure = bool(lo and hi and len(gaps) == 1 and hi[0][0] - lo[-1][0] <= SURE_SPAN)
    return [ZAPAS.format(id=ch_id + g) for g in gaps], sure


def _convert(c: dict, kb: dict) -> tuple[list[dict], bool]:
    """A candidate -> its hokej.cz version(s), and whether the link is sure."""
    import copy
    m = re.search(r"ceskyhokej\.cz/game/detail/(\d+)", c.get("url") or "")
    if not m:
        return [c], True
    cid = _comp_id(c)
    urls, sure = links_for(int(m.group(1)), cid, kb)
    base = dict(c, vpn="hokej.cz", site="hokej.cz", ch_id=m.group(1), reasons=list(c.get("reasons") or []))
    if not urls:
        base.update(url=SCHEDULE.get(cid or "", HOME), kind="schedule")
        base["reasons"].append(f"{VPN_NOTE}; this competition has no known hokej.cz game yet, so find the game on its schedule (teams, date and score above)")
        return [base], False
    out = []
    for k, u in enumerate(urls):
        x = copy.deepcopy(base)
        x["url"] = u
        x["hk_sure"] = sure
        if sure:
            x["reasons"].append(f"{VPN_NOTE}; link worked out from the hokej.cz games analysts opened just before and after this one")
        else:
            x["reasons"].append(f"{VPN_NOTE}; link worked out from nearby games, " + (f"option {k + 1} of {len(urls)}: " if len(urls) > 1 else "")
                                + "open it and check the teams before you use it")
            if k:
                x["confidence"] = max(0.0, (x.get("confidence") or 0) - 0.01)
        out.append(x)
    return out, sure


def apply(auto: dict, kb: dict) -> dict:
    """ceskyhokej.cz links in a search answer -> hokej.cz. A best answer whose hokej.cz link is not
    sure goes back to the shortlist, its options first."""
    if not auto:
        return auto
    best = auto.get("best")
    cands = []
    for c in auto.get("candidates") or []:
        if best is not None and (c is best or c.get("url") == best.get("url")):
            continue
        cands += _convert(c, kb)[0]
    if best:
        conv, sure = _convert(best, kb)
        if sure:
            auto["best"] = conv[0]
            cands = conv + cands
        else:
            auto["verdict"] = "check"
            auto["best"] = None
            cands = conv + cands
        # the same Czech game found on another site (onlajny) and on the federation's list: hokej.cz
        # first; its sure link is the answer, otherwise the hokej.cz options go first to check
        if auto.get("best") and "hokej.cz" not in (auto["best"].get("url") or ""):
            b = auto["best"]
            twins = [c for c in cands if c.get("vpn") == "hokej.cz" and c.get("kind") != "schedule"
                     and c.get("date") == b.get("date") and min(c.get("team_scores") or [0]) >= 0.8]
            if twins:
                tw_sure = [c for c in twins if c.get("hk_sure")]
                auto["best"] = tw_sure[0] if len({c["url"] for c in tw_sure}) == 1 else None
                if not auto["best"]:
                    auto["verdict"] = "check"
                cands = twins + [c for c in cands if c not in twins]
    auto["candidates"] = cands
    return auto


def learn_pick(kb: dict, url: str, ch_id: str, comp_id: str) -> bool:
    """An analyst picked a hokej.cz option: that game is a known pair from now on (saved with the
    knowledge base by the caller). A ceskyhokej game already known with another link is left alone."""
    m = re.search(r"(?<![a-z])hokej\.cz/zapas/(\d+)", url or "")
    if not (m and str(ch_id).isdigit() and str(comp_id).isdigit()):
        return False
    pair = [int(ch_id), int(m.group(1))]
    have = kb.setdefault("hokejcz_anchors", {}).setdefault(str(comp_id), [])
    if any(a[0] == pair[0] for a in have):
        return False
    have.append(pair)
    have.sort()
    return True


def anchors_from_sheet(rows, log=print) -> dict:
    """{competition id: [[ceskyhokej id, hokej.cz id], ...]} from RTT sheet rows whose link is a
    hokej.cz match: each row's game is looked up on ceskyhokej.cz by date and teams."""
    import asyncio, httpx
    from .adapters import ceskyhokej as C
    from .adapters.base import team_similarity
    from .fetch import UA
    todo = [(p, int(m.group(1))) for p, url in rows
            if url and (m := re.search(r"(?:^|//|www\.)hokej\.cz/zapas/(\d+)", url))]

    async def run():
        out = {}
        async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=20, follow_redirects=True) as cl:
            for p, hk in todo:
                best = None
                for cid in C.competitions():
                    for g in await C.games_for(cid, p["date"], cl):
                        s = team_similarity(p["t1"], g["home"])[0] + team_similarity(p["t2"], g["away"])[0]
                        if s > 1.4 and (not best or s > best[0]):
                            best = (s, int(g["gid"]), cid)
                if best:
                    out.setdefault(best[2], set()).add((best[1], hk))
        return out

    found = asyncio.run(run())
    log(f"hokej.cz: {sum(len(v) for v in found.values())} of {len(todo)} sheet links matched to ceskyhokej.cz games")
    return {cid: sorted([list(x) for x in v]) for cid, v in found.items()}

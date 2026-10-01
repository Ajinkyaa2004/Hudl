"""Last resort: a web search for games no source covers. Pages that name both teams are shown
as possible games to check by hand; a web result is never a Found."""
from __future__ import annotations
import asyncio, re
from urllib.parse import unquote
from ..fetch import get_text
from .base import Adapter

DDG = "https://html.duckduckgo.com/html/?q={q}"
SKIP = re.compile(r"wikipedia|facebook|instagram|twitter|x\.com|youtube|tiktok|eliteprospects\.com/player|lyrics|betting|odds|bet365", re.I)


def _names(team: str) -> list[str]:
    from ..lookup import strip_accents
    t = re.sub(r"\b(?:U|J)\s?\d{1,2}\b|\b\d{1,2}\s?U\b|\b(AAA|AA|HC|IF|IK|SK|HK)\b", " ", strip_accents(team), flags=re.I)
    words = [w for w in re.findall(r"[A-Za-z]{4,}", t)]
    return words[-2:] or words


class WebSearch(Adapter):
    name = "web search"
    site = "duckduckgo.com"
    budget_s = 20
    last_resort = True        # the runner calls it only when no other source found anything

    def applies(self, parsed, tm, kb):
        return False          # never in the normal round

    async def find(self, parsed, tm, kb):
        from urllib.parse import quote_plus
        q = f'"{parsed["t1"]}" "{parsed["t2"]}" {parsed["date"][:4]}'
        page = await get_text(DDG.format(q=quote_plus(q)), ttl=6 * 3600, timeout=15)
        urls = list(dict.fromkeys(unquote(u) for u in re.findall(r'uddg=([^&"]+)', page or "")))
        urls = [u for u in urls if not SKIP.search(u)][:4]
        pages = await asyncio.gather(*[get_text(u, ttl=6 * 3600, timeout=12) for u in urls])
        n1, n2 = _names(parsed["t1"]), _names(parsed["t2"])
        out = []
        from ..lookup import strip_accents
        for u, p in zip(urls, pages):
            t = strip_accents(re.sub(r"<[^>]+>", " ", p or "")).lower()
            if not (t and any(w.lower() in t for w in n1) and any(w.lower() in t for w in n2)):
                continue
            title = re.search(r"<title>(.*?)</title>", p or "", re.S)
            out.append(dict(source=self.name, site=re.sub(r"https?://(www\.)?([^/]+).*", r"\2", u), league=(title.group(1).strip()[:80] if title else ""),
                            home=parsed["t1"], away=parsed["t2"], date=parsed["date"], score=None, status=None, url=u, kind="web",
                            adapter=self.name, confidence=0.5, team_scores=[0.5, 0.5],
                            reasons=["found by a web search: the page names both teams; check it is the right game"]))
        return out

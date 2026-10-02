"""Roster pages on any website: team and club sites (SportsEngine, RAMP, Crossbar, LeagueApps,
HomeTeamsOnline, WordPress...), league sites and third-party pages.

extract(html) finds the players on a page, whatever its layout:
- tables: the columns are read from the header row (#, Name / First + Last, Pos, Birth year...),
  or guessed from the cells when there is no header; staff, schedule and standings tables are
  left out because their rows are not people;
- card or list layouts with no table: "#12 John Smith", "12 John Smith F", "John Smith #12" lines.
read(url) loads the page (plain request first, headless browser when the page is built by
script) and returns the players with the season shown on the page."""
from __future__ import annotations
import re
from .fetch import get_text, browser_page

try:
    from bs4 import BeautifulSoup
except Exception:          # pragma: no cover
    BeautifulSoup = None

TTL = 6 * 3600

NUM_H = re.compile(r"^(#|no\.?|num(ber)?|jersey|jersey\s*#|#\s*jersey|n°|nr\.?|č\.?|nro)$", re.I)
NAME_H = re.compile(r"^(name|player|players|player\s*name|athlete|full\s*name|skater|nom|jméno|spelare|namn|nimi|spieler)$", re.I)
FIRST_H = re.compile(r"^(first(\s*name)?|fname|given\s*name|prénom|förnamn)$", re.I)
LAST_H = re.compile(r"^(last(\s*name)?|lname|surname|family\s*name|nom\s*de\s*famille|efternamn)$", re.I)
POS_H = re.compile(r"^(pos\.?|position|post|pozice|spelposition)$", re.I)
BIRTH_H = re.compile(r"(birth|yob|born|dob|b\.?\s*year|year\s*of\s*birth|birth\s*year|naissance|narozen|född|syntynyt|jahrgang|^year$|^yr$)", re.I)
STAFF = re.compile(r"\b(coach|manager|trainer|director|president|assistant|head|staff|equipment|volunteer|registrar|treasurer|secretary|contact|coordinator|convenor|team\s*mom)\b", re.I)
NOT_PERSON = re.compile(r"\b(home|away|vs|total|totals|team|schedule|standings|record|goals|assists|points|game|games|season|league|division|tournament|roster|stats|statistics|login|register|news|contact|tickets|shop|sponsors?|results?|win|loss|tie|final|period|shots|saves|ppg|gwg|powered|privacy|policy|terms|copyright|cookies?|accessibility|sitemap|rights|reserved|menu|search|follow|subscribe|photos?|videos?|posts?|events?|programs?|resources|about|home|faq)\b", re.I)
SEASON_RE = re.compile(r"\b(20\d\d)\s*[-–/]\s*(?:20)?(\d\d)\b")
POS_WORD = re.compile(r"^(G|D|F|C|LW|RW|W|LD|RD|F/D|D/F|GK|Goalie|Goaltender|Defen[cs]e(man)?|Forward|Center|Centre|Wing|Left Wing|Right Wing|Skater)$", re.I)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def is_person(name: str) -> bool:
    n = _clean(name)
    if not (4 <= len(n) <= 45) or any(ch.isdigit() for ch in n) or ":" in n or STAFF.search(n) or NOT_PERSON.search(n):
        return False
    words = [w for w in re.split(r"[\s,]+", n) if w]
    if not (2 <= len(words) <= 5):
        return False
    letters = sum(c.isalpha() for c in n)
    return letters >= 0.75 * len(n.replace(" ", "")) and all(w[0].isalpha() and (w[0].isupper() or w.lower() in ("de", "van", "von", "der", "da", "di", "la", "le", "mc", "o'")) for w in words)


def _num(s: str) -> str | None:
    m = re.fullmatch(r"#?\s*(\d{1,3})", _clean(s))
    return m.group(1) if m else None


def _birth(s: str) -> int | None:
    m = re.search(r"\b((?:19[89]|20[0-2])\d)\b", s or "")
    return int(m.group(1)) if m else None


def _pos(s: str) -> str | None:
    s = _clean(s)
    if not s or not POS_WORD.match(s):
        return None
    s = s.lower()
    if s.startswith(("g", "goal")):
        return "G"
    if s.startswith(("d", "ld", "rd")) and not s.startswith("d/f"):
        return "D"
    return "F"


def _name_fix(name: str) -> str:
    n = _clean(re.sub(r"\(.*?\)|\[.*?\]|\b(?:captain|alternate|\(c\)|\(a\))\b", " ", name, flags=re.I))
    n = re.sub(r"\s*[-–]\s*$|^#\s*\d+\s*", "", n).strip(" ,*")
    if "," in n and n.count(",") == 1:          # "Smith, John" -> "John Smith"
        last, first = [x.strip() for x in n.split(",")]
        if first and last and " " not in first.strip():
            n = f"{first} {last}"
    return n


# ---------------------------------------------------------------- tables

def _cells(tr) -> list[str]:
    return [_clean(td.get_text(" ")) for td in tr.find_all(["td", "th"])]


def _table_players(table) -> list[dict]:
    rows = table.find_all("tr")
    if len(rows) < 4:
        return []
    head = None
    for tr in rows[:3]:
        cs = _cells(tr)
        if tr.find("th") or any(NAME_H.match(c) or FIRST_H.match(c) or NUM_H.match(c) for c in cs):
            head = cs
            break
    data = [_cells(tr) for tr in rows if _cells(tr) != head]
    data = [r for r in data if len(r) >= 2]
    if not data:
        return []
    width = max(len(r) for r in data)
    col = dict(num=None, name=None, first=None, last=None, pos=None, birth=None)
    if head:
        for i, h in enumerate(head):
            h2 = re.sub(r"[:*]", "", h).strip()
            if col["num"] is None and NUM_H.match(h2):
                col["num"] = i
            elif col["first"] is None and FIRST_H.match(h2):
                col["first"] = i
            elif col["last"] is None and LAST_H.match(h2):
                col["last"] = i
            elif col["name"] is None and NAME_H.match(h2):
                col["name"] = i
            elif col["pos"] is None and POS_H.match(h2):
                col["pos"] = i
            elif col["birth"] is None and BIRTH_H.search(h2) and not re.search(r"place|town|city", h2, re.I):
                col["birth"] = i
    # columns not named by a header are guessed from the cells
    def share(i, f):
        vals = [r[i] for r in data if i < len(r) and r[i]]
        return (sum(1 for v in vals if f(v)) / len(vals)) if vals else 0
    if col["name"] is None and col["first"] is None:
        best = max(range(width), key=lambda i: share(i, lambda v: is_person(_name_fix(v))), default=None)
        if best is not None and share(best, lambda v: is_person(_name_fix(v))) >= 0.6:
            col["name"] = best
    if col["num"] is None:
        best = max((i for i in range(width) if i not in (col["name"], col["first"], col["last"])), key=lambda i: share(i, _num), default=None)
        if best is not None and share(best, _num) >= 0.6:
            col["num"] = best
    if col["birth"] is None:
        best = max((i for i in range(width) if i not in (col["name"], col["num"])), key=lambda i: share(i, lambda v: bool(_birth(v))), default=None)
        if best is not None and share(best, lambda v: bool(_birth(v))) >= 0.6:
            col["birth"] = best
    if col["pos"] is None:
        best = max((i for i in range(width) if i not in (col["name"], col["num"], col["birth"])), key=lambda i: share(i, lambda v: bool(_pos(v))), default=None)
        if best is not None and share(best, lambda v: bool(_pos(v))) >= 0.6:
            col["pos"] = best
    if col["name"] is None and col["first"] is None:
        return []
    out = []
    for r in data:
        g = lambda k: r[col[k]] if col[k] is not None and col[k] < len(r) else ""
        name = _name_fix(f"{g('first')} {g('last')}" if col["first"] is not None else g("name"))
        if col["name"] is not None and not col["first"]:
            m = re.match(r"^#?\s*(\d{1,3})\s+(.*)$", name)           # "12 John Smith" in the name cell
            if m and not g("num"):
                name = m.group(2)
                r = list(r)
        if not is_person(name):
            continue
        num = _num(g("num")) if col["num"] is not None else None
        if col["num"] is None:
            m = re.match(r"^#?\s*(\d{1,3})\b", g("name"))
            num = m.group(1) if m else None
        out.append(dict(number=num, name=name, pos=_pos(g("pos")), birth_year=_birth(g("birth"))))
    # a staff table lists people too: keep it only when most rows carry a number or a position
    player_head = bool(head) and (col["num"] is not None or col["pos"] is not None) and (col["name"] is not None or col["first"] is not None)
    if out and not player_head and sum(1 for p in out if p["number"] or p["pos"] or p["birth_year"]) < 0.5 * len(out):
        return []
    return out


# ---------------------------------------------------------------- card and list layouts

LINE_NUM_FIRST = re.compile(r"^#?\s*(\d{1,3})\s*[|.\-–:]?\s+([A-ZÀ-Ý][^\d|#]{2,44}?)(?:\s*[|,\-–]\s*|\s+)?(G|D|F|C|LW|RW|W|GK|Goalie|Defen[cs]e|Forward)?\s*(?:[|,\-–]\s*)?((?:19|20)\d\d)?\s*$")
LINE_NAME_FIRST = re.compile(r"^([A-ZÀ-Ý][^\d|#]{2,44}?)\s*[|,\-–]?\s*#\s*(\d{1,3})\b")


def _text_players(text: str) -> list[dict]:
    lines = [_clean(l) for l in text.splitlines()]
    lines = [l for l in lines if l]
    out, seen = [], set()
    for i, l in enumerate(lines):
        m = LINE_NUM_FIRST.match(l)
        if m and is_person(_name_fix(m.group(2))):
            name = _name_fix(m.group(2))
            out.append(dict(number=m.group(1), name=name, pos=_pos(m.group(3) or ""), birth_year=int(m.group(4)) if m.group(4) else None))
            continue
        m = LINE_NAME_FIRST.match(l)
        if m and is_person(_name_fix(m.group(1))):
            out.append(dict(number=m.group(2), name=_name_fix(m.group(1)), pos=None, birth_year=_birth(l)))
            continue
        # card layouts: a number on its own line next to a name line
        if re.fullmatch(r"#?\d{1,3}", l) and i + 1 < len(lines) and is_person(_name_fix(lines[i + 1])):
            nxt = lines[i + 2] if i + 2 < len(lines) else ""
            out.append(dict(number=l.lstrip("#"), name=_name_fix(lines[i + 1]), pos=_pos(nxt), birth_year=_birth(nxt) if len(nxt) < 30 else None))
    res = []
    for p in out:
        k = p["name"].lower()
        if k not in seen:
            seen.add(k)
            res.append(p)
    return res


LABEL_NUM = re.compile(r"^(?:jersey|number|no\.?|num|#)\s*[:#.]?\s*#?(\d{1,3})$", re.I)
LABEL_POS = re.compile(r"^(?:position|pos)\s*[:.]?\s*(.+)$", re.I)
LABEL_BIRTH = re.compile(r"^(?:birth\s*year|year\s*of\s*birth|yob|born|birth\s*date|dob|d\.o\.b\.?)\s*[:.]?\s*.*?((?:19|20)\d\d)", re.I)
WORD = re.compile(r"^[A-ZÀ-Ý][A-Za-zÀ-ÿ'’.\-]{1,24}$")
WORD2 = re.compile(r"^[A-ZÀ-Ý][A-Za-zÀ-ÿ'’.\-]{1,24}(?:\s+(?:Jr\.?|Sr\.?|II|III|IV))?$")


def _card_players(lines: list[str]) -> list[dict]:
    """Card layouts that spread one player over several lines, e.g. SportsEngine
    ('47' / 'London' / 'Boos' / 'F') or WordPress team pages ('Ella Graham' / 'Jersey: 79' /
    'Position: Forward'). A bare number belongs to the name after it; a labelled one ('Jersey: 79')
    to the name before it."""
    out, i, n = [], 0, len(lines)
    used = set()
    bare = lambda x: re.fullmatch(r"#?\d{1,3}", x)
    while i < n:
        l = lines[i]
        name, span = None, 0
        if is_person(_name_fix(l)) and not LABEL_POS.match(l):
            name, span = _name_fix(l), 1
        elif i + 1 < n and WORD.match(l) and WORD2.match(lines[i + 1]) and is_person(f"{l} {lines[i + 1]}"):
            name, span = f"{l} {lines[i + 1]}", 2
        if not name:
            i += 1
            continue
        num = pos = birth = None
        took = []
        if i - 1 >= 0 and (i - 1) not in used and bare(lines[i - 1]):
            num = lines[i - 1].lstrip("#")
            took.append(i - 1)
        j = i + span
        while j < min(n, i + span + 4):
            x = lines[j]
            if is_person(_name_fix(x)) or (WORD.match(x) and j + 1 < n and WORD2.match(lines[j + 1]) and is_person(f"{x} {lines[j + 1]}")):
                break                                          # the next player starts here
            if bare(x) and (num or (j + 1 < n and (WORD.match(lines[j + 1]) or is_person(_name_fix(lines[j + 1]))))):
                break                                          # a number that opens the next player's card
            m = LABEL_NUM.match(x)
            if m and not num:
                num = m.group(1)
            elif bare(x) and not num:
                num = x.lstrip("#")
            elif LABEL_POS.match(x) and not pos:
                pos = _pos(LABEL_POS.match(x).group(1)) or "F"
            elif _pos(x) and not pos:
                pos = _pos(x)
            elif LABEL_BIRTH.match(x) and not birth:
                birth = int(LABEL_BIRTH.match(x).group(1))
            elif re.fullmatch(r"(?:19|20)\d\d", x) and not birth:
                birth = int(x)
            took.append(j)
            j += 1
        if num or pos or birth:
            out.append(dict(number=num, name=name, pos=pos, birth_year=birth))
            used.update(took)
            i = j
        else:
            i += span
    return out


def _name_list(lines: list[str]) -> list[dict]:
    """A plain list of names right under a 'Roster' heading, first and last names on their own
    lines (SportsEngine for young teams: 'Roster' / 'Brandon' / 'Bauer' / 'Tyson' / 'Boyer' ...)."""
    best = []
    for i, l in enumerate(lines):
        if l.lower() not in ("roster", "players", "team roster"):
            continue
        out, j = [], i + 1
        while j < len(lines):
            if is_person(_name_fix(lines[j])) and not (j + 1 < len(lines) and WORD.match(lines[j]) and " " not in lines[j]):
                out.append(dict(number=None, name=_name_fix(lines[j]), pos=None, birth_year=None))
                j += 1
            elif j + 1 < len(lines) and WORD.match(lines[j]) and WORD2.match(lines[j + 1]) and is_person(f"{lines[j]} {lines[j + 1]}"):
                out.append(dict(number=None, name=f"{lines[j]} {lines[j + 1]}", pos=None, birth_year=None))
                j += 2
            elif not out and j - i < 6:
                j += 1                                         # tabs or a season label before the list
            else:
                break
        if len(out) > len(best):
            best = out
    return best if len(best) >= 8 else []


def _page_text(soup) -> str:
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    for br in soup.find_all(["br"]):
        br.replace_with("\n")
    for blk in soup.find_all(["tr", "li", "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"]):
        blk.insert_after("\n")
    return soup.get_text("\n")


def extract(html: str) -> dict:
    """Players on a page: dict(players=[{number, name, pos, birth_year}], season, title, how)."""
    if not html or BeautifulSoup is None:
        return dict(players=[], season=None, title="", how="none")
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    title = _clean(soup.title.get_text()) if soup.title else ""
    heads = " | ".join(_clean(h.get_text(" ")) for h in soup.find_all(["h1", "h2", "h3"])[:8] if len(_clean(h.get_text(" "))) < 120)
    best = []
    for tb in soup.find_all("table"):
        if tb.find("table"):
            continue                                     # layout tables wrapping the real one
        ps = _table_players(tb)
        if len(ps) > len(best):
            best = ps
    how = "table"
    text = _page_text(soup)
    if len(best) < 8:
        tp = _text_players(text)
        if len(tp) > len(best):
            best, how = tp, "text"
    if len(best) < 8:
        cp = _card_players([l for l in (_clean(x) for x in text.splitlines()) if l])
        if len(cp) > len(best):
            best, how = cp, "cards"
    if len(best) < 8:
        nl = _name_list([l for l in (_clean(x) for x in text.splitlines()) if l])
        if len(nl) > len(best):
            best, how = nl, "names"
    # the site's own name ("Ohio Prospects") is not a player
    tw = {w.lower() for w in re.findall(r"[A-Za-zÀ-ÿ']+", title)}
    best = [p for p in best if not (tw and {w.lower() for w in p["name"].split()} <= tw)]
    # the season the roster is for: one named in the title or a roster/season heading, else the latest named near the top
    def seasons_in(t):
        return [int(a) for a, b in SEASON_RE.findall(t) if (int(b) - int(a[2:])) % 100 == 1]
    # menus link to other seasons ("2025/26 Roster"): read the season from the page body only
    try:
        body = BeautifulSoup(html, "lxml")
    except Exception:
        body = BeautifulSoup(html, "html.parser")
    for el in body.find_all(["nav", "header", "footer", "script", "style", "select"]):
        el.decompose()
    for el in body.find_all(True, attrs={"class": re.compile(r"menu|nav|dropdown|breadcrumb|footer|sidebar", re.I)}):
        el.decompose()
    for el in body.find_all(True, attrs={"id": re.compile(r"menu|nav|footer|sidebar", re.I)}):
        el.decompose()
    btext = _page_text(body)
    head_lines = "\n".join(l for l in btext[:8000].splitlines() if re.search(r"roster|season|team|saison|säsong|kausi", l, re.I))
    ys = seasons_in(title) or seasons_in(heads) or seasons_in(head_lines) or seasons_in(btext[:4000])
    season = f"{max(ys)}-{str(max(ys) + 1)[2:]}" if ys else None
    return dict(players=best, season=season, title=title, heads=heads, how=how if best else "none", text_head=_clean(text[:1500]))


HTML_JS = "() => document.documentElement.outerHTML"
# open "roster" tabs and expand lists on script-built pages before reading them
EXPAND_JS = r"""async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  for (const el of [...document.querySelectorAll('a,button,[role=tab]')]) {
    const t = (el.textContent || '').trim().toLowerCase();
    if (/^(roster|players|team roster|line ?up)$/.test(t) && !el.href?.startsWith('http')) { try { el.click(); await sleep(1200); } catch (e) {} break; }
  }
  window.scrollTo(0, document.body.scrollHeight); await sleep(600);
  return document.documentElement.outerHTML;
}"""


_challenged: dict = {}


async def _challenge(url: str) -> bool:
    """True when the site answers with a security check (Cloudflare 'Just a moment...'). Such pages
    are never worked around: the link is shown to open in Chrome."""
    import httpx
    from .fetch import UA
    host = url.split("/")[2] if "//" in url else url
    if host in _challenged:
        return _challenged[host]
    try:
        async with httpx.AsyncClient(headers={"User-Agent": UA}, follow_redirects=True, timeout=12) as c:
            r = await c.get(url)
        hit = r.status_code in (403, 503) and ("just a moment" in r.text[:3000].lower() or "cf-ray" in r.headers or "cloudflare" in r.headers.get("server", "").lower())
    except Exception:
        hit = False
    _challenged[host] = hit
    return hit


BROWSER_FIRST = re.compile(r"myhockeyrankings\.com", re.I)   # sites that challenge plain requests but not a browser


async def _paced_html(url: str) -> str | None:
    """One browser load at a time per site, 3 s apart (MyHockeyRankings challenges bursts)."""
    import asyncio, time
    from . import rosters as _r
    d = url.split("/")[2]
    gate = _r._gates.setdefault(d, asyncio.Lock())
    async with gate:
        wait = 3 - (time.time() - _r._last.get(d, 0))
        if wait > 0:
            await asyncio.sleep(wait)
        html = await browser_page(url, wait_ms=4000, ttl=TTL, js=HTML_JS, retries=1)
        _r._last[d] = time.time()
    return html


async def read(url: str, browser: bool | None = None) -> dict:
    """Load a roster page and extract its players. browser: None = only when the plain page has none."""
    raw = None
    if BROWSER_FIRST.search(url):
        html = await _paced_html(url)
        r = extract(html) if html else dict(players=[], season=None, title="", heads="", how="none")
        r["via"] = "browser"
        m = re.search(r"team-info/\d+/(20\d\d)/", url)               # the season is in the address
        if m:
            r["season"] = f"{m.group(1)}-{str(int(m.group(1)) + 1)[2:]}"
        if html is None:
            r["blocked"] = True
        return r
    if browser is not True:
        raw = await get_text(url, ttl=TTL, timeout=20)
        if raw is None and await _challenge(url):
            return dict(players=[], season=None, title="", heads="", how="none", via="page", blocked=True)
    r = extract(raw) if raw else dict(players=[], season=None, title="", how="none")
    if browser is False or len(r["players"]) >= 8:
        r["via"] = "page"
        return r
    html = await browser_page(url, wait_ms=4500, ttl=TTL, js=EXPAND_JS, retries=0)
    if raw is None and html is None:
        r["blocked"] = True             # a security check (Cloudflare) or a dead page: the link is given to open in Chrome
    if html:
        r2 = extract(html)
        if len(r2["players"]) > len(r["players"]):
            r2["via"] = "browser"
            return r2
    r["via"] = "page"
    return r


# ---------------------------------------------------------------- finding a team's roster page

def _age_variants(team_name: str, date: str) -> list[str]:
    """Ways a site may write the header's age group: '14U' -> 14U, U14, U-14, 14 & Under, 2012, 2013."""
    from .lookup import tokens
    import datetime as _dt
    d = _dt.date.fromisoformat(date)
    y = d.year if d.month >= 7 else d.year - 1
    out = []
    for a in tokens(team_name)[1]:
        if len(a) == 4:
            out += [a, f"{y - int(a)}U", f"U{y - int(a)}", f"{y + 1 - int(a)}U", f"U{y + 1 - int(a)}"]
        else:
            n = int(a)
            out += [f"{n}U", f"U{n}", f"U-{n}", f"{n} U", f"U {n}", f"{n}&U", f"{n} & Under", f"{n}O", str(y - n), str(y - n + 1)]
    return out


def identity(team_name: str, text: str, url: str, date: str, own_site: bool = False) -> tuple[float, str]:
    """How sure we are that a page is this team's roster, from its title, headings and address (not
    the menus, which name every team of the club). The club's words must be there unless the page is
    on the team's own site; the age group must be there when the header has one, and a page titled
    with another age group or level is ruled out. Returns (score 0..1, reason)."""
    from .roster_sources import _words
    from .lookup import strip_accents
    t = strip_accents(f"{text} {url}").lower()
    if not own_site:
        words = _words(re.sub(r"(?i)\b(u|j)?\d{1,2}u?\b|\b20[01]\d\b|\b(aaa|aa|a|tier\s*[12i]+)\b", " ", team_name))
        host = re.sub(r"[^a-z0-9]", "", url.lower().split("/")[2] if "//" in url else "")
        have = {w for w in words if re.search(rf"\b{re.escape(w)}", t) or w in host}
        if not words or len(have) < min(2, len(words)):
            return 0.0, f"club name not on the page ({', '.join(sorted(words - have))})"
    exact, near = _age_forms(team_name, date)
    hit = lambda forms: any(re.search(rf"(?<![\d]){re.escape(a.lower())}(?![\d])", t) for a in forms)
    if exact or near:
        if hit(exact):
            pass
        elif hit(near):
            return 0.6, "only a neighbouring birth year is shown"
        else:
            return 0.4, "age group not shown on the page"
    lv = {x.lower() for x in re.findall(r"\b(AAA|AA|A|BB|B|Tier\s*[12I]+)\b", team_name)}
    page_lv = {x.lower() for x in re.findall(r"\b(AAA|AA|BB|Tier\s*[12I]+)\b", text)}
    if lv and page_lv and not (lv & page_lv):
        return 0.5, f"level differs ({', '.join(sorted(page_lv))} on the page)"
    return 1.0, "team and age group on the page"


def _age_forms(team_name: str, date: str) -> tuple[list[str], list[str]]:
    """(exact forms, neighbouring birth year) of the header's age: '14U' in 2026-27 ->
    (['14U','U14','U-14',..., '2012'], ['2013'])."""
    from .lookup import tokens
    import datetime as _dt
    d = _dt.date.fromisoformat(date)
    y = d.year if d.month >= 7 else d.year - 1
    exact, near = [], []
    for a in tokens(team_name)[1]:
        if len(a) == 4:
            n = y - int(a)
            exact += [a]
            near += [f"{n}U", f"U{n}", f"{n + 1}U", f"U{n + 1}"]
        else:
            n = int(a)
            exact += [f"{n}U", f"U{n}", f"U-{n}", f"{n} U", f"U {n}", f"{n}&U", f"{n} & Under", f"{n}O", str(y - n)]
            near += [str(y - n + 1)]
    return exact, near


ROSTER_LINK = re.compile(r"roster|players|team[-_/ ]?page|/teams?/|/team/", re.I)
SKIP_LINK = re.compile(r"facebook|instagram|twitter|x\.com|youtube|tiktok|mailto:|tel:|\.pdf$|\.jpe?g$|\.png$|login|register|store|shop|donat|sponsor|calendar|news|schedule|standings|results|scores|stats|tryout|camp|tournament", re.I)
NOT_ROSTER_PAGE = re.compile(r"schedule|standings|results|scores|statistics|calendar|news|tryouts?", re.I)


def _links(html: str, base: str) -> list[tuple[str, str]]:
    from urllib.parse import urljoin
    out = []
    for m in re.finditer(r"<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", html or "", re.S | re.I):
        href, label = m.group(1), _clean(re.sub(r"<[^>]+>", " ", m.group(2)))
        u = urljoin(base, href)
        if u.startswith("http") and not SKIP_LINK.search(u):
            out.append((u, label))
    return list(dict.fromkeys(out))


def _same_site(a: str, b: str) -> bool:
    ha, hb = a.split("/")[2].lower().replace("www.", ""), b.split("/")[2].lower().replace("www.", "")
    return ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)


async def find_on_site(site: str, team_name: str, date: str, max_pages: int = 4) -> list[dict]:
    """Walk a club or league site for this team's roster: the links whose text or address carry the
    age group (and 'roster' where there is one), then each team page's roster tab."""
    if not site or not site.startswith("http"):
        return []
    home = await get_text(site, ttl=24 * 3600, timeout=20)
    if not home and await _challenge(site):
        return []
    if not home or len(home) < 3000:
        home = await browser_page(site, wait_ms=3500, ttl=24 * 3600, js=HTML_JS, retries=0) or home
    if not home:
        return []
    ages = [a.lower() for a in _age_variants(team_name, date)]
    def link_score(u, label):
        t = f"{label} {u}".lower()
        s = 0.0
        if ages and any(re.search(rf"(?<![\d]){re.escape(a)}(?![\d])", t) for a in ages):
            s += 2
        if re.search(r"roster|players", t):
            s += 1.5
        if re.search(r"/teams?/|team", t):
            s += 0.5
        return s
    cands = [(link_score(u, l), u, l) for u, l in _links(home, site) if _same_site(u, site)]
    cands = sorted([c for c in cands if c[0] >= (2 if ages else 1.5)], key=lambda x: -x[0])[:max_pages]
    out, seen = [], set()
    for _, u, label in cands:
        r = await read(u)
        page = u
        if len(r["players"]) < 8:
            # a team page: its roster is one link further ('Roster' tab)
            raw = await get_text(u, ttl=TTL, timeout=20) or ""
            sub = [x for x, l in _links(raw, u) if _same_site(x, u) and re.search(r"roster|players", f"{l} {x}", re.I)]
            for x in sub[:2]:
                r2 = await read(x)
                if len(r2["players"]) > len(r["players"]):
                    r, page = r2, x
        if len(r["players"]) >= 8 and page not in seen and not NOT_ROSTER_PAGE.search(r.get("title", "") + " " + page):
            seen.add(page)
            sc, why = identity(team_name, f"{label} | {r.get('title', '')} | {r.get('heads', '')}", page, date, own_site=True)
            out.append(dict(r, url=page, match=sc, why=why, by="site"))
    return out


DDG = "https://html.duckduckgo.com/html/?q={q}"
SEARCH_SKIP = re.compile(r"facebook|instagram|twitter|x\.com|youtube|tiktok|wikipedia|eliteprospects|myhockeyrankings|gamesheetstats|hockeytech|timetoscore|linkedin|reddit|news|article|blog", re.I)


async def find_by_search(team_name: str, date: str, max_pages: int = 4) -> list[dict]:
    """A web search for the team's roster page when no site is on file. Sources the tool already
    reads directly (EliteProspects, MyHockeyRankings, GameSheet...) and social media are left out."""
    from urllib.parse import quote_plus, unquote
    y = int(date[:4]) if int(date[5:7]) >= 7 else int(date[:4]) - 1
    q = f'{team_name} roster {y}-{str(y + 1)[2:]}'
    page = await get_text(DDG.format(q=quote_plus(q)), ttl=24 * 3600, timeout=15)
    urls = list(dict.fromkeys(unquote(u) for u in re.findall(r'uddg=([^&"]+)', page or "")))
    urls = [u for u in urls if u.startswith("http") and not SEARCH_SKIP.search(u)][:max_pages]
    out = []
    for u in urls:
        r = await read(u)
        if len(r["players"]) >= 8:
            sc, why = identity(team_name, f"{r.get('title', '')} | {r.get('heads', '')}", u, date)
            out.append(dict(r, url=u, match=sc, why=why, by="search"))
    return out

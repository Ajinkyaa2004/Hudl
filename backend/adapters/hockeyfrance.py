"""France (hockeyfrance.com, FFHG): the federation's WordPress site proxies the Hockeynet
competition API through admin-ajax.php (POST). get_rencontres(competition_id, date_min,
date_max) returns each game with date, both teams and its id; the report is
/competitions/rencontre/<id>/. Competition ids per season come from get_competition_by_type
(types 1..22), cached a day."""
from __future__ import annotations
import asyncio, json, re, time
import httpx
from .. import fetch
from .base import Adapter, score_candidate, days_apart, local_to_utc

AJAX = "https://www.hockeyfrance.com/competitions/wp-admin/admin-ajax.php"
REPORT = "https://www.hockeyfrance.com/competitions/rencontre/{id}/"
TYPES = range(1, 23)
_mem: dict = {}
# French club towns, to recognise friendlies whose teams have no country in Club Data
FR_TOWNS = ("grenoble", "amiens", "rouen", "angers", "gap", "briancon", "bordeaux", "cergy", "chamonix", "mont-blanc", "marseille",
            "nice", "anglet", "strasbourg", "villard", "caen", "dijon", "epinal", "mulhouse", "tours", "nantes", "brest", "lyon",
            "clermont", "montpellier", "valence", "roanne", "neuilly", "courbevoie", "francais volants", "reims", "morzine",
            "cholet", "wasquehal", "dunkerque", "hockey club 74", "annecy", "chambery", "megeve", "limoges", "toulouse", "poitiers",
            "compiegne", "meudon", "garges", "evry", "viry", "vaujany", "orleans", "rapaces", "gothiques", "boxers", "ducs d")
_sem = asyncio.Semaphore(4)


async def post(data: dict, ttl: int = 1800):
    """Cached POST to the site's admin-ajax proxy; returns parsed JSON or None."""
    key = AJAX + "?" + "&".join(f"{k}={v}" for k, v in sorted(data.items()))
    hit = _mem.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    if ttl >= 3600:
        d = fetch._disk_get(key, ttl)
        if d is not None:
            try:
                val = json.loads(d)
                _mem[key] = (time.time(), val)
                return val
            except Exception:
                pass
    val = None
    for attempt in range(2):
        try:
            async with _sem:
                async with httpx.AsyncClient(headers={"User-Agent": fetch.UA}, timeout=25) as c:
                    r = await c.post(AJAX, data=data)
            if r.status_code == 200:
                val = r.json()
                break
        except Exception:
            pass
        await asyncio.sleep(1.5)
    if val is None:
        return None
    _mem[key] = (time.time(), val)
    if ttl >= 3600:
        fetch._disk_put(key, json.dumps(val))
    return val


def _rows(val):
    d = (val or {}).get("data")
    if isinstance(d, dict) and isinstance(d.get("data"), list):
        return d["data"]
    return []


def season_label(date: str) -> str:
    """FFHG names a season by its end year: 2026-27 -> '2027'."""
    y = int(date[:4])
    return str(y + 1 if date[5:7] >= "07" else y)


def clean(name: str) -> str:
    """'72001 - ANGLET HORMADI U18 LIGUE ESPOIR' -> 'Anglet Hormadi U18'."""
    n = re.sub(r"^\s*[A-Z]?\d{3,6}[A-Z]?\s*-\s*(?:\d{3,6}\s+)?", "", name or "")
    n = re.sub(r"\b(ligue espoir|championnat de france|elite|excellence)\b", " ", n, flags=re.I)
    n = re.sub(r"\s+", " ", n).strip(" -")
    return n.title() if n.isupper() else n


def team_name(t: dict) -> str:
    """Registered name plus the club's display name: 'HCAS U18' + 'Gothiques d'Amiens'."""
    from ..lookup import norm
    n = clean(t.get("libelle") or "")
    full = (t.get("libelle_complet") or "").strip()
    if full and norm(full) not in norm(n):
        n = f"{n} {full.title() if full.isupper() else full}".strip()
    return n


def wanted(comp_name: str, parsed) -> bool:
    """Which of the season's competitions can hold the header's game."""
    from ..lookup import tokens, is_friendly
    c = comp_name.lower()
    ages = tokens(parsed["t1"])[1] | tokens(parsed["t2"])[1] | set(re.findall(r"\bu\s?(\d{2})", parsed["comp"].lower()))
    if "para" in c or "loisir" in c or "3x3" in c:
        return False
    fem = "fémin" in c or "femin" in c
    if fem != any(w in parsed["comp"].lower() for w in ("women", "fémin", "femin", "female")):
        return False
    # a header labelled friendly is sometimes a league game: friendlies read both kinds
    if "amicaux" in c and not is_friendly(parsed["comp"]):
        return False
    c_ages = set(re.findall(r"\bu(\d{2})", c))
    if ages:
        return bool(c_ages & {str(a) for a in ages})
    return not c_ages


class HockeyFrance(Adapter):
    name = "hockeyfrance"
    site = "hockeyfrance.com"
    budget_s = 45

    def applies(self, parsed, tm, kb):
        from ..lookup import is_friendly
        c = parsed["comp"].lower()
        if any(w in c for w in ("ffhg", "ligue magnus", "synerglace", "france", "coupe de france")):
            return True
        if is_friendly(parsed["comp"]):
            from ..lookup import strip_accents
            names = strip_accents(parsed["t1"] + " | " + parsed["t2"]).lower()
            if any(re.search(r"\b" + re.escape(t), names) for t in FR_TOWNS):
                return True
            for key in ("t1n", "t2n"):
                b = tm[key]["best"] or (tm[key]["candidates"][0] if tm[key]["candidates"] else None)
                if b and str(b.get("country") or "").lower() == "france":
                    return True
        return False

    async def competitions(self, season: str):
        vals = await asyncio.gather(*[post({"action": "get_competition_by_type", "id_type": str(t)}, ttl=24 * 3600) for t in TYPES])
        out = {}
        for v in vals:
            for c in _rows(v):
                if str(c.get("saison")) == season:
                    out[c["id"]] = c.get("libelle") or ""
        return out

    async def find(self, parsed, tm, kb):
        import datetime
        comps = await self.competitions(season_label(parsed["date"]))
        pick = {cid: name for cid, name in comps.items() if wanted(name, parsed)}
        d = datetime.date.fromisoformat(parsed["date"])
        lo, hi = (d - datetime.timedelta(days=1)).isoformat(), (d + datetime.timedelta(days=1)).isoformat()
        vals = await asyncio.gather(*[post({"action": "get_rencontres", "page": "", "equipe_id": "", "competition_id": str(cid), "phase_id": "",
                                            "date_min": lo, "date_max": hi, "par_page": "300", "journee": "", "limite": "0"}, ttl=1800)
                                      for cid in pick])
        out = []
        for cid, v in zip(pick, vals):
            for g in _rows(v):
                when = g.get("date_rencontre") or ((g.get("calendrier") or {}).get("date_debut") or "")
                date, hhmm = when[:10], when[11:16]
                if days_apart(parsed["date"], date) not in (0, 1):
                    continue
                rec, vis = g.get("receveur") or {}, g.get("visiteur") or {}
                home, away = team_name(rec), team_name(vis)
                sc = g.get("score") or []
                score = None
                if g.get("etat") == "T" and len(sc) == 2:
                    try:
                        score = "-".join(str(x.get("score") if isinstance(x, dict) else x) for x in sc)
                    except Exception:
                        score = None
                league = f"{pick[cid]} {((g.get('phase') or {}).get('libelle') or '')}".strip()
                cand = dict(source=self.name, site=self.site, league=league, home=home, away=away, date=date, score=score, status=g.get("etat"),
                            kind="protocol", adapter=self.name, url=REPORT.format(id=g["id"]),
                            start_utc=local_to_utc(date, hhmm, "Europe/Paris"))
                score_candidate(cand, parsed, pick[cid])
                if min(cand["team_scores"]) >= 0.6:
                    out.append(cand)
        return out

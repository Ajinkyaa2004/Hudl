"""Source health: one known game per source, searched the normal way. When a site changes its
pages or stops answering, its check fails here instead of the tool quietly missing games.

    .venv/bin/python -m backend.health        # also runs daily inside the server
Results in data/health.json, shown on the app's Status screen."""
from __future__ import annotations
import asyncio, json, time
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
OUT = DATA / "health.json"

# (source, header of a real 2026-27 game, a piece of the report URL the source must return)
CHECKS = [
    ("TimeToScore (AHF)", "Lehigh Valley Phantoms 16U AA 0 - 0 Jersey Colts 16U AA | AHF 16U | 2026-09-20T02:00:00", "game_id=78503"),
    ("TimeToScore (NGHL)", "San Jose Jr. Sharks 19U AAA Girls - Princeton Tiger Lillies 19U AAA | NGHL 19U (W) | 2026-08-28 19:25:00", "85833"),
    ("TimeToScore (USPHL)", "Mercer Chiefs 0 - 0 Rockets Hockey Club NCDC | Friendly match | 2026-09-16T20:30:00", "27005"),
    ("TimeToScore (CAHA)", "San Jose Jr. Sharks 13O AAA - Vegas Jr. Golden Knights 13U AAA | USA Hockey U13 | 2026-08-29 19:30:00", "58882"),
    ("GameSheet", "Connecticut Polar Bears 16U Prep 0 - 0 Valley Jr. Warriors 16U AAA (W) | USA Hockey 16U (G) | 2026-09-05T03:29:00", "2971456"),
    ("HockeyTech", "Lindsay Muskies 0 - 0 Trenton Golden Hawks | Friendly match | 2026-08-29T02:00:00", "16420"),
    ("HockeyTech (Alberta)", "Red Deer Rebels U15 AAA 0 - 0 Northern Alberta Xtreme U15 AAA | Friendly match | 2026-09-19T04:30:00", "game_id=28676"),
    ("AYHL", "North Jersey Avalanche 13U AAA 0 - 0 Philadelphia Jr. Flyers 13U AAA | AYHL 13U | 2026-09-12T21:30:00", "46056"),
    ("RAMP (OWHL)", "Cambridge Rivulettes - Central York Jr. Panthers U22 AA | Ontario Women's Hockey League U22 | 2026-09-04 20:00:00", "2024476"),
    ("swehockey", "MoDo Hockey 0 - 0 Skelleftea AIK | Friendly match | 2026-08-28T19:00:00", "1113753"),
    ("Finland (leijonat)", "TPS Juniorit U20 0 - 0 Kiekko-Espoo U20 | Friendly match | 2026-08-28T18:00:00", "2711055"),
    ("Norway", "Comet Halden 0 - 0 Gjovik Hockey | Friendly match | 2026-08-28T19:30:00", "8450936"),
    ("Denmark (Metal Ligaen)", "SonderjyskE 1 - 2 Odense Bulldogs | Metal Ligaen | 2026-08-28T20:00:00", "528"),
    ("Denmark (sportsadmin)", "Esbjerg IK 0 - 0 Odense IK | Denmark2 | 2026-09-01T19:15:00", "76386"),
    ("Switzerland", "HC Ambri-Piotta 0 - 0 Schwenninger Wild Wings | Friendly match | 2026-08-28T20:00:00", "20270009261217"),
    ("Germany (DEB)", "Krefelder EV 1981 U17 0 - 0 Iserlohner EC U17 | Friendly match | 2026-08-28T19:00:00", "3bce4593"),
    ("DEL2", "Bietigheim Steelers - EC Bad Nauheim | Friendly match | 2026-08-30 18:00:00", "8849"),
    ("ICE Hockey League", "EC-KAC 0 - 0 Vienna Capitals | Friendly match | 2026-08-28T19:15:00", "7960"),
    ("Alps Hockey League", "KHL Sisak 0 - 1 HDD Jesenice | Alps Hockey League | 2026-09-19T20:30:00", "dfa02595"),
    ("Czech", "HC Ocelari Trinec U18 0 - 0 Mountfield HK U18 | Extraliga starsiho dorostu | 2026-09-11T15:30:00", "181439"),
    ("Slovakia (onlajny)", "HC Slovan Bratislava - HC Sparta Praha | Friendly match | 2026-08-29 19:00:00", "539335"),
    ("France", "Grenoble U20 0 - 0 Amiens U20 | Friendly match | 2026-09-06T19:00:00", "83273"),
    ("Latvia", "HS Prizma Riga - HC Panter | Optibet hokeja liga | 2026-09-05 13:00:00", "35814"),
    ("KHL", "HC Salavat Yulaev Ufa 0 - 0 Amur Khabarovsk | Friendly match | 2026-08-28T13:00:00", "902835"),
    ("MHL", "HC Kuznetskie Medvedi 0 - 0 HC Mamonty Yugry | Molodyozhnaya Hokkeinaya Liga | 2026-09-14T14:30:00", "903059"),
    ("Belarus", "HK Vitebsk 0 - 0 HK Brest | Betera-Extraleague | 2026-09-12T13:00:00", "441538"),
    ("Kazakhstan", "HK Aktobe 0 - 2 Nomad Astana | Kazakhstan Hockey Championship | 2026-09-20T15:00:00", "2593"),
    ("EAK (tournamentsgurus)", "Woodbridge Wolfpack 16U AAA 0 - 0 Atlantic Coast Academy 16U National | EAK Tournament | 2026-09-05T23:40:00", "2025804"),
]


async def run(checks=CHECKS) -> dict:
    from . import lookup
    from .adapters import run_all
    kb = lookup.load_kb()
    sem = asyncio.Semaphore(4)

    async def one(name, header, want):
      async with sem:
        t = time.time()
        r = lookup.search(header, kb)
        ok, got, err, how = False, None, None, None
        try:
            # live sources only: the index would answer from memory even if the site were down
            from . import adapters as A
            p = r["parsed"]
            live = [a for a in A.ADAPTERS if A.country_gate(a, r["teams"]) and a.applies(p, r["teams"], kb)]
            res = await asyncio.gather(*[asyncio.wait_for(a.find(p, r["teams"], kb), 60) for a in live], return_exceptions=True)
            urls = [c["url"] for x in res if not isinstance(x, Exception) for c in x]
            ok = any(want in u for u in urls)
            got = next((u for u in urls if want in u), urls[0] if urls else None)
            errs = [f"{a.name}: {str(x)[:80]}" for a, x in zip(live, res) if isinstance(x, Exception)]
            err = "; ".join(errs) or (None if ok else "the known game was not returned")
            how = "live" if ok else None
            if not ok:
                # sources read in bulk each day (TimeToScore, GameSheet...) or tournaments already
                # over answer from the game list instead of a live page
                from . import gameindex
                with gameindex._lock:
                    hit = gameindex.conn().execute("select url, updated from games where url like ?", (f"%{want}%",)).fetchone()
                if hit:
                    ok, got, how, err = True, hit[0], "game list", None
        except Exception as e:
            err = str(e)[:200]
        return dict(source=name, ok=ok, how=how, seconds=round(time.time() - t, 1), got=got, error=None if ok else err)
    out = await asyncio.gather(*[one(*c) for c in checks])
    from . import gameindex
    res = dict(at=time.strftime("%Y-%m-%dT%H:%M:%S"), checks=out, index=gameindex.stats())
    try:
        res["harvest"] = json.loads((DATA / "harvest_status.json").read_text())
    except Exception:
        pass
    OUT.write_text(json.dumps(res, indent=1))
    return res


if __name__ == "__main__":
    r = asyncio.run(run())
    for c in r["checks"]:
        print(("OK  " if c["ok"] else "FAIL"), c["source"], c["seconds"], "s", c.get("how") or "", c["error"] or "")

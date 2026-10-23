"""Re-run missed games with today's PuckTrace and see what is found now.

    LIGHT_MODE=1 .venv/bin/python tests/tools/rerun_missed.py tests/logs/missed.json tests/logs/rerun.json

Input: the JSON written by analyse_log.py (header, link per game). Output: per game the verdict, whether the
answer is the analyst's game, and the top candidates with their reasons. Learning is switched off, so a test
never changes the real knowledge. Run from the project folder.
"""
import asyncio, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend import lookup, learning
from backend.lookup import domain
from backend.replay import canonical, same_game

learning.remembered = lambda p: None
learning.remember = lambda *a, **k: None
for n in dir(learning):
    if n.startswith("learn_") and callable(getattr(learning, n)):
        setattr(learning, n, lambda *a, **k: None)


async def main(src, dst):
    todo = json.loads(Path(src).read_text())
    kb = lookup.load_kb()
    out = {}
    sem = asyncio.Semaphore(5)

    async def one(k, v):
        async with sem:
            try:
                a = (await asyncio.wait_for(lookup.search_full(v["header"], kb, rosters=False), 240))["auto"]
            except Exception as e:
                out[k] = dict(v, error=str(e))
                print(k, "ERROR", e, flush=True)
                return
            cl = canonical(v["link"])
            cands = a.get("candidates") or []
            best = (a.get("best") or {}).get("url") or ""
            rank = next((i for i, c in enumerate(cands) if same_game(canonical(c.get("url") or ""), cl)), None)
            out[k] = dict(v, verdict=a.get("verdict"), best=best, ok=bool(best) and same_game(canonical(best), cl), rank=rank,
                          top=[dict(url=c.get("url"), home=c.get("home"), away=c.get("away"), date=c.get("date"), conf=c.get("confidence"),
                                    ts=c.get("team_scores"), reasons=c.get("reasons")) for c in cands[:3]])
            print(k, a.get("verdict"), out[k]["ok"], rank, domain(v["link"]), flush=True)

    await asyncio.gather(*[one(k, v) for k, v in todo.items()])
    Path(dst).write_text(json.dumps(out, indent=1, default=str))
    ok = sum(1 for v in out.values() if v.get("ok"))
    first = sum(1 for v in out.values() if v.get("verdict") == "check" and v.get("rank") == 0)
    print(f"DONE {len(out)} games: found as the analyst {ok}, Pick one with the right game first {first}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))

"""Local index of every game the tool has seen, from any source.

Harvesters fill it with whole seasons (TimeToScore, GameSheet, HockeyTech...), and every live
search writes the games it fetched. A header whose game is already here resolves in well under a
second without touching the source site, so the tool gets faster as the season goes on.

    games(url PK, source, site, gid, date, time, start_utc, home, away, league, status, score, updated)
"""
from __future__ import annotations
import datetime, re, sqlite3, threading, time
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "games.db"
_lock = threading.Lock()
_conn = None

COLS = ("url", "source", "site", "gid", "date", "time", "start_utc", "home", "away", "league", "status", "score")


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB, check_same_thread=False)
        _conn.execute("pragma journal_mode=wal")
        _conn.execute("""create table if not exists games(url text primary key, source text, site text, gid text, date text,
                         time text, start_utc text, home text, away text, league text, status text, score text, updated real)""")
        _conn.execute("create index if not exists games_date on games(date)")
        _conn.execute("create index if not exists games_source on games(source)")
        _conn.commit()
    return _conn


def add_games(games: list[dict], source: str | None = None) -> int:
    """Insert or refresh games. Each needs url, date, home, away; the rest is optional."""
    now = time.time()
    rows = []
    for g in games:
        if not (g.get("url") and g.get("date") and g.get("home") and g.get("away")):
            continue
        rows.append(tuple([g.get(c) if c != "source" else (source or g.get("source")) for c in COLS]) + (now,))
    if not rows:
        return 0
    with _lock:
        c = conn()
        c.executemany(f"insert or replace into games({','.join(COLS)},updated) values ({','.join('?' * (len(COLS) + 1))})", rows)
        c.commit()
    return len(rows)


def games_on(dates: list[str]) -> list[dict]:
    with _lock:
        cur = conn().execute(f"select {','.join(COLS)} from games where date in ({','.join('?' * len(dates))})", dates)
        return [dict(zip(COLS, r)) for r in cur.fetchall()]


def around(date: str, before: int = 1, after: int = 1) -> list[str]:
    d = datetime.date.fromisoformat(date)
    return [(d + datetime.timedelta(days=k)).isoformat() for k in range(-before, after + 1)]


def stats() -> dict:
    with _lock:
        c = conn()
        total = c.execute("select count(*) from games").fetchone()[0]
        by = dict(c.execute("select source, count(*) from games group by source order by 2 desc").fetchall())
        rng = c.execute("select min(date), max(date) from games where date >= '2026-08-01'").fetchone()
    return dict(total=total, by_source=by, first=rng[0], last=rng[1])


def delete_source(source: str, before_updated: float | None = None) -> int:
    with _lock:
        c = conn()
        if before_updated:
            n = c.execute("delete from games where source=? and updated<?", (source, before_updated)).rowcount
        else:
            n = c.execute("delete from games where source=?", (source,)).rowcount
        c.commit()
    return n


_WORD = re.compile(r"[a-z]{4,}")


def words(name: str) -> set:
    from .lookup import strip_accents
    return set(_WORD.findall(strip_accents(str(name or "")).lower())) - {"hockey", "club", "team", "junior", "elite", "academy", "white", "black", "blue", "gold"}

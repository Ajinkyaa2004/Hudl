"""Write the public part of the game list (the games table only: schedules and report links from
public league sites) to data/game-list.db.gz for the server to download. Learned answers and
saved headers (the results and sources tables) are never included.

    .venv/bin/python -m backend.export_games"""
from __future__ import annotations
import gzip, shutil, sqlite3
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"


def export(src: Path = DATA / "games.db", out: Path = DATA / "game-list.db.gz") -> int:
    # its own scratch file: the server unpacks its downloaded list into data/game-list.db, and an
    # export for the other server running at the same moment used to clash with it ("gl is locked")
    tmp = out.with_name(out.name.replace(".db.gz", "") + ".export-tmp.db")
    tmp.unlink(missing_ok=True)
    s = sqlite3.connect(src)
    d = sqlite3.connect(tmp)
    d.execute("""create table games(url text primary key, source text, site text, gid text, date text, time text,
                 start_utc text, home text, away text, league text, status text, score text, updated real)""")
    rows = s.execute("select url, source, site, gid, date, time, start_utc, home, away, league, status, score, updated from games").fetchall()
    d.executemany("insert or replace into games values (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    # games their source deleted: the servers merging this list delete them too
    d.execute("create table removed(url text primary key, at real)")
    try:
        d.executemany("insert or replace into removed values (?, ?)", s.execute("select url, at from removed").fetchall())
    except sqlite3.OperationalError:
        pass
    d.commit()
    d.execute("vacuum")
    d.close()
    with open(tmp, "rb") as f, gzip.open(out, "wb", compresslevel=9) as g:
        shutil.copyfileobj(f, g)
    tmp.unlink()
    return len(rows)


if __name__ == "__main__":
    n = export()
    print(f"{n} games written to {DATA / 'game-list.db.gz'} ({(DATA / 'game-list.db.gz').stat().st_size // 1024} KB)")

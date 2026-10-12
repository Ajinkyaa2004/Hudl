"""The Progress sheet export, read by column *names* (the sheet gains columns now and then: a
"Video Quality" column arrived in October 2026 and moved every link one column to the right).

    for row in sheet.rows():      # dicts: header, analyst, status, link, r1, l1, r2, l2
"""
from __future__ import annotations
from pathlib import Path
import openpyxl

PROGRESS = Path(__file__).resolve().parent.parent / "data" / "progress.xlsx"
WANT = {"header": "copy to", "game_id": "game id", "analyst": "analyst name", "status": "protocol/roster",
        "link": "protocol link", "r1": "roster team 1", "l1": "team 1 link", "r2": "roster team 2", "l2": "team 2 link",
        "remarks": "remarks", "quality": "video quality"}
FALLBACK = {"header": 2, "game_id": 3, "analyst": 4, "status": 7, "link": 8, "r1": 10, "l1": 11, "r2": 12, "l2": 13, "remarks": 14}


def columns(ws) -> dict:
    """Column index per field from the sheet's header row (the first row naming 'Protocol Link');
    the pre-October layout when no header row is found."""
    for r in ws.iter_rows(min_row=1, max_row=5, values_only=True):
        names = [str(c or "").strip().lower() for c in r]
        if any("protocol link" in n for n in names):
            cols = {}
            for field, key in WANT.items():
                cols[field] = next((i for i, n in enumerate(names) if n.startswith(key)), None)
            return {k: v for k, v in cols.items() if v is not None}
    return dict(FALLBACK)


def rows(path: Path = PROGRESS):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["Progress"]
    cols = columns(ws)
    need = max(cols.values()) + 1
    for r in ws.iter_rows(values_only=True):
        r = tuple(r) + (None,) * max(0, need - len(r))
        h = r[cols["header"]]
        if not (isinstance(h, str) and h.strip().startswith("[")):
            continue
        yield {k: r[i] for k, i in cols.items()}

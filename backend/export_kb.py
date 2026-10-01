"""Pack the knowledge base for a server without a disk (Render Free): the teams, competitions and
learned sources, gzipped and base64-encoded, written to data/knowledge.b64 and copied to the
clipboard. Paste it into Render > Environment > Secret Files as a file named knowledge.b64.

    .venv/bin/python -m backend.export_kb
"""
from __future__ import annotations
import base64, gzip, json, subprocess
from .lookup import load_kb, DATA

if __name__ == "__main__":
    kb = load_kb()
    slim = {k: kb.get(k) or {} for k in ("teams", "tournaments", "comp_sources", "team_sources")}
    raw = json.dumps(slim, ensure_ascii=False, separators=(",", ":")).encode()
    out = base64.b64encode(gzip.compress(raw, 9)).decode()
    (DATA / "knowledge.b64").write_text(out)
    try:
        subprocess.run(["pbcopy"], input=out.encode(), check=True)
        copied = " and copied to the clipboard"
    except Exception:
        copied = ""
    print(f"{len(slim['teams'])} teams, {len(out) // 1024} KB written to data/knowledge.b64{copied}")

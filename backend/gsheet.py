"""The search log, written straight into the owner's Google Sheet with the Google Sheets API.

Before this, every row went through an Apps Script web app attached to the sheet: each change to
that script needed a new deployment by hand. Now a Google *service account* (a robot user the sheet
is shared with, as Editor) writes the rows, so nothing has to be deployed in the sheet again.

The service account's key lives in data/sheet_log.json next to the old web-app address:

    {"url": "...old web app...", "key": "...", "sa": {"client_email": "...", "private_key": "...", "token_uri": "..."}}

That file is private, synced between the servers and kept in the locked backup, like the accounts.
With no "sa" in it, nothing here is used and the old web app keeps receiving the rows.

Standard library + httpx only: the RS256 signature Google asks for is made here (PKCS#1 v1.5 over
SHA-256), so no extra package has to be installed on the servers.
"""
from __future__ import annotations
import base64, hashlib, json, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "data" / "sheet_log.json"
QUEUE = ROOT / "data" / "sheet_queue.json"     # rows not written yet (survives a restart)
SHEET_ID = "1WclJXwbQ6zdBUboSOWw92-PTuRn3w7ycUg2j0oq-ahg"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"
API = "https://sheets.googleapis.com/v4/spreadsheets"
HEAD = ["Time (IST)", "Name", "Header", "Found", "Report link", "Server",
        "Search time (s)", "Result", "Found on", "Competition", "Match ID"]
ACC_HEAD = ["Match ID", "Header", "PuckTrace answer", "PuckTrace link", "Analyst link", "Verdict", "Checked on"]
FLUSH_S = 4            # rows wait at most this long, then go to the sheet together
MAX_QUEUE = 5000

STATE = dict(last_ok=None, last_error=None, written=0)
_lock = threading.Lock()
_rows: list = []
_thread = None
_token: dict = {}
_tab: dict = {}


class SheetError(Exception):
    pass


# ---------------------------------------------------------------- the key and the signature
def _der(buf: bytes, i: int):
    """One DER element at i: (tag, value, index after it)."""
    tag = buf[i]
    n = buf[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(buf[i:i + k], "big")
        i += k
    return tag, buf[i:i + n], i + n


def _seq(buf: bytes) -> list:
    out, i = [], 0
    while i < len(buf):
        tag, val, i = _der(buf, i)
        out.append((tag, val))
    return out


def rsa_key(pem: str) -> tuple:
    """(n, d) from a PEM private key: 'BEGIN PRIVATE KEY' (PKCS#8, what Google gives) or
    'BEGIN RSA PRIVATE KEY' (PKCS#1)."""
    try:
        body = "".join(l for l in pem.strip().splitlines() if l and not l.startswith("-----"))
        der = base64.b64decode(body)
        tag, top, _ = _der(der, 0)
        parts = _seq(top)
        if len(parts) >= 3 and parts[2][0] == 0x04:          # PKCS#8: version, algorithm, OCTET STRING
            tag, top, _ = _der(parts[2][1], 0)
            parts = _seq(top)
        n, d = int.from_bytes(parts[1][1], "big"), int.from_bytes(parts[3][1], "big")
        if n.bit_length() < 2000 or d <= 1:
            raise ValueError("key too short")
        return n, d
    except Exception as e:
        raise SheetError(f"the private key in this file could not be read ({e})")


_SHA256_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def rs256(message: bytes, n: int, d: int) -> bytes:
    k = (n.bit_length() + 7) // 8
    t = _SHA256_INFO + hashlib.sha256(message).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def jwt(sa: dict, now: float = None) -> str:
    now = int(now or time.time())
    head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claim = _b64(json.dumps({"iss": sa["client_email"], "scope": SCOPE,
                             "aud": sa.get("token_uri") or "https://oauth2.googleapis.com/token",
                             "iat": now, "exp": now + 3600}).encode())
    signing = f"{head}.{claim}".encode()
    return f"{head}.{claim}." + _b64(rs256(signing, *rsa_key(sa["private_key"])))


# ---------------------------------------------------------------- configuration
def config() -> dict:
    try:
        return json.loads(CFG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def account(cfg: dict = None) -> dict | None:
    sa = (cfg if cfg is not None else config()).get("sa")
    return sa if isinstance(sa, dict) and sa.get("client_email") and sa.get("private_key") else None


def sheet_id(cfg: dict = None) -> str:
    return (cfg if cfg is not None else config()).get("sheet_id") or SHEET_ID


def clean_key(raw: dict) -> dict:
    """The three fields kept from the JSON key file Google downloads."""
    if not isinstance(raw, dict) or raw.get("type") != "service_account":
        raise SheetError("this is not a service account key file (its \"type\" is not \"service_account\")")
    if not raw.get("client_email") or not raw.get("private_key"):
        raise SheetError("the key file has no client_email or private_key")
    rsa_key(raw["private_key"])
    return dict(client_email=raw["client_email"], private_key=raw["private_key"],
                token_uri=raw.get("token_uri") or "https://oauth2.googleapis.com/token")


# ---------------------------------------------------------------- talking to Google
def _why(r) -> str:
    try:
        e = r.json().get("error")
        msg = e.get("message") if isinstance(e, dict) else (r.json().get("error_description") or str(e))
    except Exception:
        msg = r.text[:200]
    return f"HTTP {r.status_code}: {msg}"


def token(sa: dict) -> str:
    c = _token.get(sa["client_email"])
    if c and c[1] > time.time() + 120 and c[2] == sa["private_key"][-40:]:
        return c[0]
    import httpx
    r = httpx.post(sa.get("token_uri") or "https://oauth2.googleapis.com/token", timeout=30,
                   data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": jwt(sa)})
    if r.status_code != 200:
        raise SheetError("Google did not accept the key (" + _why(r) + ")")
    j = r.json()
    _token[sa["client_email"]] = (j["access_token"], time.time() + int(j.get("expires_in", 3600)), sa["private_key"][-40:])
    return j["access_token"]


def _call(sa: dict, method: str, path: str, sid: str = None, **kw):
    import httpx
    r = httpx.request(method, f"{API}/{sid or sheet_id()}{path}", timeout=kw.pop("timeout", 60),
                      headers={"authorization": "Bearer " + token(sa)}, **kw)
    if r.status_code == 403 and "has not been used" in r.text:
        raise SheetError("the Google Sheets API is not switched on in the key's Google Cloud project")
    if r.status_code in (403, 404):
        raise SheetError(f"the sheet is not shared with {sa['client_email']} as Editor ({_why(r)})")
    if r.status_code >= 400:
        raise SheetError(_why(r))
    return r.json()


def tabs(sa: dict, sid: str = None) -> list:
    j = _call(sa, "GET", "", sid, params={"fields": "sheets.properties(sheetId,title)"})
    return [s["properties"] for s in j.get("sheets", [])]


def _q(title: str) -> str:
    """A tab name as the API wants it in an address: quoted, then made safe for a URL."""
    from urllib.parse import quote
    return quote("'" + title.replace("'", "''") + "'", safe="")


def log_tab(sa: dict, sid: str = None) -> str:
    """The tab the log is on: the one whose first cell says 'Time (IST)' (as the old script chose
    it), else the first tab. Remembered for an hour."""
    sid = sid or sheet_id()
    c = _tab.get(sid)
    if c and c[1] > time.time():
        return c[0]
    props = tabs(sa, sid)
    if not props:
        raise SheetError("the spreadsheet has no tabs")
    title = props[0]["title"]
    try:
        j = _call(sa, "GET", "/values:batchGet", sid, params=[("ranges", "'" + p["title"].replace("'", "''") + "'!A1") for p in props[:12]])
        for p, vr in zip(props, j.get("valueRanges", [])):
            if ((vr.get("values") or [[""]])[0] or [""])[0] == HEAD[0]:
                title = p["title"]
                break
    except SheetError:
        pass
    _tab[sid] = (title, time.time() + 3600)
    return title


def _cell(v):
    """As typed by a person (dates and numbers stay dates and numbers, as with the old script), but
    text that would be read as a formula is kept as text."""
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    return "'" + s if s and s[0] in "=+-@'" else s


def append(sa: dict, rows: list, sid: str = None):
    tab = log_tab(sa, sid)
    _call(sa, "POST", f"/values/{_q(tab)}!A1:append", sid,
          params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
          json={"values": [[_cell(c) for c in r] for r in rows]})


def read_log(sa: dict, sid: str = None) -> list:
    """The log rows as dicts by column name (what the CSV export gave)."""
    tab = log_tab(sa, sid)
    vals = _call(sa, "GET", f"/values/{_q(tab)}!A:K", sid, timeout=120).get("values") or []
    if not vals:
        return []
    head = [str(h) for h in vals[0]]
    return [{h: (r[i] if i < len(r) else "") for i, h in enumerate(head) if h} for r in vals[1:]]


def write_accuracy(sa: dict, rows: list, sid: str = None) -> str:
    if not any(p["title"] == "Accuracy" for p in tabs(sa, sid)):
        _call(sa, "POST", ":batchUpdate", sid, json={"requests": [{"addSheet": {"properties": {"title": "Accuracy"}}}]})
    _call(sa, "POST", "/values/Accuracy!A:G:clear", sid, json={})
    body = [ACC_HEAD] + [[("" if c is None else c) for c in r] for r in rows]
    _call(sa, "PUT", "/values/Accuracy!A1", sid, params={"valueInputOption": "RAW"}, json={"values": body}, timeout=180)
    return f"ok {len(rows)}"


def check(sa: dict, sid: str = None) -> str:
    """Can this key reach the sheet? The log tab's name, or SheetError saying what is missing."""
    _tab.pop(sid or sheet_id(), None)
    return log_tab(sa, sid)


# ---------------------------------------------------------------- the queue of rows
def row_of(d: dict) -> list:
    sec = d.get("seconds")
    mid = str(d.get("match_id") or "")
    return [d.get("time", ""), d.get("name", ""), d.get("header", ""), d.get("found", ""), d.get("link", ""),
            d.get("server", ""), "" if sec is None else sec, d.get("result", ""), d.get("found_on", ""),
            d.get("competition", ""), int(mid) if mid.isdigit() else mid]


def _save():
    try:
        if _rows:
            QUEUE.write_text(json.dumps(_rows), encoding="utf-8")
        elif QUEUE.exists():
            QUEUE.write_text("[]", encoding="utf-8")
    except Exception:
        pass


def _load():
    try:
        old = json.loads(QUEUE.read_text(encoding="utf-8"))
        if isinstance(old, list):
            _rows[:0] = [r for r in old if isinstance(r, dict)][:MAX_QUEUE]
    except Exception:
        pass


def _old_way(cfg: dict, d: dict) -> bool:
    """The Apps Script web app, kept as the spare way for a row Google's API would not take."""
    if not cfg.get("url"):
        return False
    try:
        import httpx
        r = httpx.post(cfg["url"], content=json.dumps(dict(d, key=cfg.get("key", ""))), headers={"content-type": "text/plain"},
                       timeout=30, follow_redirects=True)
        return r.status_code < 400 and r.text.strip().startswith("ok")
    except Exception:
        return False


def flush() -> int:
    """Write every waiting row in one call. On an error the rows go the old way if there is one,
    else they wait for the next try."""
    with _lock:
        batch = list(_rows)
    if not batch:
        return 0
    cfg = config()
    sa = account(cfg)
    try:
        if not sa:
            raise SheetError("no service account key")
        append(sa, [row_of(d) for d in batch])
        STATE.update(last_ok=time.time(), last_error=None, written=STATE["written"] + len(batch))
        sent = batch
    except Exception as e:
        STATE["last_error"] = dict(at=time.time(), error=str(e)[:300])
        print(f"[sheet log] API: {str(e)[:200]}", flush=True)
        sent = [d for d in batch if _old_way(cfg, d)]
    with _lock:
        for d in sent:
            try:
                _rows.remove(d)
            except ValueError:
                pass
        del _rows[:-MAX_QUEUE]
        _save()
    return len(sent)


def _loop():
    while True:
        time.sleep(FLUSH_S)
        try:
            if _rows:
                if flush() == 0:
                    time.sleep(60)          # Google is refusing: do not hammer it
        except Exception as e:
            print(f"[sheet log] {str(e)[:160]}", flush=True)


def start():
    global _thread
    with _lock:
        if _thread is None:
            _load()
            _thread = threading.Thread(target=_loop, daemon=True, name="sheet-log")
            _thread.start()


def add(d: dict):
    """Queue one search-log row (a dict with the old web app's field names)."""
    start()
    with _lock:
        _rows.append(d)
        _save()


def status() -> dict:
    cfg = config()
    sa = account(cfg)
    return dict(mode="api" if sa else ("script" if cfg.get("url") else "off"), email=(sa or {}).get("client_email"),
                spare=bool(cfg.get("url")), waiting=len(_rows), written=STATE["written"],
                last_ok=STATE["last_ok"], last_error=STATE["last_error"])

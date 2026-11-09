"""Accounts: the team signs in with a name alone; the admin with a password.

data/users.json (private: git-ignored, uploaded to the servers, kept in step between them and in the
locked backup like the knowledge base) holds, per person, a salted PBKDF2 hash of the admin password
(never the password itself), the browsers that have used a name, and the admin flag:
    {"users": {"actorix": {"name": "Actorix", "salt": "...", "hash": "...", "admin": true, "v": 1}}}

A name is unique: a new browser must choose a different combination unless the admin has just allowed
that one device. A session token names its user and is signed with APP_SECRET over the user's password
hash, so a new password or a removed account signs that person out everywhere at once.

    .venv/bin/python -m backend.users add "Name" "password" [--admin]   # add, or set a new password
    .venv/bin/python -m backend.users remove "Name"
    .venv/bin/python -m backend.users list
then upload data/users.json to the main server (POST /api/admin/upload?name=users.json)."""
from __future__ import annotations
import base64, hashlib, hmac, json, os, re, secrets, sys, threading
from pathlib import Path

FILE = Path(__file__).resolve().parent.parent / "data" / "users.json"
ROUNDS = 200_000
_cache: dict = {"mtime": None, "users": {}}
_LOCK = threading.Lock()     # one read-modify-write at a time (400 people arriving at 09:00)


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def users() -> dict:
    """{name key: record}, re-read when the file changes (an upload or a sync from the other server)."""
    try:
        m = FILE.stat().st_mtime
    except OSError:
        return {}
    if _cache["mtime"] != m:
        try:
            _cache["users"] = json.loads(FILE.read_text()).get("users") or {}
        except Exception:
            _cache["users"] = {}
        _cache["mtime"] = m
    return _cache["users"]


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ROUNDS).hex()


def verify(name: str, password: str) -> dict | None:
    u = users().get(_key(name))
    if not u or not password or u.get("blocked"):
        return None
    return u if hmac.compare_digest(_hash(password, u["salt"]), u["hash"]) else None


def _sig(secret: bytes, u: dict) -> str:
    return hmac.new(secret, f"pucktrace-user:{_key(u['name'])}:{u['hash']}:{u.get('v', 1)}".encode(), hashlib.sha256).hexdigest()


def _name_sig(secret: bytes, u: dict) -> str:
    return hmac.new(secret, f"pucktrace-name:{_key(u['name'])}:{int(u.get('v', 1))}".encode(), hashlib.sha256).hexdigest()


def _admin_sig(secret: bytes, u: dict) -> str:
    return hmac.new(secret, f"pucktrace-admin:{_key(u['name'])}:{u.get('hash', '')}:{int(u.get('v', 1))}".encode(), hashlib.sha256).hexdigest()


def _pack(tag: str, u: dict, sig: str) -> str:
    return tag + "." + base64.urlsafe_b64encode(u["name"].encode()).decode().rstrip("=") + "." + sig


def token_for(secret: bytes, u: dict) -> str:
    """Old-style token (u1), kept so a session opened before this deploy keeps working."""
    return _pack("u1", u, _sig(secret, u))


def token_name(secret: bytes, u: dict) -> str:
    """A name-session: the public site, no password. It can search, never administer."""
    return _pack("n1", u, _name_sig(secret, u))


def token_admin(secret: bytes, u: dict) -> str:
    """An admin-session: only the admin address with the admin's password can make one."""
    return _pack("a1", u, _admin_sig(secret, u))


def session_of(secret: bytes, token: str) -> dict | None:
    """The account and the kind of session a token is, or None.
    `_session` is "name" (public, name only) or "admin" (the admin address). A name-session
    of an admin account is still only a name-session."""
    try:
        tag, b64, sig = (token or "").split(".")
        name = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode()
    except Exception:
        return None
    u = users().get(_key(name))
    if not u or u.get("blocked"):
        return None
    if tag == "n1":
        kind, ok = "name", hmac.compare_digest(sig, _name_sig(secret, u))
    elif tag == "a1":
        kind, ok = "admin", bool(u.get("hash")) and hmac.compare_digest(sig, _admin_sig(secret, u))
    elif tag == "u1":
        kind, ok = "name", bool(u.get("hash")) and hmac.compare_digest(sig, _sig(secret, u))
    else:
        return None
    return dict(u, _session=kind) if ok else None


def user_of(secret: bytes, token: str) -> dict | None:
    """The account a token belongs to, or None (unknown, removed, blocked, or out of date)."""
    return session_of(secret, token)


def _save(d: dict):
    # write a new file and swap it in: a crash (or the updater reading it) never sees half a file
    text = json.dumps({"users": d}, indent=1, ensure_ascii=False)
    tmp = FILE.with_name(FILE.name + ".tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, FILE)
    except OSError:
        FILE.write_text(text)
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- admin actions (the Admin screen)
# A session is signed over the password hash and "v": a new password, or "v" + 1 (sign out), ends
# every session of that person at once; a blocked person can't sign in or use a session.

ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/l/I


def gen_password() -> str:
    """Easy to read out and type: three groups of four, e.g. 'Kq7m-x4Pz-9wRt'."""
    return "-".join("".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(3))


def clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())[:40]


def public(u: dict) -> dict:
    return {k: u.get(k) for k in ("name", "admin", "blocked", "created", "last_login", "signed_out")} | dict(
        name_only=not bool(u.get("hash")), devices=len(u.get("devices") or []),
        new_device_open=bool(u.get("allow_device_until") and u.get("allow_device_until") >= _now()))


def _now(plus_minutes: int = 0) -> str:
    """India time, whatever the server's own clock zone is."""
    import datetime
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30 + plus_minutes)).strftime("%Y-%m-%d %H:%M")


class NameTaken(Exception):
    """The name is already in use and this browser has never signed in with it."""


def ensure(name: str, device: str = "") -> tuple[dict, bool]:
    """A name-only account: made on first entry, no password. Returns (record, created).

    A name is unique: once it is on PuckTrace, a different browser is told to choose another
    combination (e.g. first name + surname) rather than silently sharing the account. The one
    exception is the admin's "allow a new device" reset, so the real person can return on a new
    laptop or phone: that opens the door once, for a few minutes."""
    name = clean_name(name)
    if len(name) < 2:
        raise ValueError("Please type your name")
    device = (device or "").strip()[:100]       # a browser id, never a file-sized string
    with _LOCK:
        d = dict(users())
        k = _key(name)
        u = d.get(k)
        if u:
            if u.get("blocked"):
                raise PermissionError("This name is blocked. Ask the admin.")
            devs = list(u.get("devices") or [])
            if device and device in devs:
                return u, False
            if device and not devs and not u.get("admin"):
                # an account from before devices were recorded: the first browser to use it claims it
                d[k] = dict(u, devices=[device], last_device=device)
                _save(d)
                return d[k], False
            until = u.get("allow_device_until") or ""
            if device and until and until >= _now():
                d[k] = dict(u, devices=(devs[-8:] + [device]), last_device=device,
                            allow_device_until=None)
                _save(d)
                return d[k], False
            raise NameTaken(name)
        if len(d) >= 2000:
            raise ValueError("This server has reached its user limit")
        u = dict(name=name, admin=False, v=1, created=_now(), name_only=True,
                 devices=[device] if device else [], last_device=device or None)
        d[k] = u
        _save(d)
        return u, True


def allow_new_device(name: str) -> str:
    """The admin lets the real person add one new browser (a new laptop or phone).
    Open for 15 minutes, then the next new device is accepted and the door closes again."""
    until = _now(15)
    with _LOCK:
        d = dict(users())
        u = dict(_get(d, name))
        if u.get("blocked"):
            raise ValueError("This name is blocked. Unblock it first.")
        u["allow_device_until"] = until
        d[_key(name)] = u
        _save(d)
    return until


def _get(d: dict, name: str) -> dict:
    u = d.get(_key(name))
    if not u:
        raise KeyError(f"No user called {name}")
    return u


def add(name: str, password: str | None = None) -> str:
    name = clean_name(name)
    if not name:
        raise ValueError("Give the user a name")
    pw = password or gen_password()
    if len(pw) < 6:
        raise ValueError("The password needs at least 6 characters")
    with _LOCK:
        d = dict(users())
        if _key(name) in d:
            raise ValueError(f"{d[_key(name)]['name']} already exists")
        salt = secrets.token_hex(16)
        d[_key(name)] = dict(name=name, salt=salt, hash=_hash(pw, salt), admin=False, v=1, created=_now())
        _save(d)
    return pw


def new_password(name: str, password: str | None = None) -> str:
    pw = password or gen_password()
    if len(pw) < 6:
        raise ValueError("The password needs at least 6 characters")
    with _LOCK:
        d = dict(users())
        u = dict(_get(d, name))
        u["salt"] = secrets.token_hex(16)
        u["hash"] = _hash(pw, u["salt"])
        u["v"] = int(u.get("v", 1)) + 1
        u["signed_out"] = _now()
        d[_key(name)] = u
        _save(d)
    return pw


def sign_out(name: str):
    with _LOCK:
        d = dict(users())
        u = dict(_get(d, name))
        u["v"] = int(u.get("v", 1)) + 1
        u["signed_out"] = _now()
        d[_key(name)] = u
        _save(d)


def set_blocked(name: str, blocked: bool, me: str = ""):
    with _LOCK:
        d = dict(users())
        u = dict(_get(d, name))
        if blocked and (_key(name) == _key(me) or u.get("admin")):
            raise ValueError("An admin account can't be blocked")
        u["blocked"] = bool(blocked)
        if blocked:
            u["v"] = int(u.get("v", 1)) + 1
            u["signed_out"] = _now()
        d[_key(name)] = u
        _save(d)


def delete(name: str, me: str = ""):
    with _LOCK:
        d = dict(users())
        u = _get(d, name)
        if _key(name) == _key(me) or u.get("admin"):
            raise ValueError("An admin account can't be deleted here")
        d.pop(_key(name))
        _save(d)


def note_login(name: str):
    with _LOCK:
        d = dict(users())
        u = d.get(_key(name))
        now = _now()
        if u and u.get("last_login") != now:      # minute granularity: no file write on every heartbeat
            d[_key(name)] = dict(u, last_login=now)
            _save(d)


def main():
    a = sys.argv[1:]
    d = dict(users())
    if a[:1] == ["add"] and len(a) >= 3:
        name, pw = re.sub(r"\s+", " ", a[1].strip()), a[2]
        old = d.get(_key(name)) or {}
        salt = secrets.token_hex(16)
        d[_key(name)] = dict(name=name, salt=salt, hash=_hash(pw, salt), admin="--admin" in a or bool(old.get("admin")),
                             v=int(old.get("v", 0)) + 1)
        _save(d)
        print(f"{'new password for' if old else 'added'} {name}{' (admin)' if d[_key(name)]['admin'] else ''}")
    elif a[:1] == ["remove"] and len(a) >= 2:
        print("removed" if d.pop(_key(a[1]), None) else "no such user", a[1])
        _save(d)
    elif a[:1] == ["list"]:
        for u in d.values():
            print(u["name"], "(admin)" if u.get("admin") else "")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()

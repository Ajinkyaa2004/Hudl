"""Accounts: each person signs in with their own name and password.

data/users.json (private: git-ignored, uploaded to the servers, kept in step between them and in the
locked backup like the knowledge base) holds, per person, a salted PBKDF2 hash of the password,
never the password itself:
    {"users": {"actorix": {"name": "Actorix", "salt": "...", "hash": "...", "admin": true, "v": 1}}}

A session token names its user and is signed with APP_SECRET over the user's password hash, so a
new password or a removed account signs that person out everywhere at once.

    .venv/bin/python -m backend.users add "Name" "password" [--admin]   # add, or set a new password
    .venv/bin/python -m backend.users remove "Name"
    .venv/bin/python -m backend.users list
then upload data/users.json to the main server (POST /api/admin/upload?name=users.json)."""
from __future__ import annotations
import base64, hashlib, hmac, json, os, re, secrets, sys
from pathlib import Path

FILE = Path(__file__).resolve().parent.parent / "data" / "users.json"
ROUNDS = 200_000
_cache: dict = {"mtime": None, "users": {}}


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


def token_for(secret: bytes, u: dict) -> str:
    return "u1." + base64.urlsafe_b64encode(u["name"].encode()).decode().rstrip("=") + "." + _sig(secret, u)


def user_of(secret: bytes, token: str) -> dict | None:
    """The account a token belongs to, or None (unknown, removed, or its password changed)."""
    try:
        tag, b64, sig = (token or "").split(".")
        if tag != "u1":
            return None
        name = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode()
    except Exception:
        return None
    u = users().get(_key(name))
    return u if u and not u.get("blocked") and hmac.compare_digest(sig, _sig(secret, u)) else None


def _save(d: dict):
    FILE.write_text(json.dumps({"users": d}, indent=1, ensure_ascii=False))


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
    return {k: u.get(k) for k in ("name", "admin", "blocked", "created", "last_login", "signed_out")}


def _now() -> str:
    """India time, whatever the server's own clock zone is."""
    import datetime
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M")


def _get(d: dict, name: str) -> dict:
    u = d.get(_key(name))
    if not u:
        raise KeyError(f"No user called {name}")
    return u


def add(name: str, password: str | None = None) -> str:
    name = clean_name(name)
    if not name:
        raise ValueError("Give the user a name")
    d = dict(users())
    if _key(name) in d:
        raise ValueError(f"{d[_key(name)]['name']} already exists")
    pw = password or gen_password()
    if len(pw) < 6:
        raise ValueError("The password needs at least 6 characters")
    salt = secrets.token_hex(16)
    d[_key(name)] = dict(name=name, salt=salt, hash=_hash(pw, salt), admin=False, v=1, created=_now())
    _save(d)
    return pw


def new_password(name: str, password: str | None = None) -> str:
    d = dict(users())
    u = dict(_get(d, name))
    pw = password or gen_password()
    if len(pw) < 6:
        raise ValueError("The password needs at least 6 characters")
    u["salt"] = secrets.token_hex(16)
    u["hash"] = _hash(pw, u["salt"])
    u["v"] = int(u.get("v", 1)) + 1
    u["signed_out"] = _now()
    d[_key(name)] = u
    _save(d)
    return pw


def sign_out(name: str):
    d = dict(users())
    u = dict(_get(d, name))
    u["v"] = int(u.get("v", 1)) + 1
    u["signed_out"] = _now()
    d[_key(name)] = u
    _save(d)


def set_blocked(name: str, blocked: bool, me: str = ""):
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
    d = dict(users())
    u = _get(d, name)
    if _key(name) == _key(me) or u.get("admin"):
        raise ValueError("An admin account can't be deleted here")
    d.pop(_key(name))
    _save(d)


def note_login(name: str):
    d = dict(users())
    u = d.get(_key(name))
    if u:
        d[_key(name)] = dict(u, last_login=_now())
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

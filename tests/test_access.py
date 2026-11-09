"""The two sessions must not cross.

Run: .venv/bin/python tests/test_access.py

A name-session (the public site) may search and nothing else. An admin-session (the admin address,
password) may do everything. An admin account that signs in on the public site gets a name-session.
Blocking or signing out invalidates that person's tokens on both servers.
"""
import os, sys, tempfile, pathlib

os.environ["APP_PASSWORD"] = "testpw"
os.environ["APP_SECRET"] = "testsecret"
os.environ["PT_OPEN_ACCESS"] = "1"
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import users as U                                    # noqa: E402

U.FILE = pathlib.Path(tempfile.mkdtemp()) / "users.json"
U.add("Ajinkya", "adminpass")
_d = dict(U.users())
_d[U._key("Ajinkya")]["admin"] = True
U._save(_d)

from fastapi.testclient import TestClient                         # noqa: E402
from backend.app import app                                       # noqa: E402

c = TestClient(app)
failures = []


def check(label, condition, detail=""):
    print(("ok   " if condition else "FAIL ") + label + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def token(body):
    return body["token"]


# the public door: a name, no password
r = c.post("/api/login", json={"name": "Rahul Sharma", "device": "dev1"})
check("name-only login succeeds", r.status_code == 200, r.text)
team = token(r.json())
check("it is a name-session, not admin", team.startswith("n1.") and r.json()["admin"] is False)

H = {"x-pt-session": team}
check("a name-session may search", c.post("/api/search/start", json={"header": "x"}, headers=H).status_code == 400)
check("a name-session may read config", c.get("/api/config", headers=H).status_code == 200)
for path in ("/api/history", "/api/sources", "/api/learning", "/api/vpn", "/api/health"):
    check(f"a name-session may not read {path}", c.get(path, headers=H).status_code == 403)
check("a name-session may not save", c.post("/api/save", json={"header": "A vs B | C | 2026-10-01"}, headers=H).status_code == 403)
check("a name-session may not enter the Admin screen", c.get("/api/admin/users", headers=H).status_code == 403)
check("a name-session may not upload data", c.post("/api/admin/upload?name=announce.json", content=b"{}", headers=H).status_code == 403)

# the admin door: password, and an admin account
check("the admin door refuses no password", c.post("/api/login", json={"name": "Ajinkya", "admin": True}).status_code == 401)
r = c.post("/api/login", json={"name": "Ajinkya", "password": "adminpass", "admin": True})
check("the admin door accepts the admin password", r.status_code == 200, r.text)
admin = token(r.json())
check("it is an admin-session", admin.startswith("a1.") and r.json()["admin"] is True)
A = {"x-pt-session": admin}
check("an admin-session may enter the Admin screen", c.get("/api/admin/users", headers=A).status_code == 200)
check("an admin-session may write", c.post("/api/save", json={"header": "x"}, headers=A).status_code == 400)  # parsed -> 400, not 403

# a name is unique: a browser that has never used it is told to pick another combination
r = c.post("/api/login", json={"name": "rahul sharma", "device": "dev2"})
check("a taken name from a new browser is refused", r.status_code == 409, r.text)
check("the refusal suggests another combination", "already on PuckTrace" in r.json().get("detail", ""), r.text)
check("the same browser returns to its name",
      c.post("/api/login", json={"name": "Rahul Sharma", "device": "dev1"}).status_code == 200)
check("a different combination is a new person",
      c.post("/api/login", json={"name": "Rahul Sharma (Pune)", "device": "dev2"}).status_code == 200)
check("a name-session may not open a new device",
      c.post("/api/admin/users/Rahul Sharma/new-device", json={}, headers=H).status_code == 403)
r = c.post("/api/admin/users/Rahul Sharma/new-device", json={}, headers=A)
check("the admin can open one new device", r.status_code == 200, r.text)
check("the new browser is let in once",
      c.post("/api/login", json={"name": "Rahul Sharma", "device": "dev9"}).status_code == 200)
check("the door closes again after that one device",
      c.post("/api/login", json={"name": "Rahul Sharma", "device": "dev10"}).status_code == 409)

# an account made before devices were recorded (an older password account) is claimed by its first browser
U.add("Legacy Analyst", "oldpass")
check("an old account's first browser claims it",
      c.post("/api/login", json={"name": "Legacy Analyst", "device": "legacy1"}).status_code == 200)
check("after claiming, a second browser is refused",
      c.post("/api/login", json={"name": "Legacy Analyst", "device": "legacy2"}).status_code == 409)

# an admin account on the team site is only a name (and also obeys the unique-name rule)
check("an admin's name from a new browser is refused",
      c.post("/api/login", json={"name": "Ajinkya", "device": "devA"}).status_code == 409)
check("the admin can allow its own new device",
      c.post("/api/admin/users/Ajinkya/new-device", json={}, headers=A).status_code == 200)
r = c.post("/api/login", json={"name": "Ajinkya", "device": "devA"})
check("an admin's name login is not admin", r.status_code == 200 and r.json()["admin"] is False and token(r.json()).startswith("n1."), r.text)
check("an admin's name-session cannot administer",
      c.get("/api/admin/users", headers={"x-pt-session": token(r.json())}).status_code == 403)

# block ends the name-session everywhere
check("blocking a person succeeds", c.post("/api/admin/users/Rahul Sharma/block", json={"blocked": True}, headers=A).status_code == 200)
check("their old session is refused", c.get("/api/config", headers=H).status_code == 401)
check("they cannot enter again", c.post("/api/login", json={"name": "Rahul Sharma"}).status_code == 403)

# an old (u1) token from before this deploy is a name-session, never admin
from backend.app import _SECRET                                   # noqa: E402
u1 = U.token_for(_SECRET, U.users()[U._key("Ajinkya")])
check("an old u1 token cannot administer", c.get("/api/admin/users", headers={"x-pt-session": u1}).status_code == 403)
check("an old u1 token can still search", c.get("/api/config", headers={"x-pt-session": u1}).status_code == 200)

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all access checks passed")

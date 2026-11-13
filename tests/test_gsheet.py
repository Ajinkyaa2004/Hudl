"""The search log written straight into the Google Sheet (backend/gsheet.py).

Run: .venv/bin/python tests/test_gsheet.py

Google is replaced by a stand-in, so no real key and no network are used. The signature itself is
checked with openssl when it is installed.
"""
import base64, json, os, pathlib, shutil, subprocess, sys, tempfile, time

os.environ["APP_PASSWORD"] = "testpw"
os.environ["APP_SECRET"] = "testsecret"
os.environ["PT_OPEN_ACCESS"] = "1"
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = pathlib.Path(tempfile.mkdtemp())

from backend import users as U, gsheet as G                       # noqa: E402

U.FILE = TMP / "users.json"
U.add("Ajinkya", "adminpass")
_d = dict(U.users())
_d[U._key("Ajinkya")]["admin"] = True
U._save(_d)
G.CFG, G.QUEUE = TMP / "sheet_log.json", TMP / "sheet_queue.json"
G.FLUSH_S = 0.2

failures = []


def check(label, condition, detail=""):
    print(("ok   " if condition else "FAIL ") + label + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(label)


# ---------------------------------------------------------------- a throw-away key
PEM = None
if shutil.which("openssl"):
    key = TMP / "k.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(key)],
                   check=True, capture_output=True)
    PEM = key.read_text()
    check("a PKCS#8 key (as Google gives) is read", PEM.startswith("-----BEGIN PRIVATE KEY") and G.rsa_key(PEM)[0].bit_length() == 2048)
    msg = TMP / "m.bin"
    msg.write_bytes(b"header.claim")
    (TMP / "s.bin").write_bytes(G.rs256(b"header.claim", *G.rsa_key(PEM)))
    subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(TMP / "pub.pem")], check=True, capture_output=True)
    v = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(TMP / "pub.pem"), "-signature", str(TMP / "s.bin"), str(msg)],
                       capture_output=True, text=True)
    check("openssl accepts the RS256 signature", "Verified OK" in v.stdout, v.stdout + v.stderr)
    p1 = subprocess.run(["openssl", "pkey", "-in", str(key), "-traditional"], capture_output=True, text=True).stdout
    if "BEGIN RSA PRIVATE KEY" in p1:
        check("a PKCS#1 key gives the same numbers", G.rsa_key(p1) == G.rsa_key(PEM))
    SA = dict(type="service_account", client_email="robot@proj.iam.gserviceaccount.com", private_key=PEM,
              token_uri="https://oauth2.googleapis.com/token")
    t = G.jwt(SA, now=1_800_000_000).split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)                      # noqa: E731
    claim = json.loads(base64.urlsafe_b64decode(pad(t[1])))
    check("the token request names the account, the scope and one hour",
          len(t) == 3 and claim["iss"] == SA["client_email"] and claim["scope"] == G.SCOPE and claim["exp"] - claim["iat"] == 3600
          and json.loads(base64.urlsafe_b64decode(pad(t[0])))["alg"] == "RS256")
else:
    print("skip the signature checks: openssl is not installed")
    SA = dict(type="service_account", client_email="robot@proj.iam.gserviceaccount.com", private_key="x")
    G.rsa_key = lambda pem: (1 << 2047 | 1, 3)

for bad, why in (({"type": "authorized_user"}, "not a service account"), ({"type": "service_account"}, "no e-mail"),
                 ({"type": "service_account", "client_email": "a@b", "private_key": "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----"}, "broken key")):
    try:
        G.clean_key(bad)
        check(f"a wrong key file is refused ({why})", PEM is None and why == "broken key")
    except G.SheetError:
        check(f"a wrong key file is refused ({why})", True)

# ---------------------------------------------------------------- Google, replaced
import httpx                                                      # noqa: E402

CALLS, MODE = [], {"share": True, "append": True, "old": True}


class R:
    def __init__(self, code, body):
        self.status_code, self._b = code, body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return self._b if not isinstance(self._b, str) else json.loads(self._b)


def fake_post(url, **kw):
    CALLS.append(("POST", url, kw))
    if "oauth2" in url:
        return R(200, {"access_token": "tok", "expires_in": 3600})
    return R(200, "ok") if MODE["old"] else R(500, "no")       # the old web app


def fake_request(method, url, **kw):
    CALLS.append((method, url, kw))
    if not MODE["share"]:
        return R(403, {"error": {"message": "The caller does not have permission"}})
    if url.endswith(G.SHEET_ID):
        return R(200, {"sheets": [{"properties": {"sheetId": 5, "title": "Dashboard"}}, {"properties": {"sheetId": 0, "title": "Search log"}},
                                  {"properties": {"sheetId": 9, "title": "Accuracy"}}]})
    if "values:batchGet" in url:
        return R(200, {"valueRanges": [{"values": [["PuckTrace"]]}, {"values": [["Time (IST)"]]}, {"values": [["Match ID"]]}]})
    if ":append" in url:
        return R(200, {}) if MODE["append"] else R(429, {"error": {"message": "Quota exceeded"}})
    if method == "GET" and "/values/" in url:
        return R(200, {"values": [G.HEAD, ["2026-11-13 10:00:00", "Asha", "[1] A - B | C | 2026-11-12", "Yes", "http://x", "hudl", "3.2", "Found"]]})
    return R(200, {})


httpx.post, httpx.request = fake_post, fake_request

check("the log tab is the one headed 'Time (IST)', not the first tab", G.check(SA) == "Search log")
G.CFG.write_text(json.dumps({"url": "https://script.example/exec", "key": "K", "sa": G.clean_key(SA) if PEM else SA}))
check("status says automatic and never shows the key", G.status()["mode"] == "api" and "private_key" not in json.dumps(G.status()))

row = dict(time="2026-11-13 22:00:00", name="=HYPERLINK(\"http://evil\")", header="[2286907] A 0 - 0 B | Cup | 2026-11-07T05:30:00 | RL:8",
           found="Yes", link="https://site/game/1", server="nas", seconds=4.2, result="Found", found_on="site", competition="Cup", match_id="2286907")
CALLS.clear()
G.add(row)
G.add(dict(row, name="-", seconds=None, match_id=""))
check("a waiting row is kept on disk until it is written", len(json.loads(G.QUEUE.read_text())) == 2)
time.sleep(1.2)
ap = [c for c in CALLS if ":append" in c[1]]
body = ap[0][2]["json"]["values"] if ap else []
check("two searches go to the sheet in one call", len(ap) == 1 and len(body) == 2, str(len(ap)))
check("the row has the sheet's eleven columns in order", bool(body) and len(body[0]) == 11 and body[0][0] == row["time"] and body[0][2] == row["header"] and body[0][5] == "nas")
check("a name that looks like a formula is written as text", bool(body) and body[0][1].startswith("'="))
check("seconds and the match id are numbers; an empty time is empty", bool(body) and body[0][6] == 4.2 and body[0][10] == 2286907 and body[1][6] == "")
check("the append is 'as typed' on the log tab", bool(ap) and ap[0][2]["params"]["valueInputOption"] == "USER_ENTERED" and "Search%20log" in ap[0][1])
check("nothing is left waiting", G.status()["waiting"] == 0 and json.loads(G.QUEUE.read_text()) == [] and G.status()["written"] == 2)
check("no row went to the old web app", not [c for c in CALLS if "script.example" in c[1]])

MODE["append"] = False                                            # Google refuses: the spare way takes the row
CALLS.clear()
G.add(row)
time.sleep(1.2)
old = [c for c in CALLS if "script.example" in c[1]]
check("when Google refuses, the row goes through the old web app with its key", len(old) == 1 and json.loads(old[0][2]["content"])["key"] == "K")
check("and the refusal is shown in the status", G.status()["waiting"] == 0 and "Quota" in (G.status()["last_error"] or {}).get("error", ""))
MODE["old"] = False
G.add(row)
time.sleep(1.2)
check("with both ways down the row waits instead of being lost", G.status()["waiting"] == 1)
MODE["append"] = MODE["old"] = True
G.flush()
check("and is written when Google answers again", G.status()["waiting"] == 0)

rows = G.read_log(SA)
check("the log is read back by column name", rows and rows[0]["Name"] == "Asha" and rows[0]["Result"] == "Found" and rows[0]["Match ID"] == "")
CALLS.clear()
out = G.write_accuracy(SA, [["1", "h", "Found", "a", "b", "Correct", "2026-11-13"]])
put = [c for c in CALLS if c[0] == "PUT"]
check("the Accuracy tab is cleared, then filled with its heading and rows",
      out == "ok 1" and any(":clear" in c[1] for c in CALLS) and put and put[0][2]["json"]["values"][0] == G.ACC_HEAD and len(put[0][2]["json"]["values"]) == 2)

# ---------------------------------------------------------------- the Admin routes
from fastapi.testclient import TestClient                         # noqa: E402
from backend import app as A                                      # noqa: E402

A._push_file = lambda name: PUSHED.append(name)
PUSHED = []
c = TestClient(A.app)
team = c.post("/api/login", json={"name": "Rahul Sharma", "device": "d1"}).json()["token"]
adm = c.post("/api/login", json={"name": "Ajinkya", "password": "adminpass", "admin": True}).json()["token"]
T, AD = {"x-pt-session": team}, {"x-pt-session": adm}
check("a name-session may not see the log key status", c.get("/api/admin/logkey", headers=T).status_code == 403)
check("a name-session may not set a key", c.post("/api/admin/logkey", content=json.dumps(SA), headers=T).status_code == 403)
check("no session may not set a key", c.post("/api/admin/logkey", content=json.dumps(SA)).status_code in (401, 403))
check("an admin sees the status without the key", c.get("/api/admin/logkey", headers=AD).status_code == 200 and "private_key" not in c.get("/api/admin/logkey", headers=AD).text)
check("something that is not a key file is refused", c.post("/api/admin/logkey", content=b"hello", headers=AD).status_code == 400
      and c.post("/api/admin/logkey", content=json.dumps({"type": "authorized_user"}), headers=AD).status_code == 400)

G.CFG.write_text(json.dumps({"url": "https://script.example/exec", "key": "K"}))
MODE["share"] = False
r = c.post("/api/admin/logkey", content=json.dumps(SA), headers=AD)
check("a key the sheet is not shared with is refused, with the e-mail to share it with", r.status_code == 400 and SA["client_email"] in r.json()["detail"], r.text)
check("and nothing was stored", "sa" not in json.loads(G.CFG.read_text()) and c.get("/api/admin/logkey", headers=AD).json()["mode"] == "script")
MODE["share"] = True
r = c.post("/api/admin/logkey", content=json.dumps(SA), headers=AD)
cfg = json.loads(G.CFG.read_text())
check("a good key is stored next to the old address, and sent to the other server",
      r.status_code == 200 and r.json()["tab"] == "Search log" and cfg["sa"]["client_email"] == SA["client_email"] and bool(cfg["url"]) and "sheet_log.json" in PUSHED, f"{PUSHED} {list(cfg)}")
check("the answer does not carry the key back", "private_key" not in r.text and "BEGIN" not in r.text)
r = c.delete("/api/admin/logkey", headers=AD)
check("removing the key returns to the old way", r.status_code == 200 and r.json()["mode"] == "script" and "sa" not in json.loads(G.CFG.read_text()))

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED: " + "; ".join(failures))
sys.exit(1 if failures else 0)

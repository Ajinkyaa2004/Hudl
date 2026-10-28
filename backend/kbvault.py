"""Locked backup of the private data files (knowledge base, learned spellings, search-log
settings), so the Render backup, which has no disk, starts complete even while the NAS is off.

A GitHub Actions job (.github/workflows/kb-backup.yml) copies the files every 30 minutes from the
NAS (or from Render when the NAS is off), locks them with KB_KEY and publishes the locked file as
the release asset kb-backup/kb.vault. KB_KEY is a random key kept only as a GitHub secret and a
Render environment variable: without it the file is noise. Render opens it when it is running
without a knowledge base and the NAS can't be reached.

Lock: SHA-256 in counter mode as the key stream and HMAC-SHA256 over nonce + locked text; standard
library only, so neither server needs a new package.

    python -m backend.kbvault backup kb.vault     # (GitHub job) env KB_KEY, APP_PASSWORD
"""
from __future__ import annotations
import base64, gzip, hashlib, hmac, json, os, secrets, struct, sys, time
from pathlib import Path

MAGIC = b"PTV1"
VAULT_URL = "https://github.com/Ajinkyaa2004/Hudl/releases/download/kb-backup/{name}"
FILES = ("knowledge.json", "aliases_learned.json", "sheet_log.json", "gamesheet_teams.json")
SERVERS = ("https://nas.actorix.in", "https://pucktrace-api-8u5i.onrender.com")
MIN_TEAMS = 5000          # never back up (or restore) a knowledge base that is empty or thin


def _keys(key: str) -> tuple[bytes, bytes]:
    k = hashlib.sha256(key.encode()).digest()
    return hashlib.sha256(b"enc" + k).digest(), hashlib.sha256(b"mac" + k).digest()


def _xor(data: bytes, k: bytes, nonce: bytes) -> bytes:
    stream = b"".join(hashlib.sha256(k + nonce + struct.pack(">Q", i)).digest() for i in range(len(data) // 32 + 1))
    n = len(data)
    return (int.from_bytes(data, "big") ^ int.from_bytes(stream[:n], "big")).to_bytes(n, "big")


def seal(data: bytes, key: str) -> bytes:
    ek, mk = _keys(key)
    nonce = secrets.token_bytes(16)
    ct = _xor(data, ek, nonce)
    return MAGIC + nonce + hmac.new(mk, nonce + ct, hashlib.sha256).digest() + ct


def unseal(blob: bytes, key: str) -> bytes:
    if blob[:4] != MAGIC:
        raise ValueError("not a PuckTrace vault")
    ek, mk = _keys(key)
    nonce, tag, ct = blob[4:20], blob[20:52], blob[52:]
    if not hmac.compare_digest(tag, hmac.new(mk, nonce + ct, hashlib.sha256).digest()):
        raise ValueError("wrong key or damaged vault")
    return _xor(ct, ek, nonce)


def _teams(raw: bytes) -> int:
    try:
        return len(json.loads(raw).get("teams") or {})
    except Exception:
        return 0


def fetch_bundle(password: str) -> dict | None:
    """The three files from the first server that has a full knowledge base, with their times."""
    import httpx
    for base in SERVERS:
        try:
            with httpx.Client(timeout=120) as c:
                r = c.post(f"{base}/api/login", json={"password": password})
                r.raise_for_status()
                c.headers["x-pt-session"] = r.json().get("token") or ""
                files = {}
                for name in FILES:
                    r = c.get(f"{base}/api/admin/data", params={"name": name})
                    if r.status_code != 200:
                        continue
                    json.loads(r.content)
                    files[name] = dict(mtime=float(r.headers.get("x-data-time") or 0), data=base64.b64encode(r.content).decode())
            kb = files.get("knowledge.json")
            n = _teams(base64.b64decode(kb["data"])) if kb else 0
            print(f"{base}: {len(files)} files, {n} teams", flush=True)
            if n >= MIN_TEAMS:
                return dict(at=time.time(), source=base, files=files)
        except Exception as e:
            print(f"{base}: {type(e).__name__}: {e}", flush=True)
    return None


def fingerprint(bundle: dict) -> str:
    """Changes only when a file's content changes (not with each copy)."""
    return hashlib.sha256(b"".join(base64.b64decode(bundle["files"][n]["data"]) for n in sorted(bundle["files"]))).hexdigest()


def restore(data_dir: Path, key: str) -> list[str]:
    """Download and open the vault; write each file that is missing here or older here. Files
    keep their original times, so the NAS doesn't take them for newer when it comes back."""
    import httpx
    for name in ("kb.vault", "kb-spare.vault"):
        try:
            r = httpx.get(VAULT_URL.format(name=name), follow_redirects=True, timeout=120)
            if r.status_code != 200:
                continue
            bundle = json.loads(gzip.decompress(unseal(r.content, key)))
        except Exception as e:
            print(f"[vault] {name}: {e}", flush=True)
            continue
        done = []
        for fname, f in bundle["files"].items():
            raw = base64.b64decode(f["data"])
            if fname == "knowledge.json" and _teams(raw) < MIN_TEAMS:
                continue
            p = data_dir / fname
            here = p.stat().st_mtime if p.exists() else 0
            if fname == "knowledge.json" and p.exists() and _teams(p.read_bytes()) >= MIN_TEAMS and here >= f["mtime"]:
                continue
            if fname != "knowledge.json" and p.exists() and here >= f["mtime"]:
                continue
            p.write_bytes(raw)
            os.utime(p, (f["mtime"], f["mtime"]))
            done.append(fname)
        return done
    return []


def main():
    if len(sys.argv) < 3 or sys.argv[1] != "backup":
        print(__doc__)
        sys.exit(1)
    key, password = os.environ.get("KB_KEY"), os.environ.get("APP_PASSWORD")
    if not key or not password:
        print("KB_KEY and APP_PASSWORD are needed")
        sys.exit(1)
    bundle = fetch_bundle(password)
    if not bundle:
        print("no server has a full knowledge base right now: nothing backed up")
        return
    out = Path(sys.argv[2])
    out.write_bytes(seal(gzip.compress(json.dumps(bundle).encode(), 9), key))
    out.with_suffix(".sha").write_text(fingerprint(bundle) + "\n")
    print(f"locked {len(bundle['files'])} files from {bundle['source']} -> {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()

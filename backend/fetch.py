"""HTTP and headless-browser fetching with a small cache."""
from __future__ import annotations
import asyncio, time
import httpx

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0.0.0 Safari/537.36")
_cache: dict = {}
import hashlib, json as _json
from pathlib import Path as _Path
DISK = _Path(__file__).resolve().parent.parent / "data" / "cache"
DISK.mkdir(parents=True, exist_ok=True)


def _disk_get(url, ttl):
    f = DISK / (hashlib.sha1(url.encode()).hexdigest() + ".txt")
    if f.exists() and time.time() - f.stat().st_mtime < ttl:
        try:
            return f.read_text()
        except Exception:
            return None
    return None


def _disk_put(url, text):
    try:
        (DISK / (hashlib.sha1(url.encode()).hexdigest() + ".txt")).write_text(text)
    except Exception:
        pass
_client: httpx.AsyncClient | None = None
_client_loop = None
_pw = None
_browser = None
class _LoopSem:
    """A semaphore per event loop. Python 3.9 binds asyncio primitives to the loop that exists
    when they are created, which breaks scripts that call asyncio.run more than once."""
    def __init__(self, n: int):
        self.n, self.by = n, {}

    def _get(self):
        loop = asyncio.get_running_loop()
        s = self.by.get(id(loop))
        if s is None or self.by.get(("loop", id(loop))) is not loop:
            s = self.by[id(loop)] = asyncio.Semaphore(self.n)
            self.by[("loop", id(loop))] = loop
        return s

    async def __aenter__(self):
        await self._get().acquire()

    async def __aexit__(self, *exc):
        self._get().release()


_pw_lock = _LoopSem(1)
_pw_sem = _LoopSem(3)
_pw_loop = None


def _cached(key, ttl):
    v = _cache.get(key)
    if v and time.time() - v[0] < ttl:
        return v[1]
    return None


def _store(key, val):
    _cache[key] = (time.time(), val)
    if len(_cache) > 500:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
            _cache.pop(k, None)


async def get_text(url: str, ttl: int = 600, timeout: float = 15.0, headers: dict | None = None) -> str | None:
    """Plain GET. Returns body text or None. Cached by URL."""
    global _client
    hit = _cached(("http", url), ttl)
    if hit is not None:
        return hit
    if ttl >= 3600:
        d = _disk_get(url, ttl)
        if d is not None:
            _store(("http", url), d)
            return d
    global _client_loop
    if _client is not None and _client_loop is not asyncio.get_running_loop():
        _client = None                      # connections belong to an earlier asyncio.run
    if _client is None:
        _client_loop = asyncio.get_running_loop()
        _client = httpx.AsyncClient(headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"}, follow_redirects=True, timeout=timeout)
    try:
        r = await _client.get(url, headers=headers or {})
        if r.status_code != 200:
            return None
        _store(("http", url), r.text)
        if ttl >= 3600:
            _disk_put(url, r.text)
        return r.text
    except Exception:
        return None


async def get_json(url: str, ttl: int = 600, timeout: float = 20.0):
    import json
    t = await get_text(url, ttl, timeout)
    if not t:
        return None
    try:
        return json.loads(t)
    except Exception:
        return None


_ctx = None


async def _context():
    """One persistent browser context so Cloudflare clearance cookies are reused."""
    global _pw, _browser, _ctx, _pw_loop
    async with _pw_lock:
        loop = asyncio.get_running_loop()
        if _browser is not None and _pw_loop is not loop:     # a new asyncio.run: the old browser is gone
            _pw = _browser = _ctx = None
        _pw_loop = loop
        if _browser is None:
            from playwright.async_api import async_playwright
            _pw = await async_playwright().start()
            _browser = await _pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        if _ctx is None:
            _ctx = await _browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 1400, "height": 8000})
    return _ctx


def _blocked(title: str) -> bool:
    t = (title or "").lower()
    return "just a moment" in t or "attention required" in t or "security verification" in t


async def browser_page(url: str, wait_ms: int = 6000, ttl: int = 600, js: str | None = None, viewport: dict | None = None, retries: int = 1):
    """Load a page in headless Chromium (passes most Cloudflare checks).
    Returns the page HTML, or the result of `js` evaluated in the page. None when blocked."""
    key = ("pw", url, js)
    hit = _cached(key, ttl)
    if hit is not None:
        return hit
    await _context()
    for attempt in range(retries + 1):
        async with _pw_sem:
            ctx = await _browser.new_context(user_agent=UA, locale="en-US", viewport=viewport or {"width": 1400, "height": 8000})
            pg = await ctx.new_page()
            try:
                await pg.goto(url, wait_until="domcontentloaded", timeout=45000)
                for _ in range(12):
                    if not _blocked(await pg.title()):
                        break
                    await pg.wait_for_timeout(2000)
                await pg.wait_for_timeout(wait_ms)
                if _blocked(await pg.title()):
                    continue
                out = await pg.evaluate(js) if js else await pg.content()
                _store(key, out)
                return out
            except Exception:
                continue
            finally:
                await ctx.close()
        await asyncio.sleep(5)
    return None


SHOTS = DISK / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)
_shot_gates: dict = {}
_shot_last: dict = {}
# hide cookie and consent overlays without accepting anything
_HIDE_OVERLAYS = r"""() => {
  const words = /cookie|consent|privacy|gdpr|integritet|samtycke|datenschutz|eväste|souhlas|personvern|confidentialit|zustimm|akzeptier|we value your|vi värdesätter/i;
  const vw = innerWidth, vh = innerHeight;
  const hide = el => el.style.setProperty('display','none','important');
  for (const el of document.querySelectorAll('body *')) {
    const st = getComputedStyle(el);
    if (st.position !== 'fixed' && st.position !== 'sticky') continue;
    const r = el.getBoundingClientRect();
    const big = r.width * r.height > vw * vh * 0.5;
    const txt = (el.innerText || '').slice(0, 3000);
    if (words.test(txt) || words.test(el.id + ' ' + el.className) || (el.tagName === 'IFRAME' && /consent|cmp|sp_message|privacy/i.test(el.src + el.id))) hide(el);
    else if (big && txt.trim().length < 20 && parseFloat(st.opacity) > 0 && /rgba?\(/.test(st.backgroundColor) && st.backgroundColor !== 'rgba(0, 0, 0, 0)') hide(el);  // dimming backdrop
  }
  for (const el of document.querySelectorAll('[id^="sp_message"],#qc-cmp2-container,.qc-cmp2-container,#onetrust-consent-sdk,#CybotCookiebotDialog,.fc-consent-root,#usercentrics-root,#didomi-host')) hide(el);
  for (const el of [document.documentElement, document.body]) { el.style.setProperty('overflow','auto','important'); el.style.setProperty('position','static','important'); }
  document.body.classList.remove('modal-open','no-scroll','noscroll');
}"""


async def screenshot(url: str, ttl: int = 6 * 3600, width: int = 1280, height: int = 1300) -> bytes | None:
    """JPEG of the top of a page, for previews. Cached on disk. None when the page is blocked or fails."""
    from urllib.parse import urlparse
    f = SHOTS / (hashlib.sha1(url.encode()).hexdigest() + ".jpg")
    if f.exists() and time.time() - f.stat().st_mtime < ttl:
        return f.read_bytes()
    miss = SHOTS / (hashlib.sha1(url.encode()).hexdigest() + ".fail")
    if miss.exists() and time.time() - miss.stat().st_mtime < 1800:
        return None
    d = urlparse(url).netloc.lower().replace("www.", "")
    gate = _shot_gates.setdefault(d, asyncio.Lock())
    await _context()
    async with gate:                       # one load at a time per site, a little apart
        wait = 2.5 - (time.time() - _shot_last.get(d, 0))
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with _pw_sem:
                ctx = await _browser.new_context(user_agent=UA, locale="en-US", viewport={"width": width, "height": height})
                pg = await ctx.new_page()
                try:
                    await pg.goto(url, wait_until="domcontentloaded", timeout=30000)
                    for _ in range(8):
                        if not _blocked(await pg.title()):
                            break
                        await pg.wait_for_timeout(1500)
                    try:
                        await pg.wait_for_load_state("networkidle", timeout=3500)
                    except Exception:
                        pass
                    await pg.wait_for_timeout(800)
                    if _blocked(await pg.title()):
                        miss.write_text("blocked")
                        return None
                    try:
                        await pg.evaluate(_HIDE_OVERLAYS)
                        await pg.wait_for_timeout(150)
                    except Exception:
                        pass
                    img = await pg.screenshot(type="jpeg", quality=68)
                finally:
                    await ctx.close()
        except Exception:
            miss.write_text("failed")
            return None
        finally:
            _shot_last[d] = time.time()
    f.write_bytes(img)
    return img

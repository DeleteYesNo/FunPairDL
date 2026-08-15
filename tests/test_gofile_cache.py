"""Unit tests for GoFile module-level token TTL caches (audit [7]).

Guest account token and website token must be cached at module level (shared
by probe and resolve paths), expire after TOKEN_CACHE_TTL_SECONDS, and be
invalidated + refetched on auth failures. The paid-token path must bypass the
cache entirely.

The website token is no longer a static string scraped from a JS bundle — it
is computed per account per 4h window; see test_gofile_wt.
"""
import asyncio
import json

import funpairdl.providers.gofile as gofile_mod
from funpairdl.providers.gofile import GoFileProvider, invalidate_token_cache

# Stand-in for GoFile's obfuscated bundle: same contract, trivial body.
FAKE_BUNDLE = "function generateWT(t) { return 'wt-' + t; }"


class _Resp:
    """Fake aiohttp response usable as async context manager."""

    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise AssertionError(f"unexpected HTTP {self.status}")

    async def json(self):
        return self._payload

    async def text(self):
        if isinstance(self._payload, str):
            return self._payload
        return json.dumps(self._payload)


class _FakeSession:
    """Fake session for direct _get_token/_get_website_token calls."""

    def __init__(self, guest_token="guesttok", js_text=FAKE_BUNDLE):
        self.post_calls = 0
        self.get_calls = 0
        self._guest_token = guest_token
        self._js_text = js_text

    def post(self, url, **kw):
        self.post_calls += 1
        return _Resp({"data": {"token": self._guest_token}})

    def get(self, url, **kw):
        self.get_calls += 1
        return _Resp(self._js_text)


def setup_function(_fn):
    # Each test starts with cold caches.
    invalidate_token_cache()


def test_guest_token_cached_across_calls_and_instances():
    sess = _FakeSession()

    async def run():
        t1 = await GoFileProvider()._get_token(sess)
        t2 = await GoFileProvider()._get_token(sess)  # new instance, same module cache
        return t1, t2

    t1, t2 = asyncio.run(run())
    assert t1 == t2 == "guesttok"
    assert sess.post_calls == 1


def test_paid_token_bypasses_cache_and_network():
    sess = _FakeSession()
    tok = asyncio.run(GoFileProvider(token="paid123")._get_token(sess))
    assert tok == "paid123"
    assert sess.post_calls == 0
    # Paid token must not be written into the guest cache.
    assert gofile_mod._guest_token_cache is None


def test_wt_token_cached():
    sess = _FakeSession()

    async def run():
        w1 = await GoFileProvider()._get_website_token(sess)
        w2 = await GoFileProvider()._get_website_token(sess)
        return w1, w2

    w1, w2 = asyncio.run(run())
    # Derived from the account token by the site's own generator.
    assert w1 == w2 == "wt-guesttok"
    assert sess.get_calls == 1, "the bundle should be fetched once, then cached"


def test_wt_is_refetched_after_invalidation():
    """A rejected request invalidates, so a rotated salt is picked up rather
    than being served from cache until the TTL expires."""
    sess = _FakeSession()

    async def run():
        return await GoFileProvider()._get_website_token(sess)

    assert asyncio.run(run()) == "wt-guesttok"
    assert sess.get_calls == 1
    assert asyncio.run(run()) == "wt-guesttok"
    assert sess.get_calls == 1  # served from cache

    invalidate_token_cache()
    asyncio.run(run())
    assert sess.get_calls == 2


def test_unusable_bundle_raises_instead_of_silently_using_a_stale_value():
    """The old code fell back to a hardcoded token when the scrape failed,
    which turned a broken contract into an opaque 401 much later."""
    import pytest

    sess = _FakeSession(js_text="nothing useful here")
    with pytest.raises(RuntimeError, match="generateWT"):
        asyncio.run(GoFileProvider()._get_website_token(sess))


def test_ttl_expiry_refetches_guest_token():
    sess = _FakeSession()
    asyncio.run(GoFileProvider()._get_token(sess))
    assert sess.post_calls == 1

    # Age the cache entry past the TTL.
    tok, ts = gofile_mod._guest_token_cache
    gofile_mod._guest_token_cache = (
        tok, ts - gofile_mod.TOKEN_CACHE_TTL_SECONDS - 1)

    asyncio.run(GoFileProvider()._get_token(sess))
    assert sess.post_calls == 2


class _ResolveSession:
    """Fake aiohttp.ClientSession for resolve(): first contents call 401,
    second succeeds — exercising invalidate-and-retry."""

    def __init__(self):
        self.contents_calls = 0
        self.post_calls = 0
        self.js_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, **kw):
        self.post_calls += 1
        return _Resp({"data": {"token": f"guest{self.post_calls}"}})

    def get(self, url, **kw):
        if "/contents/" in url:
            self.contents_calls += 1
            if self.contents_calls == 1:
                # What a stale website token actually looks like.
                return _Resp('{"status":"error-notPremium","data":{}}', status=401)
            return _Resp({
                "status": "ok",
                "data": {
                    "type": "file",
                    "name": "video.mp4",
                    "size": 123,
                    "link": "https://store1.gofile.io/download/x/video.mp4",
                },
            })
        self.js_calls += 1
        return _Resp(FAKE_BUNDLE)


def test_resolve_auth_failure_invalidates_and_retries(monkeypatch):
    sess = _ResolveSession()
    monkeypatch.setattr(gofile_mod.aiohttp, "ClientSession",
                        lambda *a, **k: sess)

    rf = asyncio.run(GoFileProvider().resolve("https://gofile.io/d/abc123"))
    assert rf.filename == "video.mp4"
    assert rf.total_size == 123
    # 401 -> invalidate -> fresh guest token + wt -> success on 2nd call.
    assert sess.contents_calls == 2
    assert sess.post_calls == 2
    assert sess.js_calls == 2
    assert "guest2" in rf.headers["Cookie"]

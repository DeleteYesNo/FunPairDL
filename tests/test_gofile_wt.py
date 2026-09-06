"""GoFile's X-Website-Token generation and the request contract that uses it.

GoFile retired the static `?wt=` query parameter for a header carrying
`generateWT(accountToken)`, computed by an obfuscated bundle. Requests on the
old contract come back as HTTP 401 `error-notPremium` — a name that reads like
a paywall but is really a rejected token, which is what made this fail
silently for so long.

Everything here runs offline: the bundle is stubbed with a stand-in that has
the same observable shape, so the tests exercise our plumbing rather than
GoFile's algorithm (and never touch the network — repeated 401s get the IP
temporarily blocked).
"""
import asyncio

import pytest

from funpairdl.providers import gofile_wt
from funpairdl.providers.gofile import (
    CONTENTS_PARAMS, _describe_error, _is_auth_error, api_headers,
)

# Same observable contract as GoFile's bundle: defines generateWT, mixes in
# navigator fields, returns a hex digest.
FAKE_BUNDLE = """
function generateWT(token) {
    var s = token + '|' + navigator.userAgent + '|' + navigator.language;
    var h = 0;
    for (var i = 0; i < s.length; i++) { h = ((h << 5) - h + s.charCodeAt(i)) | 0; }
    return ('00000000' + (h >>> 0).toString(16)).slice(-8);
}
"""


@pytest.fixture(autouse=True)
def _clean_caches():
    gofile_wt.invalidate()
    yield
    gofile_wt.invalidate()


def test_compute_token_runs_the_bundle():
    got = gofile_wt.compute_token(FAKE_BUNDLE, "tok", "UA-A", "en-US")
    assert got and got != "undefined"
    # deterministic for the same inputs
    assert got == gofile_wt.compute_token(FAKE_BUNDLE, "tok", "UA-A", "en-US")


def test_token_depends_on_account_user_agent_and_language():
    """All three feed GoFile's real hash. If we ever stopped passing one, the
    server would recompute a different token and reject every request."""
    base = gofile_wt.compute_token(FAKE_BUNDLE, "tok", "UA-A", "en-US")
    assert gofile_wt.compute_token(FAKE_BUNDLE, "other", "UA-A", "en-US") != base
    assert gofile_wt.compute_token(FAKE_BUNDLE, "tok", "UA-B", "en-US") != base
    assert gofile_wt.compute_token(FAKE_BUNDLE, "tok", "UA-A", "fr-FR") != base


def test_bundle_url_is_read_from_the_home_page():
    # 2026-08: the bundle moved from /dist/js/wt.obf.js to /js/wt.obf.js and
    # every request 404'd until the path was updated by hand.
    html = '<script src="/js/wt.obf.js"></script><script src="/js/app.js"></script>'
    assert gofile_wt.bundle_url_from_html(html) == "https://gofile.io/js/wt.obf.js"
    assert gofile_wt.bundle_url_from_html('<script src="https://cdn.gofile.io/x/wt.obf.js">') \
        == "https://cdn.gofile.io/x/wt.obf.js"
    assert gofile_wt.bundle_url_from_html('<script src="/js/app.js"></script>') == ""
    assert gofile_wt.bundle_url_from_html("") == ""
    assert gofile_wt.WT_JS_CANDIDATES[0] == "https://gofile.io/js/wt.obf.js"


def test_fetch_js_falls_back_across_candidates_and_discovers_a_moved_bundle():
    """A 404 at every known path must not be fatal: the home page names the
    live bundle and that is fetched instead."""

    class _Resp:
        def __init__(self, status, text):
            self.status, self._text = status, text
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        def raise_for_status(self):
            if self.status >= 400:
                raise RuntimeError(f"HTTP {self.status}")
        async def text(self):
            return self._text

    calls = []

    class _Session:
        def get(self, url, **kw):
            calls.append(url)
            if url == gofile_wt.GOFILE_HOME_URL:
                return _Resp(200, '<script src="/new/place/wt.obf.js"></script>')
            if url == "https://gofile.io/new/place/wt.obf.js":
                return _Resp(200, FAKE_BUNDLE)
            return _Resp(404, "")

    src = asyncio.run(gofile_wt._fetch_js(_Session(), "UA"))
    assert "generateWT" in src
    assert calls == [*gofile_wt.WT_JS_CANDIDATES, gofile_wt.GOFILE_HOME_URL,
                     "https://gofile.io/new/place/wt.obf.js"]
    # Cached: a second call makes no request.
    asyncio.run(gofile_wt._fetch_js(_Session(), "UA"))
    assert len(calls) == 4


def test_bundle_without_generatewt_is_reported_clearly():
    with pytest.raises(RuntimeError, match="generateWT"):
        gofile_wt.compute_token("var x = 1;", "tok", "UA", "en-US")


def test_broken_bundle_raises_rather_than_returning_junk():
    with pytest.raises(RuntimeError):
        gofile_wt.compute_token("function generateWT( {{{", "tok", "UA", "en-US")


def test_user_agent_with_quotes_does_not_break_the_bootstrap():
    """The UA is interpolated into JS source; a naive f-string would let a
    quote escape the literal and take the bundle down with it."""
    got = gofile_wt.compute_token(FAKE_BUNDLE, "tok", 'UA "x" \\ \'y\'', "en-US")
    assert got and got != "undefined"


# ── 4-hour rotation ───────────────────────────────────────────────────
def test_window_is_four_hours_aligned_to_the_epoch():
    w = gofile_wt.WT_WINDOW_SECONDS
    assert w == 4 * 3600
    assert gofile_wt.current_window(0) == 0
    assert gofile_wt.current_window(w - 1) == 0
    assert gofile_wt.current_window(w) == 1
    assert gofile_wt.current_window(w + 1) == 1


class _FakeResp:
    status = 200  # real aiohttp responses carry one; _fetch_js reads it for 404s

    def __init__(self, text):
        self._text = text

    async def text(self):
        return self._text

    def raise_for_status(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Counts bundle fetches so caching can be asserted."""
    def __init__(self, body=FAKE_BUNDLE):
        self.body = body
        self.fetches = 0

    def get(self, url, **kw):
        self.fetches += 1
        return _FakeResp(self.body)


def test_token_is_cached_within_a_window_and_refetched_after_invalidate():
    s = _FakeSession()
    a = asyncio.run(gofile_wt.website_token(s, "tok", "UA-A"))
    b = asyncio.run(gofile_wt.website_token(s, "tok", "UA-A"))
    assert a == b
    assert s.fetches == 1, "bundle should be fetched once, then cached"

    gofile_wt.invalidate()
    asyncio.run(gofile_wt.website_token(s, "tok", "UA-A"))
    assert s.fetches == 2, "invalidate() must force a refetch so a rotated salt is picked up"


def test_cache_is_keyed_per_account_and_user_agent():
    s = _FakeSession()
    t1 = asyncio.run(gofile_wt.website_token(s, "tok1", "UA-A"))
    t2 = asyncio.run(gofile_wt.website_token(s, "tok2", "UA-A"))
    t3 = asyncio.run(gofile_wt.website_token(s, "tok1", "UA-B"))
    assert len({t1, t2, t3}) == 3


def test_cached_token_expires_with_its_window(monkeypatch):
    """A token outlives its 4h window on the server, so the cache must not
    hand back the previous window's value."""
    s = _FakeSession()
    monkeypatch.setattr(gofile_wt, "current_window", lambda now=None: 100)
    asyncio.run(gofile_wt.website_token(s, "tok", "UA-A"))
    assert ("tok", "UA-A", "en-US", 100) in gofile_wt._token_cache

    monkeypatch.setattr(gofile_wt, "current_window", lambda now=None: 101)
    asyncio.run(gofile_wt.website_token(s, "tok", "UA-A"))
    # Recomputed under the new window's key rather than served from the old one.
    assert ("tok", "UA-A", "en-US", 101) in gofile_wt._token_cache


def test_bundle_missing_generatewt_fails_the_fetch():
    with pytest.raises(RuntimeError, match="generateWT"):
        asyncio.run(gofile_wt.website_token(_FakeSession("var nope = 1;"), "tok", "UA"))


# ── request contract ──────────────────────────────────────────────────
def test_headers_carry_the_website_token_and_matching_user_agent():
    from funpairdl.constants import BROWSER_USER_AGENT

    h = api_headers("acct-token", "wt-value")
    assert h["Authorization"] == "Bearer acct-token"
    # The header name is what changed; ?wt= is no longer accepted.
    assert h["X-Website-Token"] == "wt-value"
    assert h["X-BL"] == "en-US"
    # The UA is hashed into the token, so it must be the one we send.
    assert h["User-Agent"] == BROWSER_USER_AGENT


def test_contents_params_request_a_full_page():
    # Without pageSize, folders come back truncated and files go missing.
    assert CONTENTS_PARAMS["pageSize"] == "1000"
    assert "contentFilter" in CONTENTS_PARAMS


@pytest.mark.parametrize("status,api_status", [
    (401, None),
    (403, None),
    (401, "error-notPremium"),
    (200, "error-wrongToken"),
])
def test_auth_errors_trigger_a_token_refresh(status, api_status):
    assert _is_auth_error(status, api_status) is True


def test_not_premium_is_treated_as_a_token_problem():
    """It is the symptom of a stale website token, so it must retry with a
    fresh one rather than be reported as a paywall."""
    assert _is_auth_error(401, "error-notPremium") is True
    assert "website-token" in _describe_error(401, "error-notPremium")


@pytest.mark.parametrize("api_status,expected", [
    ("error-rateLimit", "rate-limited"),
    ("error-notFound", "not found"),
])
def test_error_messages_are_actionable(api_status, expected):
    assert expected in _describe_error(403, api_status)


def test_rate_limit_is_not_mistaken_for_an_auth_failure():
    # Retrying immediately on a rate limit is what escalates to an IP block.
    assert _is_auth_error(429, "error-rateLimit") is False

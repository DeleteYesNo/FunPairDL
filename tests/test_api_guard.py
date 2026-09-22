"""Tests for the local API's client guard — no web page may call it."""
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from funpairdl.api.server import _LOOPBACK_HOSTS, create_app, refusal_reason

LOCAL = "http://127.0.0.1:9172"


def _client(host: str = "127.0.0.1"):
    qm = MagicMock()
    qm.pairs = []
    return TestClient(create_app(qm, host), base_url=LOCAL), qm


class TestRefusalReason:
    def test_local_client_without_origin_passes(self):
        # The bridge's aiohttp session and curl send neither Origin nor
        # Sec-Fetch-*.
        assert refusal_reason({"host": "127.0.0.1:9172"}, _LOOPBACK_HOSTS) is None
        assert refusal_reason({"host": "localhost:9172"}, _LOOPBACK_HOSTS) is None
        assert refusal_reason({"host": "[::1]:9172"}, _LOOPBACK_HOSTS) is None

    def test_rebound_or_missing_host_refused(self):
        assert refusal_reason({"host": "rebind.evil.example:9172"}, _LOOPBACK_HOSTS)
        assert refusal_reason({"host": "127.0.0.1.evil.example"}, _LOOPBACK_HOSTS)
        assert refusal_reason({}, _LOOPBACK_HOSTS)

    def test_web_origins_refused(self):
        for origin in ("https://evil.example", "http://127.0.0.1:8080",
                       "https://discuss.eroscripts.com", "null"):
            assert refusal_reason(
                {"host": "127.0.0.1:9172", "origin": origin}, _LOOPBACK_HOSTS
            ), origin

    def test_extension_origin_passes(self):
        # The retired Chrome extension's background/popup.
        assert refusal_reason(
            {"host": "127.0.0.1:9172", "origin": "chrome-extension://abcdefgh"},
            _LOOPBACK_HOSTS,
        ) is None

    def test_cross_site_fetch_metadata_refused(self):
        # <img>/<script> GETs carry no Origin but do carry Sec-Fetch-Site.
        base = {"host": "127.0.0.1:9172"}
        assert refusal_reason({**base, "sec-fetch-site": "cross-site"}, _LOOPBACK_HOSTS)
        assert refusal_reason({**base, "sec-fetch-site": "same-site"}, _LOOPBACK_HOSTS)
        # Typed into the address bar.
        assert refusal_reason({**base, "sec-fetch-site": "none"}, _LOOPBACK_HOSTS) is None


class TestGuardedApp:
    def test_local_request_served(self):
        client, _ = _client()
        r = client.get("/api/status")
        assert r.status_code == 200
        assert "access-control-allow-origin" not in r.headers

    def test_web_page_cannot_read_config(self):
        client, _ = _client()
        r = client.get("/api/config", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        assert "gofile_token" not in r.text
        assert "access-control-allow-origin" not in r.headers

    def test_preflight_gets_no_cors_grant(self):
        client, _ = _client()
        r = client.options("/api/resolve", headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        })
        assert r.status_code == 403
        assert "access-control-allow-origin" not in r.headers

    def test_simple_post_side_effect_blocked(self):
        # A no-cors POST needs no preflight; the handler must not run.
        client, qm = _client()
        r = client.post("/api/pair/abc/remove", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        qm.remove_pair.assert_not_called()

    def test_dns_rebinding_blocked(self):
        client, _ = _client()
        r = client.get("/api/status", headers={"Host": "rebind.evil.example:9172"})
        assert r.status_code == 403

    def test_configured_lan_host_allowed(self):
        client, _ = _client("192.168.1.50")
        r = client.get("/api/status", headers={"Host": "192.168.1.50:9172"})
        assert r.status_code == 200

    def test_wildcard_bind_does_not_open_every_host(self):
        client, _ = _client("0.0.0.0")
        r = client.get("/api/status", headers={"Host": "rebind.evil.example:9172"})
        assert r.status_code == 403

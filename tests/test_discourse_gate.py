"""utils/discourse: every forum request waits its turn, and a 429 waits out
Retry-After instead of failing."""
import asyncio
import time

import pytest

from funpairdl.utils import discourse


@pytest.fixture(autouse=True)
def _fresh_gate(monkeypatch):
    monkeypatch.setattr(discourse, "_next_at", 0.0)
    monkeypatch.setattr(discourse, "_next_search_at", 0.0)
    monkeypatch.setattr(discourse, "_paused_until", 0.0)
    monkeypatch.setattr(discourse, "MIN_INTERVAL_S", 1.0)   # the backfill CLI may have raised it
    monkeypatch.setattr(discourse, "_headers", lambda: ({}, "", None))


def test_retry_after_forms():
    assert discourse.retry_after_seconds("7") == 7
    assert discourse.retry_after_seconds(None) == discourse.RETRY_AFTER_DEFAULT_S
    assert discourse.retry_after_seconds("100000") == discourse.RETRY_AFTER_MAX_S
    assert discourse.retry_after_seconds("0") == 1
    now = 1_800_000_000
    http_date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(now + 20))
    assert abs(discourse.retry_after_seconds(http_date, now=now) - 20) < 1.01


def test_slots_are_spaced_and_searches_wider(monkeypatch):
    monkeypatch.setattr(discourse.random, "uniform", lambda a, b: 0.0)
    assert discourse._reserve_slot(False) <= 0
    assert 0.9 < discourse._reserve_slot(False) <= 1.0
    # a search waits for the plain gap, and the next search for the search gap
    first_search = discourse._reserve_slot(True)
    assert 1.9 < first_search <= 2.0
    second_search = discourse._reserve_slot(True)
    assert second_search - first_search >= discourse.SEARCH_INTERVAL_S - 0.01


class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status, self._body, self.headers = status, body or {}, headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._body

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(self.status)


class _Session:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def get(self, url, **kw):
        self.calls += 1
        return self.replies.pop(0)


def test_a_429_pauses_then_retries(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(discourse.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(discourse, "MIN_INTERVAL_S", 0.0)
    session = _Session([_Resp(429, headers={"Retry-After": "5"}), _Resp(200, {"ok": 1})])
    got = asyncio.run(discourse._get_json(session, "https://forum.invalid/t/1.json"))
    assert got == {"ok": 1} and session.calls == 2
    assert any(4.5 <= s <= 5.0 for s in slept)


def test_429_every_time_gives_up(monkeypatch):
    async def fake_sleep(s):
        return None

    monkeypatch.setattr(discourse.asyncio, "sleep", fake_sleep)
    session = _Session([_Resp(429, headers={"Retry-After": "1"}) for _ in range(discourse.RETRY_429 + 1)])
    with pytest.raises(discourse.RateLimited):
        asyncio.run(discourse._get_json(session, "https://forum.invalid/t/1.json"))
    assert session.calls == discourse.RETRY_429 + 1


def test_missing_topic_is_none(monkeypatch):
    session = _Session([_Resp(404)])
    assert asyncio.run(discourse._get_json(session, "https://forum.invalid/t/1.json")) is None

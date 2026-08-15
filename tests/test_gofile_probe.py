"""The /probe branch must size both shapes a GoFile /d/ link can return.

A /d/ link points either at a folder (metadata in `children`) or straight at
a single file (metadata at the top level, no children). Reading only
`children` reported "0 files" and size 0 for the single-file case, so the UI
showed no size — the failure mode the provider checklist warns about, and one
that looks like a slow host rather than a bug.
"""
import asyncio
import json

import pytest

from funpairdl.providers.probe import _meta_from_info, _probe_gofile

FAKE_BUNDLE = "function generateWT(t) { return 'wt-' + t; }"

SINGLE_FILE = {
    "status": "ok",
    "data": {
        "id": "deadbeef", "type": "file",
        "name": "artistdemo sample.mp4", "size": 1710000000,
        "link": "https://store1.gofile.io/download/web/deadbeef/x.mp4",
    },
}

FOLDER = {
    "status": "ok",
    "data": {
        "id": "c5a122a4", "type": "folder",
        "name": "a folder",
        "children": {
            "k1": {"id": "k1", "type": "file", "name": "video.mp4", "size": 669361049},
            "k2": {"id": "k2", "type": "file", "name": "script.funscript", "size": 152838},
            "k3": {"id": "k3", "type": "folder", "name": "nested"},
        },
    },
}

EMPTY_FOLDER = {"status": "ok", "data": {"id": "x", "type": "folder", "children": {}}}
NOT_PREMIUM = {"status": "error-notPremium", "data": {}}


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def json(self):
        if isinstance(self._payload, str):
            return json.loads(self._payload)
        return self._payload

    async def text(self):
        if isinstance(self._payload, str):
            return self._payload
        return json.dumps(self._payload)


class _Session:
    def __init__(self, contents_payload, status=200):
        self._contents = contents_payload
        self._status = status

    def post(self, url, **kw):
        return _Resp({"data": {"token": "guest"}})

    def get(self, url, **kw):
        if "/contents/" in url:
            return _Resp(self._contents, status=self._status)
        return _Resp(FAKE_BUNDLE)  # wt.obf.js


class _Settings:
    gofile_token = "paid-token"


def _probe(payload, status=200):
    return asyncio.run(
        _probe_gofile("https://gofile.io/d/abc123", _Settings(), _Session(payload, status))
    )


def test_single_file_link_reports_its_size():
    info = _probe(SINGLE_FILE)
    assert info["success"] is True
    assert info["size"] == 1710000000
    assert info["filename"] == "artistdemo sample.mp4"
    # and the UI-facing derivation agrees
    assert _meta_from_info(info).size == 1710000000


def test_folder_link_sums_only_its_files():
    info = _probe(FOLDER)
    assert info["success"] is True
    # nested folders must not be counted as files
    assert info["size"] == 669361049 + 152838
    assert info["filename"] == "2 files"
    assert {f["name"] for f in info["files"]} == {"video.mp4", "script.funscript"}


def test_empty_folder_is_not_an_error():
    info = _probe(EMPTY_FOLDER)
    assert info["success"] is True
    assert info["size"] == 0
    assert info["files"] is None


def test_rejected_request_surfaces_the_real_reason():
    """A bare 'Status 401' sent the last investigation chasing the account
    tier; the body says what actually happened."""
    info = _probe(NOT_PREMIUM, status=401)
    assert info["success"] is False
    assert "website-token" in info["error"]


@pytest.mark.parametrize("payload", [SINGLE_FILE, FOLDER])
def test_probe_and_resolve_agree_on_the_shape_split(payload):
    """resolve() treats `type == file` or a top-level `link` as a single file;
    probe must use the same rule or the two disagree about what a link is."""
    data = payload["data"]
    is_single = data.get("type") == "file" or "link" in data
    info = _probe(payload)
    assert (len(info["files"] or []) == 1 and is_single) or not is_single

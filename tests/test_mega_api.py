"""Pure-function tests for the MEGA API helpers.

Covers URL parsing, the folder-file node-key derivation (the audit-[1f] fix:
node key, not folder master key), attribute decryption, and the TTL cache
used for sid validation / folder listings. No network involved.
"""
import threading

import pytest
from Crypto.Cipher import AES

import funpairdl.utils.mega_api as mega_api
from funpairdl.utils.mega_api import (
    _a32_to_base64,
    _a32_to_bytes,
    _aes_cbc_encrypt_a32,
    _decrypt_attr,
    _file_key,
    _folder_file_key_iv,
    parse_mega_url,
)

# ─── Synthetic key material ───

FOLDER_KEY = (0x11111111, 0x22222222, 0x33333333, 0x44444444)
# 8-int full file key: first 4 XOR'd with last 4 give the AES key;
# ints 4-5 are the CTR iv.
FULL_KEY = (
    0xA1A2A3A4, 0xB1B2B3B4, 0xC1C2C3C4, 0xD1D2D3D4,
    0x01020304, 0x05060708, 0x090A0B0C, 0x0D0E0F10,
)
SHARE_ROOT = "rootHndl"


def _encrypt_key(key: tuple, master: tuple) -> tuple:
    """Inverse of mega_api._decrypt_key — encrypt in 4-int blocks."""
    out = ()
    for i in range(0, len(key), 4):
        out += _aes_cbc_encrypt_a32(key[i:i + 4], master)
    return out


def _node(k_field: str, **extra) -> dict:
    node = {"h": "fileHndl", "t": 0, "k": k_field, "s": 12345}
    node.update(extra)
    return node


def _encrypt_attrs(name: str, key: tuple) -> bytes:
    """Build an encrypted MEGA attribute blob containing filename ``name``."""
    raw = ('MEGA{"n":%s}' % __import__("json").dumps(name)).encode("utf-8")
    padded = raw + b"\0" * (-len(raw) % 16)
    return AES.new(_a32_to_bytes(key), AES.MODE_CBC, b"\0" * 16).encrypt(padded)


# ─── Folder-file key derivation (audit [1f]) ───


def test_folder_file_key_iv_roundtrip():
    enc_b64 = _a32_to_base64(_encrypt_key(FULL_KEY, FOLDER_KEY))
    node = _node(f"{SHARE_ROOT}:{enc_b64}")

    k, iv = _folder_file_key_iv(node, FOLDER_KEY, SHARE_ROOT)

    assert k == _file_key(FULL_KEY)
    assert iv == (FULL_KEY[4], FULL_KEY[5])


def test_folder_file_key_iv_picks_share_root_pair():
    # Multi-share node: "h1:k1/h2:k2" — must select the share-root pair,
    # not blindly take the first one.
    good = _a32_to_base64(_encrypt_key(FULL_KEY, FOLDER_KEY))
    bogus = _a32_to_base64(_encrypt_key(FULL_KEY, (9, 9, 9, 9)))
    node = _node(f"otherHndl:{bogus}/{SHARE_ROOT}:{good}")

    k, iv = _folder_file_key_iv(node, FOLDER_KEY, SHARE_ROOT)

    assert k == _file_key(FULL_KEY)
    assert iv == (FULL_KEY[4], FULL_KEY[5])


def test_folder_file_key_iv_folder_node_raises_runtime_error():
    # A folder node carries a 4-int key. The old probe path fed the 4-int
    # folder master key straight into _file_key, raising a swallowed
    # IndexError; the helper must instead raise a clear RuntimeError.
    enc_b64 = _a32_to_base64(_encrypt_key(FOLDER_KEY, FOLDER_KEY))
    node = _node(f"{SHARE_ROOT}:{enc_b64}", t=1)

    with pytest.raises(RuntimeError, match="expected 8"):
        _folder_file_key_iv(node, FOLDER_KEY, SHARE_ROOT)


def test_folder_file_key_iv_missing_key_raises():
    with pytest.raises(RuntimeError, match="Missing encrypted key"):
        _folder_file_key_iv(_node(""), FOLDER_KEY, SHARE_ROOT)


def test_master_key_is_not_a_file_key():
    # Regression guard for the original bug: the folder master key (4 ints)
    # must never be usable where an 8-int full key is required.
    with pytest.raises(IndexError):
        _file_key(FOLDER_KEY)


# ─── Attribute decryption ───


def test_decrypt_attr_roundtrip_via_derived_key():
    # End-to-end: derive the node key, then decrypt attrs with it — exactly
    # what _probe_folder_file does.
    enc_b64 = _a32_to_base64(_encrypt_key(FULL_KEY, FOLDER_KEY))
    node = _node(f"{SHARE_ROOT}:{enc_b64}")
    k, _iv = _folder_file_key_iv(node, FOLDER_KEY, SHARE_ROOT)

    attrs = _decrypt_attr(_encrypt_attrs("video [1080p] final.mp4", k), k)

    assert attrs is not None
    assert attrs["n"] == "video [1080p] final.mp4"


def test_decrypt_attr_wrong_key_returns_none():
    k = _file_key(FULL_KEY)
    wrong = (1, 2, 3, 4)
    assert _decrypt_attr(_encrypt_attrs("x.mp4", k), wrong) is None


# ─── URL parsing ───


def test_parse_mega_url_v2_file():
    assert parse_mega_url("https://mega.nz/file/AbCd1234#key_-part") == {
        "type": "file", "handle": "AbCd1234", "key": "key_-part",
    }


def test_parse_mega_url_v2_folder():
    assert parse_mega_url("https://mega.nz/folder/FoLdEr12#fkey") == {
        "type": "folder", "handle": "FoLdEr12", "key": "fkey",
    }


def test_parse_mega_url_folder_file():
    assert parse_mega_url(
        "https://mega.nz/folder/FoLdEr12#fkey/file/FiLe4567"
    ) == {
        "type": "folder_file",
        "folder_handle": "FoLdEr12",
        "folder_key": "fkey",
        "file_handle": "FiLe4567",
    }


def test_parse_mega_url_v1_file():
    # "#!HANDLE!KEY" splits to ["", "HANDLE", "KEY"] — the empty first part
    # must be skipped (was a latent bug returning handle="").
    assert parse_mega_url("https://mega.nz/#!HaNdLe12!theKey") == {
        "type": "file", "handle": "HaNdLe12", "key": "theKey",
    }


def test_parse_mega_url_v1_folder():
    assert parse_mega_url("https://mega.nz/#F!HaNdLe12!theKey") == {
        "type": "folder", "handle": "HaNdLe12", "key": "theKey",
    }


def test_parse_mega_url_invalid():
    assert parse_mega_url("https://example.com/file/x#y") is not None  # host-agnostic parser
    assert parse_mega_url("https://mega.nz/") is None
    assert parse_mega_url("not a url") is None


# ─── TTL cache (sid validation / folder listings) ───


class _FakeTime:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


def test_ttl_cache_get_put_and_expiry(monkeypatch):
    ft = _FakeTime()
    monkeypatch.setattr(mega_api, "time", ft)
    cache, lock = {}, threading.Lock()

    mega_api._cache_put(cache, lock, "a", {"v": 1}, max_entries=4)
    assert mega_api._cache_get(cache, lock, "a") == {"v": 1}

    ft.now += mega_api._MEGA_CACHE_TTL - 1
    assert mega_api._cache_get(cache, lock, "a") == {"v": 1}

    ft.now += 2  # past TTL
    assert mega_api._cache_get(cache, lock, "a") is None
    assert "a" not in cache  # expired entry removed


# ─── _stream_decrypt (synthetic, no network) ───


class _FakeResp:
    """Mimics aiohttp response streaming for a Range request."""

    def __init__(self, data: bytes, status: int = 206):
        self._data = data
        self.status = status
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def iter_chunked(self, n: int):
        async def gen():
            for i in range(0, len(self._data), n):
                yield self._data[i:i + n]
        return gen()


class _FakeSession:
    def __init__(self, ciphertext: bytes, fail_at_start: int | None = None):
        self._ct = ciphertext
        self._fail_at_start = fail_at_start

    def get(self, url, headers=None, timeout=None):
        rng = (headers or {}).get("Range", "bytes=0-")
        a, _, b = rng.split("=")[1].partition("-")
        start = int(a)
        end = int(b) if b else len(self._ct) - 1
        if self._fail_at_start is not None and start == self._fail_at_start:
            raise RuntimeError("boom: connection refused")
        return _FakeResp(self._ct[start:end + 1])


def _make_cipher_fixture(size: int):
    from Crypto.Cipher import AES as _AES
    from Crypto.Util import Counter as _Counter

    k = _file_key(FULL_KEY)
    iv = (FULL_KEY[4], FULL_KEY[5])
    key_bytes = _a32_to_bytes(k)
    base_ctr = int.from_bytes(_a32_to_bytes(iv) + b"\0" * 8, "big")
    plaintext = bytes((i * 7 + 13) % 256 for i in range(size))
    ct = _AES.new(
        key_bytes, _AES.MODE_CTR,
        counter=_Counter.new(128, initial_value=base_ctr),
    ).encrypt(plaintext)
    return k, iv, plaintext, ct


def test_stream_decrypt_segmented_roundtrip(tmp_path):
    import asyncio

    # >256 KB (segmented path) and 16-byte-UNaligned total, so the final
    # tail flush is exercised.
    size = 512 * 1024 + 37
    k, iv, plaintext, ct = _make_cipher_fixture(size)

    seen = []
    out = asyncio.run(mega_api._stream_decrypt(
        _FakeSession(ct), "http://fake/dl", tmp_path, "out.bin", size,
        k, iv, lambda done, total: seen.append(done), 65536, max_segments=4,
    ))

    assert out.read_bytes() == plaintext
    assert max(seen) == size  # progress is exact, never overcounts


def test_stream_decrypt_failure_cancels_and_raises(tmp_path, monkeypatch):
    import asyncio

    size = 512 * 1024
    k, iv, _plaintext, ct = _make_cipher_fixture(size)
    # No retry sleeps — the first segment failure must propagate promptly
    # after siblings are cancelled and reaped.
    monkeypatch.setattr(mega_api, "_MEGA_SEGMENT_MAX_RETRIES", 1)

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(mega_api._stream_decrypt(
            _FakeSession(ct, fail_at_start=0), "http://fake/dl", tmp_path,
            "out.bin", size, k, iv, None, 65536, max_segments=4,
        ))


def test_ttl_cache_evicts_oldest_when_full(monkeypatch):
    ft = _FakeTime()
    monkeypatch.setattr(mega_api, "time", ft)
    cache, lock = {}, threading.Lock()

    mega_api._cache_put(cache, lock, "a", 1, max_entries=2)
    ft.now += 1
    mega_api._cache_put(cache, lock, "b", 2, max_entries=2)
    ft.now += 1
    mega_api._cache_put(cache, lock, "c", 3, max_entries=2)

    assert mega_api._cache_get(cache, lock, "a") is None  # oldest evicted
    assert mega_api._cache_get(cache, lock, "b") == 2
    assert mega_api._cache_get(cache, lock, "c") == 3


# ─── Share-root selection & self-authenticating key choice ───
#
# Regression suite for the nested-share bug: a folder exported from inside
# other shares of the same owner carries one handle:enckey pair PER ancestor
# share on every node, the k-prefix frequency counts tie exactly, and the old
# most-common heuristic picked the outermost ancestor — whose key the URL's
# folder key cannot decrypt. Names fell back to file_<handle> AND downloads
# decrypted to garbage with the same wrong key.

ANC = "ancHndl0"     # outer ancestor share (the wrong pick)
ROOT = "rootHnd1"    # the export root — the listing's structural root node


def _b64url(raw: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _nested_share_nodes() -> list[dict]:
    """Two files + the root folder node, every one keyed for BOTH shares
    (counts tie), with the ancestor pair listed first — the exact layout of
    the real-world broken folders."""
    good8 = _a32_to_base64(_encrypt_key(FULL_KEY, FOLDER_KEY))
    bogus8 = _a32_to_base64(_encrypt_key(FULL_KEY, (9, 9, 9, 9)))
    enc4 = _a32_to_base64(_encrypt_key(FOLDER_KEY, FOLDER_KEY))
    bogus4 = _a32_to_base64(_encrypt_key(FOLDER_KEY, (9, 9, 9, 9)))
    file_key = _file_key(FULL_KEY)
    return [
        {"h": ROOT, "p": "notInLst", "t": 1,
         "k": f"{ANC}:{bogus4}/{ROOT}:{enc4}"},
        {"h": "fileAAAA", "p": ROOT, "t": 0, "s": 111,
         "k": f"{ANC}:{bogus8}/{ROOT}:{good8}",
         "a": _b64url(_encrypt_attrs("Sample Extended.mp4", file_key))},
        {"h": "fileBBBB", "p": ROOT, "t": 0, "s": 222,
         "k": f"{ANC}:{bogus8}/{ROOT}:{good8}",
         "a": _b64url(_encrypt_attrs("Sample Extended.funscript", file_key))},
    ]


def test_share_root_handle_prefers_structural_root_over_frequency():
    nodes = _nested_share_nodes()
    # Frequency ties (ANC and ROOT appear in every node's k) — the structural
    # root (parent absent from the listing) must win, not insertion order.
    assert mega_api._share_root_handle(nodes) == ROOT


def test_share_root_handle_empty_listing():
    assert mega_api._share_root_handle([]) == ""


def test_folder_file_key_iv_attrs_self_authenticates_on_wrong_root_guess():
    # Even with a WRONG share_root guess the attribute magic identifies the
    # right pair — and the same validated key is returned for content.
    node = _nested_share_nodes()[1]
    k, iv, attrs = mega_api._folder_file_key_iv_attrs(node, FOLDER_KEY, ANC)
    assert k == _file_key(FULL_KEY)
    assert iv == (FULL_KEY[4], FULL_KEY[5])
    assert attrs["n"] == "Sample Extended.mp4"


def test_folder_file_key_iv_attrs_raises_when_attr_never_decrypts():
    # Attribute present but no key pair decrypts it -> refuse instead of
    # silently downloading garbage under an unvalidated key.
    node = _nested_share_nodes()[1]
    node["a"] = _b64url(b"\x12\x34" * 16)
    with pytest.raises(RuntimeError, match="does not decrypt"):
        mega_api._folder_file_key_iv_attrs(node, FOLDER_KEY, ROOT)


def test_folder_file_key_iv_attrs_without_attr_keeps_share_root_pair():
    node = _nested_share_nodes()[1]
    del node["a"]
    k, iv, attrs = mega_api._folder_file_key_iv_attrs(node, FOLDER_KEY, ROOT)
    assert k == _file_key(FULL_KEY)
    assert attrs is None


def _run_probe_with_nodes(monkeypatch, nodes, url_key: tuple):
    import asyncio

    async def fake_fetch(session, folder_handle, **kwargs):
        return {"f": nodes}

    monkeypatch.setattr(mega_api, "_fetch_folder_nodes", fake_fetch)
    url = f"https://mega.nz/folder/testFldr#{_a32_to_base64(url_key)}"
    return asyncio.run(mega_api.probe_mega_folder(url))


def test_probe_mega_folder_recovers_names_on_nested_share_tie(monkeypatch):
    result = _run_probe_with_nodes(monkeypatch, _nested_share_nodes(), FOLDER_KEY)
    assert result["success"] is True
    names = [f["name"] for f in result["files"]]
    assert names == ["Sample Extended.mp4", "Sample Extended.funscript"]
    assert not any(n.startswith("file_") for n in names)


def test_probe_mega_folder_fails_loudly_on_key_mismatch(monkeypatch):
    # A folder key that decrypts NOTHING must fail the probe instead of
    # returning a listing of file_<handle> placeholders (which cascades into
    # mis-typed, mis-paired queue items downstream).
    result = _run_probe_with_nodes(monkeypatch, _nested_share_nodes(), (5, 5, 5, 5))
    assert result["success"] is False
    assert "mismatch" in result["error"]


def test_folder_file_key_iv_attrs_malformed_attr_raises_runtime_error():
    # A present-but-undecodable attribute blob (wrong length / bad base64)
    # must surface as the uniform RuntimeError, not a stray ValueError from
    # the AES layer — and must never return an unvalidated key.
    node = _nested_share_nodes()[1]
    node["a"] = _b64url(b"12345")  # 5 bytes: not block-aligned
    with pytest.raises(RuntimeError, match="does not decrypt"):
        mega_api._folder_file_key_iv_attrs(node, FOLDER_KEY, ROOT)

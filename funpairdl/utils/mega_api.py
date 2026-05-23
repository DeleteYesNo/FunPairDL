"""Lightweight MEGA API client for probing and downloading files.

Uses MEGA's public API + crypto from pycryptodome.
Does NOT require mega.py (which is broken on Python 3.11+).
"""
from __future__ import annotations

import base64
import json
import logging
import re
import struct
from pathlib import Path
from urllib.parse import urlparse

from Crypto.Cipher import AES
from Crypto.Util import Counter

logger = logging.getLogger("funpairdl.utils.mega_api")

MEGA_API = "https://g.api.mega.co.nz/cs"

# MEGA error codes
_MEGA_EAGAIN = -3        # Temporary server overload
_MEGA_EOVERQUOTA = -18   # Transfer quota exceeded (bandwidth limit)
_MEGA_ETOOMANY = -17     # Too many concurrent connections/requests
_MEGA_EACCESS = -11      # Access denied (expired session, bad sid)
_MEGA_EEXPIRED = -15     # Session expired
_MEGA_ETEMPUNAVAIL = -6  # Temporarily unavailable

# Error codes that warrant retry (with backoff)
_MEGA_RETRY_CODES = {_MEGA_EAGAIN, _MEGA_ETOOMANY, _MEGA_ETEMPUNAVAIL}
# Error codes that indicate quota exceeded — retry with longer delay
_MEGA_QUOTA_CODES = {_MEGA_EOVERQUOTA}
# Error codes that indicate auth/session issues
_MEGA_AUTH_ERROR_CODES = {_MEGA_EACCESS, _MEGA_EEXPIRED}

_MEGA_ERROR_NAMES = {
    -3: "EAGAIN (temporary overload)",
    -6: "ETEMPUNAVAIL (temporarily unavailable)",
    -9: "ENOENT (not found)",
    -11: "EACCESS (access denied / expired session)",
    -15: "EEXPIRED (session expired)",
    -17: "ETOOMANY (too many connections)",
    -18: "EOVERQUOTA (transfer quota exceeded)",
}

_MEGA_MAX_RETRIES = 6
_MEGA_RETRY_BASE_DELAY = 2.0  # seconds
_MEGA_SEGMENT_MAX_RETRIES = 5  # retries per download segment (MEGA drops conns)

# MEGA throttles each connection to ~0.25 MB/s but scales linearly with
# parallel connections to one download URL (measured: 32 conns ≈ 8.6 MB/s,
# 0 errors; resets only appear past ~48). So allow plenty of segments per
# file — the connection budget is bounded instead by running one MEGA file
# at a time (see the semaphore in queue_manager).
_MEGA_MAX_SEGMENTS = 32

# ─── Crypto helpers (ported from mega.py/crypto.py) ───


def _a32_to_bytes(a: tuple | list) -> bytes:
    return struct.pack(">%dI" % len(a), *a)


def _bytes_to_a32(b: bytes) -> tuple:
    if len(b) % 4:
        b += b"\0" * (4 - len(b) % 4)
    return struct.unpack(">%dI" % (len(b) // 4), b)


def _base64_url_decode(data: str) -> bytes:
    data += "=="[(2 - len(data) * 3) % 4 :]
    for s, r in (("-", "+"), ("_", "/"), (",", "")):
        data = data.replace(s, r)
    return base64.b64decode(data)


def _base64_to_a32(s: str) -> tuple:
    return _bytes_to_a32(_base64_url_decode(s))


def _aes_cbc_decrypt(data: bytes, key: bytes) -> bytes:
    return AES.new(key, AES.MODE_CBC, b"\0" * 16).decrypt(data)


def _aes_cbc_encrypt(data: bytes, key: bytes) -> bytes:
    return AES.new(key, AES.MODE_CBC, b"\0" * 16).encrypt(data)


def _aes_cbc_decrypt_a32(data, key):
    return _bytes_to_a32(_aes_cbc_decrypt(_a32_to_bytes(data), _a32_to_bytes(key)))


def _aes_cbc_encrypt_a32(data, key):
    return _bytes_to_a32(_aes_cbc_encrypt(_a32_to_bytes(data), _a32_to_bytes(key)))


def _decrypt_attr(attr_bytes: bytes, key: tuple) -> dict | None:
    """Decrypt MEGA file attributes (contains filename etc.)."""
    decrypted = _aes_cbc_decrypt(attr_bytes, _a32_to_bytes(key))
    try:
        text = decrypted.decode("utf-8", errors="ignore").rstrip("\0")
    except Exception:
        return None
    if text[:6] == 'MEGA{"':
        try:
            return json.loads(text[4:])
        except json.JSONDecodeError:
            # Try finding the end of JSON
            idx = text.find("}", 4)
            if idx > 0:
                try:
                    return json.loads(text[4 : idx + 1])
                except Exception:
                    pass
    return None


def _a32_to_base64(a: tuple) -> str:
    """Convert a32 tuple to MEGA-style base64 string."""
    raw = base64.b64encode(_a32_to_bytes(a)).decode()
    return raw.rstrip("=").replace("+", "-").replace("/", "_")


def _str_to_a32(s: str) -> tuple:
    """Convert string to a32 (pad to 4-byte boundary)."""
    b = s.encode("utf-8")
    if len(b) % 4:
        b += b"\0" * (4 - len(b) % 4)
    return _bytes_to_a32(b)


def _prepare_key(password: str) -> tuple:
    """Derive MEGA password key (65536 rounds AES-CBC)."""
    pw_bytes = password.encode("utf-8")
    pkey = [0x93C467E3, 0x7DB0C7A4, 0xD1BE3F81, 0x0152CB56]
    for _ in range(65536):
        for j in range(0, len(pw_bytes), 16):
            key = [0, 0, 0, 0]
            for i in range(4):
                pos = i * 4 + j
                if pos < len(pw_bytes):
                    key[i] = pw_bytes[pos] << 24
                if pos + 1 < len(pw_bytes):
                    key[i] |= pw_bytes[pos + 1] << 16
                if pos + 2 < len(pw_bytes):
                    key[i] |= pw_bytes[pos + 2] << 8
                if pos + 3 < len(pw_bytes):
                    key[i] |= pw_bytes[pos + 3]
            pkey = list(_aes_cbc_encrypt_a32(pkey, key))
    return tuple(pkey)


def _stringhash(s: str, aes_key: tuple) -> str:
    """Compute MEGA string hash for authentication."""
    s32 = _str_to_a32(s)
    h32 = [0, 0, 0, 0]
    for i in range(len(s32)):
        h32[i % 4] ^= s32[i]
    h32 = tuple(h32)
    for _ in range(16384):
        h32 = _aes_cbc_encrypt_a32(h32, aes_key)
    return _a32_to_base64((h32[0], h32[2]))


def _decrypt_key(encrypted_key: tuple, master_key: tuple) -> tuple:
    """Decrypt a file's key using the folder master key (AES-ECB in 4-int blocks)."""
    result = ()
    for i in range(0, len(encrypted_key), 4):
        result += _aes_cbc_decrypt_a32(encrypted_key[i : i + 4], master_key)
    return result


def _file_key(full_key: tuple) -> tuple:
    """Derive the 4-int AES key from the 8-int full key (XOR halves)."""
    return (
        full_key[0] ^ full_key[4],
        full_key[1] ^ full_key[5],
        full_key[2] ^ full_key[6],
        full_key[3] ^ full_key[7],
    )


# ─── URL parsing ───


def parse_mega_url(url: str) -> dict | None:
    """Parse a MEGA URL into type, handle, and key.

    Returns:
        {"type": "file", "handle": str, "key": str}
        {"type": "folder", "handle": str, "key": str}
        {"type": "folder_file", "folder_handle": str, "folder_key": str, "file_handle": str}
        or None
    """
    parsed = urlparse(url.strip())
    path = parsed.path.strip("/")
    fragment = parsed.fragment

    # V2 URLs: mega.nz/file/HANDLE#KEY or mega.nz/folder/HANDLE#KEY
    if path.startswith("file/") or path.startswith("folder/"):
        parts = path.split("/")
        if len(parts) >= 2 and fragment:
            # Check for folder file URL: mega.nz/folder/HANDLE#KEY/file/FILE_HANDLE
            if parts[0] == "folder" and "/" in fragment:
                frag_parts = fragment.split("/")
                # frag_parts = ["KEY", "file", "FILE_HANDLE"]
                if len(frag_parts) >= 3 and frag_parts[1] == "file":
                    return {
                        "type": "folder_file",
                        "folder_handle": parts[1],
                        "folder_key": frag_parts[0],
                        "file_handle": frag_parts[2],
                    }
            return {
                "type": parts[0],
                "handle": parts[1],
                "key": fragment.split("/")[0] if "/" in fragment else fragment,
            }

    # V1 URLs: mega.nz/#!HANDLE!KEY or mega.nz/#F!HANDLE!KEY
    if fragment and "!" in fragment:
        parts = fragment.split("!")
        if fragment.startswith("F!") and len(parts) >= 3:
            return {"type": "folder", "handle": parts[1], "key": parts[2]}
        if len(parts) >= 2:
            return {"type": "file", "handle": parts[0], "key": parts[1]}

    return None


# ─── API calls ───


async def probe_mega_file(url: str) -> dict:
    """Probe a single MEGA file URL for name and size."""
    import aiohttp

    info = parse_mega_url(url)
    if not info or info["type"] not in ("file", "folder_file"):
        return {"success": False, "error": "Invalid MEGA file URL"}

    # For folder_file URLs, probe the specific file within the folder
    handle = info.get("file_handle") or info.get("handle")
    key = info.get("folder_key") or info.get("key")

    try:
        api_url = MEGA_API
        if info["type"] == "folder_file":
            api_url = f"{MEGA_API}?n={info['folder_handle']}"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                api_url,
                json=[{"a": "g", "p": handle, "ssm": 1}],
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()

        if isinstance(data, list):
            data = data[0]
        if isinstance(data, int):
            return {"success": False, "error": f"MEGA API error: {data}"}

        size = data.get("s", 0)
        filename = ""

        # Try to decrypt filename
        if "at" in data and key:
            try:
                full_key = _base64_to_a32(key)
                k = _file_key(full_key)
                attrs = _decrypt_attr(_base64_url_decode(data["at"]), k)
                if attrs and "n" in attrs:
                    filename = attrs["n"]
            except Exception as e:
                logger.debug("Failed to decrypt MEGA filename: %s", e)

        return {
            "success": True,
            "provider": "mega",
            "size": size,
            "filename": filename or "MEGA file",
        }
    except Exception as e:
        logger.error("MEGA probe failed: %s", e)
        return {"success": False, "error": str(e)}


async def probe_mega_folder(url: str) -> dict:
    """Probe a MEGA folder URL for file list with names and sizes."""
    import aiohttp

    info = parse_mega_url(url)
    if not info or info["type"] != "folder":
        return {"success": False, "error": "Invalid MEGA folder URL"}

    try:
        folder_key = _base64_to_a32(info["key"])

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{MEGA_API}?n={info['handle']}",
                json=[{"a": "f", "c": 1, "r": 1}],
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()

        if isinstance(data, list):
            data = data[0]
        if isinstance(data, int):
            return {"success": False, "error": f"MEGA API error: {data}"}

        nodes = data.get("f", [])
        files = []
        total_size = 0

        for node in nodes:
            if node.get("t") != 0:  # 0 = file, 1 = folder
                continue

            size = node.get("s", 0)
            total_size += size
            filename = ""

            # Decrypt filename: node["k"] = "HANDLE:ENCRYPTED_KEY"
            k_str = node.get("k", "")
            if ":" in k_str:
                try:
                    enc_key_b64 = k_str.split(":")[1]
                    enc_key = _base64_to_a32(enc_key_b64)
                    dec_key = _decrypt_key(enc_key, folder_key)
                    fk = _file_key(dec_key)

                    if "a" in node:
                        attrs = _decrypt_attr(_base64_url_decode(node["a"]), fk)
                        if attrs and "n" in attrs:
                            filename = attrs["n"]
                except Exception as e:
                    logger.debug("Failed to decrypt MEGA folder file: %s", e)

            file_handle = node.get("h", "")
            # Build per-file URL using folder link + file handle
            file_url = f"https://mega.nz/folder/{info['handle']}#{info['key']}/file/{file_handle}"

            files.append({
                "name": filename or f"file_{file_handle}",
                "size": size,
                "url": file_url,
            })

        return {
            "success": True,
            "provider": "mega",
            "size": total_size,
            "filename": f"{len(files)} files" if len(files) != 1 else (files[0]["name"] if files else ""),
            # Always return the file list, even for a single-file folder. The
            # per-file download URL only exists inside this list; nulling it
            # out for len==1 made single-file folders impossible to expand,
            # so they fell back to the raw /folder/ URL and failed to download.
            "files": files or None,
        }
    except Exception as e:
        logger.error("MEGA folder probe failed: %s", e)
        return {"success": False, "error": str(e)}


async def _mega_api_post(session, url: str, json_payload: list, timeout: int = 30):
    """POST to MEGA API with retry on temporary / quota errors."""
    import asyncio, aiohttp

    for attempt in range(_MEGA_MAX_RETRIES):
        async with session.post(
            url, json=json_payload,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            data = await resp.json(content_type=None)

        if isinstance(data, list):
            data = data[0]

        if isinstance(data, int):
            err_name = _MEGA_ERROR_NAMES.get(data, f"unknown ({data})")

            # Auth / session errors — don't retry, surface clear message
            if data in _MEGA_AUTH_ERROR_CODES:
                raise RuntimeError(
                    f"MEGA session error: {err_name}. "
                    "Your mega_sid may have expired. "
                    "Please open mega.nz in the embedded browser to refresh your session."
                )

            # Quota exceeded — retry with much longer delay
            if data in _MEGA_QUOTA_CODES and attempt < _MEGA_MAX_RETRIES - 1:
                delay = 30 * (2 ** attempt)  # 30s, 60s, 120s, ...
                logger.warning(
                    "MEGA quota exceeded (%s), waiting %.0fs before retry (attempt %d/%d)",
                    err_name, delay, attempt + 1, _MEGA_MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                continue

            # Temporary errors — retry with standard backoff
            if data in _MEGA_RETRY_CODES and attempt < _MEGA_MAX_RETRIES - 1:
                delay = _MEGA_RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "MEGA API error %s, retrying in %.0fs (attempt %d/%d)",
                    err_name, delay, attempt + 1, _MEGA_MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                continue

            raise RuntimeError(f"MEGA API error: {err_name}")

        return data

    raise RuntimeError("MEGA API retries exhausted")


async def validate_mega_sid(sid: str) -> dict:
    """Validate a MEGA session ID by querying account quota.

    Distinguishes a genuinely dead session (auth error → caller should drop
    the sid) from a transient server hiccup (overload/throttle/network → the
    sid is probably fine, keep using it). Transient codes are retried with
    backoff before giving up.

    Returns:
        {"valid": True, "type": ...}                       — session works
        {"valid": False, "auth_error": True,  "error": ...} — sid is dead
        {"valid": False, "auth_error": False, "error": ...} — transient, keep sid
    """
    import asyncio

    import aiohttp

    if not sid:
        return {"valid": False, "auth_error": True, "error": "No mega_sid configured"}

    last_error = "unknown"
    for attempt in range(_MEGA_MAX_RETRIES):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{MEGA_API}?sid={sid}",
                    # "pro": 1 is required for the response to include utype;
                    # without it utype is absent and a Pro account looks free.
                    json=[{"a": "uq", "xfer": 1, "strg": 1, "pro": 1}],
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json(content_type=None)

            if isinstance(data, list):
                data = data[0]
            if isinstance(data, int):
                err_name = _MEGA_ERROR_NAMES.get(data, f"unknown ({data})")
                last_error = f"MEGA API error: {err_name}"
                if data in _MEGA_AUTH_ERROR_CODES:
                    return {"valid": False, "auth_error": True, "error": last_error}
                if data in _MEGA_RETRY_CODES or data in _MEGA_QUOTA_CODES:
                    delay = _MEGA_RETRY_BASE_DELAY * (2 ** attempt)
                    logger.info(
                        "MEGA sid validation hit %s, retrying in %.0fs (%d/%d)",
                        err_name, delay, attempt + 1, _MEGA_MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue
                # Unknown non-auth code: treat as transient, don't drop sid
                return {"valid": False, "auth_error": False, "error": last_error}

            is_premium = data.get("utype", 0) > 0
            return {
                "valid": True,
                "type": "premium" if is_premium else "free",
                "utype": data.get("utype", 0),
                "transfer_used": data.get("tuo", 0),
                "transfer_max": data.get("tal", 0),
                "storage_used": data.get("cstrg", 0),
                "storage_max": data.get("mstrg", 0),
            }
        except Exception as e:
            last_error = str(e)
            delay = _MEGA_RETRY_BASE_DELAY * (2 ** attempt)
            logger.info(
                "MEGA sid validation network error (%s), retrying in %.0fs (%d/%d)",
                last_error, delay, attempt + 1, _MEGA_MAX_RETRIES,
            )
            await asyncio.sleep(delay)

    # Exhausted retries on transient errors — keep the sid, it's likely fine
    return {"valid": False, "auth_error": False, "error": last_error}


async def download_mega_file(
    url: str,
    output_dir: Path,
    on_progress: callable = None,
    chunk_size: int = 65536,
    sid: str = "",
    max_segments: int = 8,
) -> Path:
    """Download and decrypt a MEGA file (standalone or from folder).

    Supports:
        - mega.nz/file/HANDLE#KEY (standalone file)
        - mega.nz/folder/FOLDER#KEY/file/FILE_HANDLE (file within folder)

    Uses parallel segmented downloads to overcome MEGA's per-connection throttle.

    Returns:
        Path to the downloaded file
    """
    import aiohttp

    info = parse_mega_url(url)
    if not info:
        raise ValueError(f"Invalid MEGA URL: {url}")

    # Clamp to MEGA's tolerance — too many parallel connections get reset.
    if max_segments > _MEGA_MAX_SEGMENTS:
        logger.info(
            "MEGA: capping segments %d -> %d (CDN resets too-parallel downloads)",
            max_segments, _MEGA_MAX_SEGMENTS,
        )
        max_segments = _MEGA_MAX_SEGMENTS

    if sid:
        logger.info("MEGA download with session ID (first 8 chars): %s...", sid[:8])
    else:
        logger.warning("MEGA download without session ID — anonymous mode (bandwidth limited)")

    if info["type"] == "folder_file":
        return await _download_folder_file(info, output_dir, on_progress, chunk_size, sid, max_segments)
    if info["type"] == "file":
        return await _download_standalone_file(info, output_dir, on_progress, chunk_size, sid, max_segments)

    raise ValueError(f"Cannot download MEGA URL (type={info['type']}): {url}")


async def _download_standalone_file(
    info: dict, output_dir: Path, on_progress, chunk_size: int, sid: str,
    max_segments: int = 8,
) -> Path:
    """Download a standalone MEGA file (mega.nz/file/HANDLE#KEY)."""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        api_url = f"{MEGA_API}?sid={sid}" if sid else MEGA_API
        if sid:
            logger.info("Using MEGA premium session for download")

        data = await _mega_api_post(
            session, api_url,
            [{"a": "g", "g": 1, "p": info["handle"], "ssm": 1}],
        )

        download_url = data.get("g")
        if not download_url:
            raise RuntimeError("MEGA API did not return download URL")

        file_size = data.get("s", 0)
        full_key = _base64_to_a32(info["key"])
        k = _file_key(full_key)
        iv = (full_key[4], full_key[5])

        filename = ""
        if "at" in data:
            try:
                attrs = _decrypt_attr(_base64_url_decode(data["at"]), k)
                if attrs and "n" in attrs:
                    filename = attrs["n"]
            except Exception:
                pass
        if not filename:
            filename = f"mega_{info['handle']}"

        return await _stream_decrypt(
            session, download_url, output_dir, filename, file_size, k, iv,
            on_progress, chunk_size, max_segments,
        )


async def _download_folder_file(
    info: dict, output_dir: Path, on_progress, chunk_size: int, sid: str,
    max_segments: int = 8,
) -> Path:
    """Download a file from within a MEGA folder.

    Requires folder master key to decrypt the per-file key.
    """
    import aiohttp

    folder_handle = info["folder_handle"]
    folder_key = _base64_to_a32(info["folder_key"])
    file_handle = info["file_handle"]

    async with aiohttp.ClientSession() as session:
        # Step 1: List folder contents to find the file's encrypted key
        data = await _mega_api_post(
            session, f"{MEGA_API}?n={folder_handle}",
            [{"a": "f", "c": 1, "r": 1}],
        )

        # Find the target file node
        nodes = data.get("f", [])
        target = None
        for node in nodes:
            if node.get("h") == file_handle:
                target = node
                break
        if not target:
            raise RuntimeError(f"File {file_handle} not found in MEGA folder {folder_handle}")

        # Step 2: Decrypt file key using folder master key
        k_str = target.get("k", "")
        if ":" not in k_str:
            raise RuntimeError(f"Missing encrypted key for file {file_handle}")
        enc_key = _base64_to_a32(k_str.split(":")[1])
        dec_key = _decrypt_key(enc_key, folder_key)
        k = _file_key(dec_key)
        iv = (dec_key[4], dec_key[5])

        # Decrypt filename
        filename = ""
        if "a" in target:
            try:
                attrs = _decrypt_attr(_base64_url_decode(target["a"]), k)
                if attrs and "n" in attrs:
                    filename = attrs["n"]
            except Exception:
                pass
        if not filename:
            filename = f"mega_{file_handle}"

        file_size = target.get("s", 0)

        # Step 3: Get download URL (use folder context + sid for premium)
        api_url = f"{MEGA_API}?n={folder_handle}"
        if sid:
            api_url += f"&sid={sid}"
            logger.info("Using MEGA premium session for folder file download")

        dl_data = await _mega_api_post(
            session, api_url,
            [{"a": "g", "g": 1, "n": file_handle}],
        )

        download_url = dl_data.get("g")
        if not download_url:
            raise RuntimeError("MEGA API did not return download URL for folder file")

        return await _stream_decrypt(
            session, download_url, output_dir, filename, file_size, k, iv,
            on_progress, chunk_size, max_segments,
        )


async def _stream_decrypt(
    session, download_url: str, output_dir: Path, filename: str,
    file_size: int, k: tuple, iv: tuple,
    on_progress, chunk_size: int, max_segments: int = 16,
) -> Path:
    """Download a MEGA file using parallel segmented Range requests + AES-CTR decrypt.

    AES-CTR is seekable: each 16-byte block has counter = base_iv + block_index,
    so segments can be decrypted independently. Each segment streams directly to
    the output file at its correct offset — no buffering entire file in memory.
    """
    import asyncio as _aio

    import aiohttp

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / filename
    key_bytes = _a32_to_bytes(k)
    iv_bytes = _a32_to_bytes(iv)
    base_ctr = int.from_bytes(iv_bytes + b"\0" * 8, "big")

    # For tiny files or unknown size, fall back to single stream
    if file_size <= 0 or file_size < 256 * 1024 or max_segments <= 1:
        return await _single_stream_decrypt(
            session, download_url, output_path, file_size, key_bytes,
            base_ctr, on_progress, chunk_size,
        )

    # Segment boundaries must align to AES block size (16 bytes)
    n_seg = min(max_segments, max(1, file_size // (64 * 1024)))
    seg_size = (file_size // n_seg // 16) * 16  # Align to 16-byte blocks

    segments = []
    for i in range(n_seg):
        start = i * seg_size
        end = (i + 1) * seg_size - 1 if i < n_seg - 1 else file_size - 1
        segments.append((start, end))

    downloaded = [0]  # Shared mutable counter

    # Pre-allocate file
    with open(output_path, "wb") as f:
        f.seek(file_size - 1)
        f.write(b"\0")

    async def _download_segment(idx: int, start: int, end: int):
        # Each segment owns its own file handle and writes its region purely
        # sequentially. A single SHARED handle is catastrophic here: the 32
        # segments sit tens of MB apart, so seeking the shared pointer between
        # them flushes the buffer and turns every chunk into a scattered random
        # write — measured ~0.1 MB/s vs ~5 MB/s with per-segment sequential
        # writes. The OS coalesces each segment's sequential stream efficiently.
        #
        # resume_pos is the absolute offset to (re)start from. MEGA frequently
        # drops a connection mid-segment; rather than discard the bytes already
        # written, we resume from here. It stays 16-byte aligned (iter_chunked
        # yields full chunks except the final one, after which we return), so
        # the AES-CTR counter realigns exactly.
        resume_pos = start
        with open(output_path, "r+b") as seg_f:
            for retry in range(_MEGA_SEGMENT_MAX_RETRIES):
                try:
                    ctr = Counter.new(128, initial_value=base_ctr + resume_pos // 16)
                    decryptor = AES.new(key_bytes, AES.MODE_CTR, counter=ctr)
                    seg_f.seek(resume_pos)

                    async with session.get(
                        download_url,
                        headers={"Range": f"bytes={resume_pos}-{end}"},
                        timeout=aiohttp.ClientTimeout(total=3600, sock_read=120),
                    ) as resp:
                        if resp.status not in (200, 206):
                            raise RuntimeError(f"MEGA segment {idx} HTTP {resp.status}")
                        async for chunk in resp.content.iter_chunked(chunk_size):
                            seg_f.write(decryptor.decrypt(chunk))
                            downloaded[0] += len(chunk)
                            resume_pos += len(chunk)  # commit — survives a reset
                            if on_progress:
                                on_progress(downloaded[0], file_size)
                    return  # Success
                except Exception as e:
                    if retry < _MEGA_SEGMENT_MAX_RETRIES - 1:
                        delay = min(0.5 * (2 ** retry), 5)  # 0.5,1,2,4s — quick resume
                        logger.warning(
                            "MEGA segment %d dropped at +%d/%d bytes (%s), resuming in "
                            "%.1fs (attempt %d/%d)",
                            idx, resume_pos - start, end - start + 1, e, delay,
                            retry + 1, _MEGA_SEGMENT_MAX_RETRIES,
                        )
                        await _aio.sleep(delay)
                    else:
                        raise

    # Download all segments in parallel
    tasks = [_download_segment(i, s, e) for i, (s, e) in enumerate(segments)]
    await _aio.gather(*tasks)

    # Truncate to actual file size (MEGA pads to AES block boundary)
    if file_size > 0 and output_path.stat().st_size > file_size:
        with open(output_path, "r+b") as f:
            f.truncate(file_size)

    logger.info("MEGA download complete: %s (%d bytes, %d segments)", filename, file_size, n_seg)
    return output_path


async def _single_stream_decrypt(
    session, download_url: str, output_path: Path,
    file_size: int, key_bytes: bytes, base_ctr: int,
    on_progress, chunk_size: int,
) -> Path:
    """Fallback: single-stream download + decrypt for tiny files."""
    import aiohttp

    ctr = Counter.new(128, initial_value=base_ctr)
    decryptor = AES.new(key_bytes, AES.MODE_CTR, counter=ctr)
    downloaded = 0

    async with session.get(
        download_url,
        timeout=aiohttp.ClientTimeout(total=3600, sock_read=120),
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"MEGA download HTTP {resp.status}")
        with open(output_path, "wb") as f:
            async for chunk in resp.content.iter_chunked(chunk_size):
                f.write(decryptor.decrypt(chunk))
                downloaded += len(chunk)
                if on_progress:
                    on_progress(downloaded, file_size)

    if file_size > 0 and output_path.stat().st_size > file_size:
        with open(output_path, "r+b") as f:
            f.truncate(file_size)

    logger.info("MEGA download complete: %s (%d bytes)", output_path.name, file_size)
    return output_path

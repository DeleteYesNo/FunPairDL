from __future__ import annotations

import logging
from urllib.parse import urlparse

from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.mega")


class MegaProvider(BaseProvider):
    """Provider for MEGA cloud storage.

    resolve() probes the MEGA public API for the real filename/size (one
    cheap POST; folder listings are TTL-cached in funpairdl.utils.mega_api).
    The download itself has no direct URL — files are decrypted client-side —
    so ResolvedFile carries is_mega=True and queue_manager delegates to
    funpairdl.utils.mega_api.download_mega_file. The mega.py library is NOT
    used (it is broken on Python 3.11+).
    """

    def __init__(self, email: str = "", password: str = ""):
        # Kept for registry compatibility; credentials are unused — MEGA
        # sessions come from the embedded browser (settings.mega_sid).
        self.email = email
        self.password = password

    @staticmethod
    def can_handle(url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "mega.nz" in host or "mega.co.nz" in host

    @property
    def name(self) -> str:
        return "mega"

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        # Populate name/size up front so the queue shows real info as soon
        # as the item resolves, instead of "mega_HANDLE" / 0 bytes until the
        # download actually starts (audit [1e]).
        filename = ""
        total_size = 0
        try:
            from funpairdl.utils.mega_api import probe_mega_file

            probe = await probe_mega_file(url)
            if probe.get("success"):
                total_size = int(probe.get("size") or 0)
                name = probe.get("filename") or ""
                if name and name != "MEGA file":  # skip the probe placeholder
                    filename = sanitize_filename(name)
            else:
                logger.warning(
                    "MEGA resolve probe failed for %s: %s",
                    url[:80], probe.get("error"),
                )
        except Exception as e:
            # Metadata is best-effort — the download itself re-derives it.
            logger.warning("MEGA resolve probe error for %s: %s", url[:80], e)

        return ResolvedFile(
            direct_url=url,
            filename=filename,
            total_size=total_size,
            supports_range=False,
            is_mega=True,
            mega_url=url,
        )

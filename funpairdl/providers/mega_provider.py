from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.mega")


class MegaProvider(BaseProvider):
    """Provider for MEGA cloud storage. Uses mega.py library.
    MEGA uses encryption so segmented downloads are not possible.
    Downloads are delegated to the mega.py library."""

    def __init__(self, email: str = "", password: str = ""):
        self.email = email
        self.password = password
        self._mega = None

    @staticmethod
    def can_handle(url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "mega.nz" in host or "mega.co.nz" in host

    @property
    def name(self) -> str:
        return "mega"

    def _get_mega(self):
        if self._mega is None:
            from mega import Mega
            mega = Mega()
            if self.email and self.password:
                self._mega = mega.login(self.email, self.password)
                logger.info("Logged in to MEGA as %s", self.email)
            else:
                self._mega = mega.login()
                logger.info("Using anonymous MEGA login")
        return self._mega

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        # MEGA files cannot be resolved to a direct URL
        # They must be downloaded via the mega.py library
        return ResolvedFile(
            direct_url=url,
            filename="",  # Will be determined during download
            total_size=0,
            supports_range=False,
            is_mega=True,
            mega_url=url,
        )

    async def download_file(
        self,
        url: str,
        output_dir: Path,
        on_progress: callable = None,
    ) -> Path:
        """Download a file from MEGA directly to output_dir."""

        def _download():
            mega = self._get_mega()
            output_dir.mkdir(parents=True, exist_ok=True)
            # mega.py downloads to a temp location then moves
            result = mega.download_url(url, dest_path=str(output_dir))
            return result

        result_path = await asyncio.to_thread(_download)

        if result_path:
            return Path(result_path)

        raise RuntimeError(f"MEGA download failed for: {url}")

    async def download_folder(
        self,
        url: str,
        output_dir: Path,
    ) -> list[Path]:
        """Download all files from a MEGA folder."""

        def _download_folder():
            mega = self._get_mega()
            output_dir.mkdir(parents=True, exist_ok=True)
            # Get folder contents
            # mega.py doesn't have a clean folder download API
            # We need to get the folder link and download each file
            folder = mega.get_public_url_info(url)
            files = []

            if hasattr(mega, 'download_url'):
                result = mega.download_url(url, dest_path=str(output_dir))
                if result:
                    files.append(Path(result))

            return files

        return await asyncio.to_thread(_download_folder)

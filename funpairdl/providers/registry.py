from __future__ import annotations

import logging

from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.providers.pixeldrain import PixeldrainProvider
from funpairdl.providers.mega_provider import MegaProvider
from funpairdl.providers.iwara import IwaraProvider
from funpairdl.providers.gofile import GoFileProvider
from funpairdl.providers.ytdlp_generic import YtdlpGenericProvider
from funpairdl.providers.eroscripts import EroScriptsProvider
from funpairdl.providers.hmvmania import HmvManiaProvider
from funpairdl.providers.direct_http import DirectHTTPProvider

logger = logging.getLogger("funpairdl.providers.registry")


class ProviderRegistry:
    """Registry of all download providers. Matches URLs to the right provider."""

    def __init__(
        self,
        pixeldrain_api_key: str = "",
        mega_email: str = "",
        mega_password: str = "",
        gofile_token: str = "",
    ):
        # Order matters: specific providers first, generic fallback last
        self._providers: list[BaseProvider] = [
            PixeldrainProvider(api_key=pixeldrain_api_key),
            MegaProvider(email=mega_email, password=mega_password),
            GoFileProvider(token=gofile_token),
            IwaraProvider(),
            EroScriptsProvider(),
            HmvManiaProvider(),
            YtdlpGenericProvider(),
            DirectHTTPProvider(),  # fallback
        ]

    def get_provider(self, url: str) -> BaseProvider:
        for provider in self._providers:
            if provider.can_handle(url):
                logger.debug("URL %s matched provider: %s", url, provider.name)
                return provider
        # Should never reach here since DirectHTTPProvider handles all HTTP
        return self._providers[-1]

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        provider = self.get_provider(url)
        return await provider.resolve(url, **kwargs)

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ResolvedFile:
    """Result of resolving a URL to a downloadable file."""
    direct_url: str
    filename: str
    total_size: int = 0
    supports_range: bool = True
    headers: dict[str, str] | None = None
    # For HLS streams
    is_hls: bool = False
    # For MEGA - delegate to library
    is_mega: bool = False
    mega_url: str = ""


class BaseProvider(ABC):
    """Abstract base class for download providers."""

    @staticmethod
    @abstractmethod
    def can_handle(url: str) -> bool:
        """Return True if this provider can handle the given URL."""
        ...

    @abstractmethod
    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        """Resolve a URL to a direct download link with metadata."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        ...

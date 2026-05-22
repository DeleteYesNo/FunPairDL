from __future__ import annotations

import time
from collections import deque


class SpeedCalculator:
    """Rolling window speed calculator."""

    def __init__(self, window_seconds: float = 3.0):
        self._window = window_seconds
        self._samples: deque[tuple[float, int]] = deque()
        self._total_bytes: int = 0

    def update(self, bytes_downloaded: int) -> None:
        now = time.monotonic()
        self._samples.append((now, bytes_downloaded))
        self._total_bytes = bytes_downloaded
        self._prune(now)

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    @property
    def speed_bps(self) -> float:
        if len(self._samples) < 2:
            return 0.0
        now = time.monotonic()
        self._prune(now)
        if len(self._samples) < 2:
            return 0.0
        oldest_time, oldest_bytes = self._samples[0]
        newest_time, newest_bytes = self._samples[-1]
        dt = newest_time - oldest_time
        if dt <= 0:
            return 0.0
        return (newest_bytes - oldest_bytes) / dt

    def reset(self) -> None:
        self._samples.clear()
        self._total_bytes = 0


def format_size(bytes_val: int | float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(bytes_val) < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} PB"


def format_speed(bps: float) -> str:
    return f"{format_size(bps)}/s"


def format_eta(remaining_bytes: int, speed_bps: float) -> str:
    if speed_bps <= 0 or remaining_bytes <= 0:
        return "--:--"
    seconds = int(remaining_bytes / speed_bps)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}h {minutes}m"

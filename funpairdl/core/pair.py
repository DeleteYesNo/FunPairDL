from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path


class PairState(Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class ItemState(Enum):
    PENDING = "pending"
    RESOLVING = "resolving"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class FileType(Enum):
    VIDEO = "video"
    FUNSCRIPT = "funscript"
    OTHER = "other"


@dataclass
class SegmentInfo:
    index: int
    range_start: int
    range_end: int
    downloaded: int = 0
    temp_file: str = ""


@dataclass
class PairItem:
    url: str
    filename: str
    file_type: FileType
    provider_name: str = "direct"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: ItemState = ItemState.PENDING
    total_bytes: int = 0
    downloaded_bytes: int = 0
    speed_bps: float = 0.0
    error_message: str = ""
    resolved_url: str = ""
    segments: list[SegmentInfo] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    is_bundle: bool = False
    author: str = ""
    # Group assignment within the parent Pair. "" or "Main" → root folder;
    # "Alt 1", "Alt 2", ... → .alt[N-1] subfolder at organize time.
    group: str = ""

    @property
    def progress(self) -> float:
        if self.state == ItemState.COMPLETED:
            return 100.0
        if self.total_bytes <= 0:
            return 0.0
        return min(self.downloaded_bytes / self.total_bytes * 100, 100.0)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "filename": self.filename,
            "file_type": self.file_type.value,
            "provider_name": self.provider_name,
            "state": self.state.value,
            "total_bytes": self.total_bytes,
            "downloaded_bytes": self.downloaded_bytes,
            "resolved_url": self.resolved_url,
            "author": self.author,
            "group": self.group,
            "error_message": self.error_message,
            "segments": [
                {
                    "index": s.index,
                    "range_start": s.range_start,
                    "range_end": s.range_end,
                    "downloaded": s.downloaded,
                    "temp_file": s.temp_file,
                }
                for s in self.segments
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> PairItem:
        item = cls(
            url=d["url"],
            filename=d["filename"],
            file_type=FileType(d["file_type"]),
            provider_name=d.get("provider_name", "direct"),
            id=d.get("id", uuid.uuid4().hex[:12]),
            state=ItemState(d.get("state", "pending")),
            total_bytes=d.get("total_bytes", 0),
            downloaded_bytes=d.get("downloaded_bytes", 0),
            resolved_url=d.get("resolved_url", ""),
            is_bundle=d.get("is_bundle", False),
            author=d.get("author", ""),
            group=d.get("group", ""),
            error_message=d.get("error_message", ""),
        )
        item.segments = [
            SegmentInfo(**s) for s in d.get("segments", [])
        ]
        return item


@dataclass
class Pair:
    name: str
    items: list[PairItem] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: PairState = PairState.QUEUED
    output_dir: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    error_message: str = ""
    preferred_resolution: str = "best"
    auto_rename: bool = True
    organized: bool = False
    original_filenames: dict[str, str] = field(default_factory=dict)  # {item_id: original_filename}
    # Per-Alt-group config. Key is group name ("Alt 1", "Alt 2", ...).
    # Currently only `inherit_multi_axis` — whether Main's multi-axis
    # funscripts should be hardlinked into this Alt's subfolder.
    alt_group_config: dict[str, dict] = field(default_factory=dict)

    @property
    def total_bytes(self) -> int:
        return sum(i.total_bytes for i in self.items)

    @property
    def downloaded_bytes(self) -> int:
        return sum(i.downloaded_bytes for i in self.items)

    @property
    def progress(self) -> float:
        if not self.items:
            return 0.0

        completed = sum(1 for i in self.items if i.state == ItemState.COMPLETED)

        # If every item is done, report 100% regardless of byte state.
        # (downloaded_bytes may be stale/zero after a queue reload.)
        if completed == len(self.items):
            return 100.0

        total = self.total_bytes
        if total <= 0:
            # No byte info — fall back to item completion ratio
            return completed / len(self.items) * 100
        pct = min(self.downloaded_bytes / total * 100, 100.0)
        # Don't report 100% unless every item actually completed —
        # items that failed before getting a size (total_bytes=0) would
        # otherwise be invisible to the byte-based calculation.
        if pct >= 100.0 and completed < len(self.items):
            return completed / len(self.items) * 100
        return pct

    @property
    def speed_bps(self) -> float:
        return sum(i.speed_bps for i in self.items if i.state == ItemState.DOWNLOADING)

    @property
    def video_items(self) -> list[PairItem]:
        return [i for i in self.items if i.file_type == FileType.VIDEO]

    @property
    def script_items(self) -> list[PairItem]:
        return [i for i in self.items if i.file_type == FileType.FUNSCRIPT]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "state": self.state.value,
            "output_dir": self.output_dir,
            "created_at": self.created_at,
            "preferred_resolution": self.preferred_resolution,
            "auto_rename": self.auto_rename,
            "organized": self.organized,
            "original_filenames": self.original_filenames,
            "alt_group_config": self.alt_group_config,
            "error_message": self.error_message,
            "items": [i.to_dict() for i in self.items],
        }

    @classmethod
    def from_dict(cls, d: dict) -> Pair:
        pair = cls(
            name=d["name"],
            id=d.get("id", uuid.uuid4().hex[:12]),
            state=PairState(d.get("state", "queued")),
            output_dir=d.get("output_dir", ""),
            created_at=d.get("created_at", datetime.now().isoformat()),
            preferred_resolution=d.get("preferred_resolution", "best"),
            auto_rename=d.get("auto_rename", True),
            organized=d.get("organized", False),
            original_filenames=d.get("original_filenames", {}),
            alt_group_config=d.get("alt_group_config", {}),
            error_message=d.get("error_message", ""),
        )
        pair.items = [PairItem.from_dict(i) for i in d.get("items", [])]
        return pair

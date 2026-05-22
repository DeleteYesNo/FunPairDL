from __future__ import annotations

import json
import logging
from pathlib import Path

from funpairdl.constants import QUEUE_FILE
from funpairdl.core.pair import ItemState, Pair, PairState

logger = logging.getLogger("funpairdl.persistence.queue_store")


class QueueStore:
    """Persists download queue state to JSON file."""

    def __init__(self, path: Path = QUEUE_FILE):
        self.path = path

    def save(self, pairs: list[Pair]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = [p.to_dict() for p in pairs]
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error("Failed to save queue: %s", e)

    def load(self) -> list[Pair]:
        if not self.path.exists():
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)

            pairs = []
            for d in data:
                pair = Pair.from_dict(d)
                # Reset downloading pairs to queued on reload
                if pair.state == PairState.DOWNLOADING:
                    pair.state = PairState.QUEUED
                # Force-sync bytes for completed items — queue may have
                # been saved before bytes were synced (crash, old version).
                for item in pair.items:
                    if item.state == ItemState.COMPLETED and item.total_bytes > 0:
                        item.downloaded_bytes = item.total_bytes
                pairs.append(pair)

            logger.info("Loaded %d pairs from queue store", len(pairs))
            return pairs
        except Exception as e:
            logger.error("Failed to load queue: %s", e)
            return []

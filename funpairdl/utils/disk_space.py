"""Free space of the disk a path lives on."""
from __future__ import annotations

import shutil
from pathlib import Path


def drive_usage(path: str | Path):
    """``shutil.disk_usage`` of the drive holding ``path``, or None when it
    can't be read. Asks the drive root, not the folder: a cloud work is a
    junction into the cloud drive, and writing into one first pulls it back
    to this disk."""
    p = Path(path)
    try:
        return shutil.disk_usage(p.anchor or p)
    except OSError:
        return None

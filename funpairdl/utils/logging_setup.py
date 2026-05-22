import logging
import sys
from pathlib import Path

from funpairdl.constants import LOG_FILE


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("funpairdl")
    logger.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler (always works)
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Console handler (only when stdout is available, i.e. not pythonw.exe)
    if sys.stdout is not None:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(level)
        ch.setFormatter(fmt)
        logger.addHandler(ch)

    return logger

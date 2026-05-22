"""Lightweight entry point: API server + download engine only (no GUI).

Use this with the browser extension. No PySide6/Qt dependency required.
Usage: python run_server.py
"""

import sys
import os

os.environ["PYTHONUTF8"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from funpairdl.server import run

if __name__ == "__main__":
    run()

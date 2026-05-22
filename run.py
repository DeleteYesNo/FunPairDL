"""Entry point for FunPairDL."""

import sys
import os

# Force UTF-8 mode on Windows
os.environ["PYTHONUTF8"] = "1"

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from funpairdl.app import run

if __name__ == "__main__":
    run()

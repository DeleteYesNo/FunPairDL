"""FunPairDL launcher — no console window.
Double-click this file to start FunPairDL silently.
"""
import sys
import os

# Force UTF-8 mode (equivalent to python -X utf8)
os.environ["PYTHONUTF8"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from funpairdl.app import run

if __name__ == "__main__":
    run()

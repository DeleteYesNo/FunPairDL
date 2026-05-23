"""Run the content.js helper-function regression suite under pytest.

content.js is browser-injected JS with no Python entry point, so the actual
assertions live in tests/content_js_test.mjs (executed by Node). This wrapper
lets `pytest` cover them too; it skips cleanly when Node isn't installed.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_TEST = Path(__file__).parent / "content_js_test.mjs"


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_content_js_helpers():
    result = subprocess.run(
        [_NODE, str(_TEST)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"content.js helper assertions failed:\n{result.stdout}\n{result.stderr}"
    )

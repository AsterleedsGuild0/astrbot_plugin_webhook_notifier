"""Test configuration.

Adds the fake_astrbot package to sys.path so that core modules can be imported
during unit testing without requiring a full AstrBot runtime.
"""

from __future__ import annotations

import sys
from pathlib import Path

FAKE_ASTRBOT = str(Path(__file__).parent / "fake_astrbot")
if FAKE_ASTRBOT not in sys.path:
    sys.path.insert(0, FAKE_ASTRBOT)

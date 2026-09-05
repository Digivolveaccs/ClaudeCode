"""Test package.

Puts the repo root on sys.path so the suite runs from anywhere, with or without
the package installed.
"""

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

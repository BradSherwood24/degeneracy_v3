"""Make `import service.*` resolve when pytest runs from anywhere in the repo."""

import os
import sys

_PILOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PILOT_DIR not in sys.path:
    sys.path.insert(0, _PILOT_DIR)
